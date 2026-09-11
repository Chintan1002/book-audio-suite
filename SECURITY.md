# Security posture — Book·Audio Suite

An honest assessment of the code as it stands, written to answer two questions:
**can anyone look into the database, and is this safe to put on the internet?**

Findings below were verified against the running application, not assumed.
Severity uses the usual scale; "before launch" means *blocking*, not advisory.

---

## The short answer

**On your laptop today: fine.** Nothing is exposed to the internet, and the
design avoids several whole categories of risk (see "What is genuinely solid").

**On the public internet today: not safe.** The sign-in gate is cosmetic. Two
findings are blocking, and both are straightforward to fix.

> **Demonstrated, not theorised.** With no browser, no Google account and no
> session, a plain `curl` uploaded audio, ran a transcription, and downloaded
> the finished transcript. The landing page never entered the picture.
>
> ```
> POST /transcribe               -> accepted, job_id=ef273da19171
> GET  /jobs/ef273da19171        -> done
> GET  /jobs/…/transcript.txt    -> "Thank you, Archangel Uriel, for pouring…"
> ```

## "Can anyone look into the database?"

Three separate answers, because "the database" means three different things:

1. **Right now there is no database.** With `DATABASE_URL` unset the app runs on
   `MemoryRepository` — job state lives in process memory and dies with it. There
   is nothing on disk to read.
2. **If you enable Postgres/Supabase,** the database is only as reachable as you
   make it. Supabase databases are not open to the world by default; the danger
   is leaking the connection string or the service-role key, not someone
   "finding" the database.
3. **The row-level security in `db/schema.sql` currently protects nothing** — and
   this is the subtle part. RLS restricts queries made *as the user*. Our server
   connects as the owner/service role, which **bypasses RLS by design**. So the
   only thing separating one user's rows from another's is the `user_id` filter
   the repository applies in application code — and see S-2 for why that filter
   is currently a no-op.

The practical risk is not someone breaking into the database. It is the
application handing data to the wrong person through the front door.

---

## Findings

### 🔴 S-1 · No authentication on any API endpoint — **blocking**

The landing page and "Continue with Google" gate are **client-side only**. Every
endpoint that matters is open:

| Endpoint | Auth required |
|---|---|
| `POST /convert`, `POST /transcribe` | ❌ none |
| `GET /jobs/{id}` | ❌ none |
| `GET /jobs/{id}/full`, `/chapters.zip`, `/textpdf`, `/transcript.{fmt}` | ❌ none |
| `POST /jobs/{id}/save-to-drive`, `POST /drive/library` | ✅ Bearer token |

Only the two Drive routes check anything, and they check a *Google* token, not a
session with us.

**Impact.** Anyone who can reach the server can consume your compute and, given a
job id, read other people's transcripts.

**Fix.** Verify the Google ID token server-side and require it on every
non-public route: a FastAPI dependency that validates the JWT signature against
Google's JWKS, checks `aud` equals your client id, `iss` is `accounts.google.com`,
and `exp` is in the future — then returns `sub` as the user id. **Never trust a
JWT you have not verified**; decoding it (what the browser does for display) is
not verification.

### 🔴 S-2 · Every user shares one identity — **blocking for multi-user**

```python
CURRENT_USER = os.environ.get("DEV_USER_ID", "00000000-0000-0000-0000-000000000001")
```

Ownership checks read `repo.get_job(CURRENT_USER, job_id)`. Since `CURRENT_USER`
is a constant, that check passes for *everyone*: there is one tenant, and all
visitors are it. Combined with S-1, any visitor with a job id can read the
corresponding transcript.

The repository seam and the schema are already shaped for real users — the value
just has to come from the verified token in S-1 instead of a constant.

### 🟠 S-3 · No rate limiting or quotas — **high**

Transcription is CPU-bound and minutes long; TTS makes hundreds of upstream
calls. Nothing stops one client queueing jobs in a loop. A single script can
saturate the box, and the two-worker pool means everyone else simply waits.

**Fix.** Per-user quota (the `monthly_minutes_quota` column already exists) plus
per-IP rate limits at the reverse proxy, and a cap on concurrent jobs per user.

### 🟠 S-4 · No upload size limit — **high**

`shutil.copyfileobj` streams whatever arrives straight to disk. A single large
POST can fill the volume; a few can take the service down.

**Fix.** Reject oversized uploads by `Content-Length` before reading, enforce a
hard cap while streaming, and set `client_max_body_size` at the proxy.

### 🟡 S-5 · Job ids are the only access control on downloads — **medium**

