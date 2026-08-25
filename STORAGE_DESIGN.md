# Design — User storage via their own Google Drive (no app database)

How finished creations are stored and where the "library" lives. Core principle:
**we store nothing. Google does.** The app writes each user's creations into
*their* Google Drive and the "library" is just a redirect to that folder.

---

> **Revised in review — see `STORAGE_TASK.md` for the authoritative build spec.**
> Four decisions supersede parts of this document:
> 1. **§2** — every conversion gets its own folder inside `Audiobooks/` or
>    `Transcripts/`. This reverses "do NOT nest a folder per book", which
>    assumed one file per job; we now save *all* artifacts (a real book is 21
>    files) and flat would be unusable.
> 2. **§3/§5** — we DO persist one thing: a `google_sub -> folder_id/url`
>    pointer, so "View my library" tomorrow is a plain redirect instead of a
>    fresh Drive-consent prompt for a folder the user already owns. Still no
>    files, no text, no job history.
> 3. **§5** — sign-in splits in two: identity (`openid`/`email`) to show the
>    library link, `drive.file` only when actually saving. The core tool stays
>    anonymous.
> 4. **§6** — generated files are kept for **7 days**, then swept. "Never at
>    rest" and "the download button always works" cannot both be true.
> 5. **§1/§5 — REVERSED: sign-in is now required to use the tool at all.** A
>    landing page gates everything behind "Continue with Google"; the anonymous
>    core flow is gone. Consequence to plan for: while the app is unverified
>    Google caps sign-in at 100 test users, so verification now blocks *all*
>    use rather than only the save feature. Mitigation in place: the gate asks
>    for identity only, and `drive.file` is still requested later, at save.

---

## 1. The idea

- Users sign in with Google **only to save** — not to use the tool. The core
  flow (upload → generate → download) stays anonymous; Drive save is an optional
  upgrade on top of the always-available download button.
- After a job finishes, the user can **"Save to my Drive."** The file lands in a
  folder the app owns inside their Drive.
- **"View my library"** redirects the browser to that Drive folder. No library
  UI is built on our side.

## 2. Folder structure (in the user's Drive)

```
Book·Audio Suite/            <- parent folder the app creates on first save
├── Audiobooks/              <- TTS output (.m4b)  — Feature A
└── Transcripts/             <- STT output (.txt/.md/.json) — Feature B
```

Keep it flat: two subfolders, mapped to the two features. Do NOT nest a folder
per book — the flat split is the cleaner mental model.

## 3. Statelessness — how we find the folders without a database

Use the Google **`drive.file` OAuth scope** (NOT full `drive`). `drive.file`
lets the app see and search only the files/folders **it created** — never the
rest of the user's Drive. That's exactly enough:

- On each session/save, run one `files.list` query to find the app's
  `Book·Audio Suite` folder (and its two subfolders) by name among app-created
  items, and get their IDs.
- If not found (first-time user, or they deleted it), **create** the parent +
  two subfolders fresh.
- We rederive folder IDs from Drive itself every time, so **no folder IDs or
  files are persisted in any database.**

## 4. The two actions

**Save (after a job completes):**
1. Ensure the folder structure exists (find-or-create, per section 3).
2. Upload the output into the right subfolder — `.m4b` → `Audiobooks/`,
   transcript files → `Transcripts/`.
3. Use the Drive API's **resumable upload** (audiobooks can be hundreds of MB).
4. Name files meaningfully: `"<Title> — <YYYY-MM-DD>.<ext>"`. Drive is now the
   filing system, so the filename is all the user browses by.

**View my library:**
1. Resolve the parent folder ID (authenticated Drive search — requires sign-in).
2. Redirect to `https://drive.google.com/drive/folders/<id>` in a new tab.
   Google renders the folder with both subfolders. No listing UI on our side.

## 5. Auth model

- OAuth 2.0, scope `drive.file` only.
- Sign-in is required at **save** and **view library** (both touch their Drive),
  never to run the core tool.
- Access tokens expire (~1 h). Since saving happens right after generation while
  the session is live, use the live token — **no refresh-token storage needed**
  for the immediate-save flow (keeps us truly database-free). Only introduce a
  stored refresh token if we later want background/offline uploads.

## 6. Where files live transiently

The pipeline generates the file on our server (temp job folder), we stream it to
the user's Drive, then delete the temp. We hold content only for the seconds of
the upload — never at rest.

## 7. Edge cases / gotchas

- **User reorganizes their Drive.** `drive.file` only sees app-created items, so
  if they rename/move/delete our folders, the next save may recreate a fresh
  `Book·Audio Suite`. Handle "not found → recreate" gracefully; the user owns
  their Drive.
- **"View library" needs sign-in** (folder ID is resolved via an authed call).
  That's fine — it's their private Drive.
- **Big uploads** → resumable upload, with retry on a dropped connection.
- **Bandwidth** flows through our server on server-side upload. Optional later
  optimization: have the browser upload directly to Drive with the user's token
  to keep bandwidth off our server entirely.
- **Google app verification** is the real-world hurdle before public launch
  (unverified apps show a warning + ~100-user cap). `drive.file` keeps this
  relatively light. Requires a privacy policy.

## 8. Why this is the right shape

Turns "where do we store user creations?" into "we don't — Google does." No
storage cost, no content held at rest, far less privacy/legal burden, and the
library is a redirect. The only real work is OAuth + folder-bootstrap-and-upload,
plus Google verification.

## 9. Out of scope (for now)

Refresh-token storage / background sync, other providers (Dropbox/OneDrive —
same pattern), sharing/collaboration. The always-available local **download**
button remains the zero-integration baseline.
