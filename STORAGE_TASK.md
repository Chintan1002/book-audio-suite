# TASK BRIEF — Save to the user's own Google Drive

Authoritative build spec for the "bring your own storage" model. Supersedes the
marked sections of `STORAGE_DESIGN.md` and most of `PRD_MULTIUSER.md`.

---

## 0. Read first

- `STORAGE_DESIGN.md` — the original design and its rationale (§8 is the "why").
- `CLAUDE.md` — working style: architect before code, verify each stage.
- `app.py` — the job/download flow this bolts onto. Confirmed already: a
  finished job's artifacts are recorded as paths in `job.result` and served from
  `jobs/<id>/out/`, which is exactly what a save will read.
- `db/repository.py` — the persistence seam; `prune_stale_jobs()` already exists
  and is currently never called.

## 1. Objective

After a job finishes, the user can save **every artifact it produced** into
**their own Google Drive**, and later reach it from our site with one click. We
store no files and no content — only a pointer to their folder.

The core tool stays anonymous. Download never requires sign-in.

## 2. Decisions (these override STORAGE_DESIGN)

| # | Decision | Why |
|---|---|---|
| D1 | Save **all** artifacts, in a **folder per conversion** | A real book is 1 full file + 19 chapters + an optional text PDF; flat would bury it |
| D2 | Persist `google_sub -> folder_id/url`, nothing else | Otherwise "View my library" tomorrow needs a fresh `drive.file` consent just to search for a folder the user owns |
| D3 | Two-tier sign-in: identity vs `drive.file` | Showing the library link shouldn't demand Drive permission; saving should |
| D4 | Keep generated files **7 days**, then sweep | "Never at rest" and "download always works" are mutually exclusive; 7 days keeps re-saving possible and stops `jobs/` growing (it is 664 MB today) |

## 3. Drive layout

```
Book·Audio Suite/
├── Audiobooks/
│   └── <Title> — <YYYY-MM-DD>/          one per conversion
│       ├── <Title>.mp3                   whatever format the job produced
│       ├── <Title>_text.pdf              only when OCR ran
│       └── Chapters/
│           └── NN_<Chapter>.mp3          one per detected chapter
└── Transcripts/
    └── <Title> — <YYYY-MM-DD>/
        ├── <Title>.txt
        ├── <Title>.md
        └── <Title>.json
```

The conversion folder is **find-or-create by name**, so re-saving the same job
updates rather than duplicating. Accepted edge case: two conversions with the
same title on the same day share a folder.

## 4. Auth model

Google Identity Services in the browser — **no client secret, no server-side
OAuth callback, no refresh tokens.**

```
tier 1  identity   openid, email            -> know who they are; show library link
tier 2  drive.file requested at first save  -> create folders, upload
```

- The access token lives in a JS variable for the life of the tab. Never
  `localStorage`, never our database, never a log line.
- Tokens travel in the `Authorization: Bearer` header. **Never a query string** —
  query params end up in access logs.
- `drive.file` only: the app can see and search *only the items it created*.
  It cannot read the rest of the user's Drive, by construction.

## 5. What we store

One table. No content, ever.

```sql
create table drive_links (
  google_sub  text primary key,          -- from the verified ID token
  folder_id   text not null,
  folder_url  text not null,
  updated_at  timestamptz not null default now()
);
```

`PRD_MULTIUSER.md` shrinks to this: the `transcripts` table and all content
storage are dropped. `db/repository.py`'s seam survives (anonymous jobs still
need in-flight state); `PostgresRepository` is cut down to `drive_links`.

## 6. Components to build

### 6.1 `drive.py` — new module, no web dependencies

```python
class DriveClient:
    def __init__(self, access_token: str)                 # short-lived, never stored
    def _search(self, q: str) -> list[dict]               # files.list within drive.file
    def find_or_create_folder(name, parent=None) -> str
    def ensure_structure() -> dict                        # {parent, audiobooks, transcripts}
    def ensure_conversion_folder(kind, title, when) -> str
    def upload_resumable(path, name, parent_id, mime, on_progress=None) -> str
```