Downloads are protected purely by knowing an unguessable id — a "capability URL".
That is a legitimate pattern, but here it is weakened three ways: the id is
truncated from 128 bits to **48** (`uuid.uuid4().hex[:12]`), it never expires,
and it travels in URLs, which end up in browser history, referrer headers and
proxy logs.

**Fix.** Use the full uuid, and once S-1 lands treat the id as an identifier
rather than a secret.

### 🟡 S-6 · Generated files are retained indefinitely — **medium**

`jobs/` is **664 MB** today and nothing deletes it. Every audiobook, transcript
and original upload stays on disk forever. That is both an availability problem
and a data-retention one: content you promised not to keep is still there.

**Fix.** The 7-day sweep already agreed in `STORAGE_TASK.md`; `prune_stale_jobs()`
exists and is not yet called.

### 🟡 S-7 · `innerHTML` used with server-derived values — **low**

Two spots in the save-status UI build markup by string concatenation including
`folder_url`. Not exploitable today (the value comes from Google's API), but it
is the wrong default.

**Fix.** `textContent` plus `createElement` for the link.

### 🟡 S-8 · No security headers, and HTTP in development — **low, becomes high in production**

No CSP, `X-Content-Type-Options`, `Referrer-Policy` or HSTS. Development runs
over plain HTTP, which is fine on localhost but would expose Google access
tokens in transit if ever deployed unencrypted.

**Fix.** TLS everywhere in production, HSTS, a restrictive CSP, and
`Referrer-Policy: no-referrer` so job ids do not leak via referrers.

---

## What is genuinely solid

Not everything needs fixing. These were checked and hold up:

- **No command injection.** Every `subprocess` call passes an argument list;
  `shell=True` appears nowhere in the codebase, so a filename can never become a
  shell command.
- **No path traversal.** Upload names are reduced with `Path(name).stem`, which
  discards directory components entirely. Verified with
  `../../../../etc/passwd.mp3` → `passwd`.
- **No accidental file exposure.** There is no `StaticFiles` mount; `jobs/` is
  reachable only through explicit download routes.
- **Google tokens are handled correctly.** Header only, never a query string
  (which would land in access logs), never persisted server-side, and confirmed
  to appear **zero times** in the server log. In the browser they live in a
  single JavaScript variable — not `localStorage`, not a cookie.
- **There is no client secret to leak.** The OAuth flow used here does not have
  one.
- **Minimum Drive scope.** `drive.file` means the app is *structurally* unable to
  read anything in a user's Drive that it did not create — not a promise, a
  property.
- **Secrets are not committed.** `.env` is gitignored; the repository contains no
  credentials.
- **Uploads stream to disk** rather than being buffered whole in memory.
- **The RLS policies themselves are correct** — they simply need the app to stop
  connecting as a role that bypasses them.

---

## Before you launch

In order:

1. **S-1** — verify Google ID tokens server-side; require auth on every route.
2. **S-2** — derive `user_id` from the verified token, then confirm isolation by
   *attempting* a cross-user fetch and getting a 404.
3. **S-4, S-3** — upload caps, then rate limits and quotas.
4. **S-6** — turn on the 7-day sweep.
5. **S-8** — TLS, HSTS, CSP, `Referrer-Policy`.
6. Then Google verification (needs a published privacy policy).

Items 1–2 are roughly a day's work and close the only findings that let one
person read another's data.

---

## Draft public security statement

Publish only what is true at the time. Once items 1–6 are done, this is accurate:

> **Your data**
>
> Book·Audio Suite is built so that we hold as little of your content as
> possible.
>
> - **We do not keep your files.** Uploads and generated audio are deleted from
>   our servers within 7 days. Anything you choose to keep is saved to *your*
>   Google Drive, in a folder you own and can delete at any time.
> - **We cannot see the rest of your Drive.** We request Google's `drive.file`
>   permission, which allows an app to access only the files it created. Your
>   other documents are not visible to us — that is enforced by Google, not by
>   our promise.
> - **We never see your Google password.** Sign-in happens on Google's own
>   pages. We receive a short-lived token, we never store it, and we do not
>   request offline access.
> - **Transcription runs on our servers, not a third party.** Your audio is not
>   sent to any external transcription service.
> - **Your work is yours.** We do not read, analyse, or train on your
>   transcripts, and we do not share them.
>
> Found a security problem? Email <your address>. We will acknowledge within
> 72 hours.

**Do not publish it yet.** Two sentences in it ("we do not keep your files",
"deleted within 7 days") are not true until S-6 ships, and the isolation implied
by the whole thing is not true until S-1 and S-2 do. Publishing a security claim
you have not implemented is worse than publishing nothing.

---

*Assessment date: 2026-08-26. Re-run after the fixes above; treat it as stale
once the auth model changes.*