Implementation notes:
- The parent search carries **no parent constraint** — the user may have moved
  the folder, and `drive.file` can only see our own items anyway.
- Resumable upload is the bulk of the work: initiate session → `PUT` chunks →
  handle `308 Resume Incomplete` → retry a dropped connection.
- Concurrent first-saves can create duplicate parents; re-search after create
  and keep the oldest.

### 6.2 Endpoints

```
POST /jobs/{job_id}/save-to-drive     Authorization: Bearer <drive.file token>
     -> ensure structure + conversion folder, upload every artifact
     -> {saved:[{name,id}], failed:[...], folder_url}

POST /drive/library                   Authorization: Bearer <identity token>
     -> look up drive_links by google_sub; fall back to Drive search + store
     -> {url}
```

A save is now ~21 uploads, so it must report progress per file and treat
**partial failure as a real state**: retry the failed file, then report exactly
which landed rather than failing the whole save.

### 6.3 UI (`index.html`)

- **"Save to Drive"** — a card beside the existing downloads in *both* result
  panels, shown only when a job is `done`.
  States: idle → *Connecting…* → *Uploading 7 of 21…* → **Saved ✓ Open folder**.
- **"View my library"** — in the header next to the theme toggle; triggers
  sign-in on click when needed, then opens the folder in a new tab.

Both are additive. The anonymous download path is untouched.

### 6.4 Retention sweep

A 7-day TTL over `jobs/<id>/`: delete the directory **and** call the existing
`prune_stale_jobs(timedelta(days=7))`, which is already implemented and
currently never invoked. Never sweep a job that is still queued or running.

## 7. Build order (verify before moving on)

1. **`drive.py` + CLI smoke test** — `python drive.py <token> <file>` creates the
   structure and uploads. Provable before any UI exists.
2. **`POST /jobs/{id}/save-to-drive`** — verify with curl against a real Drive.
3. **Retention sweep** — verify an old job directory is removed and a running
   one never is.
4. **`drive_links` + `POST /drive/library`** — verify the second visit needs no
   Drive consent.
5. **UI buttons** — verify end to end in the browser, including a large file to
   exercise resumable upload and a forced failure mid-upload.

## 8. Acceptance criteria

- A finished TTS job saves the full file, every chapter, and the text PDF into
  one dated folder under `Audiobooks/`; STT saves all three transcript formats.
- Re-saving the same job updates that folder instead of creating a second one.
- "View my library" on a later visit is a plain redirect — no Drive prompt.
- Deleting the folder in Drive and saving again recreates it cleanly.
- A killed connection mid-upload resumes rather than restarting.
- Job directories older than 7 days are gone; running jobs are untouched.
- **The anonymous flow still works with no sign-in anywhere.**
- No token and no transcript text ever appears in a log line.

## 9. Edge cases

User trashes or renames our folders (recreate, update the stored link) ·
token expiry mid-save (re-prompt, resume) · partial upload failure (retry the
file, report precisely) · duplicate folder creation under concurrency · a job
whose files were already swept (offer regeneration, not a broken save) · Drive
storage full (surface Google's error verbatim) · same title twice in one day.

## 10. Out of scope

Refresh-token storage and background sync · other providers · sharing ·
browser-direct upload (a later bandwidth optimization; the file is already on
our server, so uploading from there is one hop instead of forcing the user to
re-upload 330 MB) · any server-side library UI.

## 11. Prerequisite you must supply

A Google Cloud project with an OAuth **client ID** (Web application),
`drive.file` + `openid`/`email` scopes, and `http://localhost:8000` as an
authorized JavaScript origin. No client secret is needed for this flow.
Google app verification and a privacy policy are required before public launch,
not for local development.
