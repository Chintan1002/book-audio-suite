# PRD — Accounts & Transcript Library (multi-user)

Turns the Book/Audio Suite into a site many people can sign into. Schema lives
in `db/schema.sql`; the application framework in `db/repository.py`.

---

## 1. The scope decision, stated first

**Only STT (audio → text) has a library. TTS (PDF → audiobook) saves nothing.**

| Feature | Signed in? | What persists |
|---|---|---|
| Book → Audio (TTS) | yes, for fair use | **nothing** — no audiobook, no book text, no PDF |
| Audio → Text (STT) | yes | the transcript, permanently, in the user's library |

TTS stays a transient tool: upload a PDF, download the audiobook, done. Because
the product never offers a TTS library, nothing about it can feel lost — the
expectation is never set. That is what makes this scope coherent rather than a
compromise.

**What this decision buys.** A 330 MB audiobook was the entire storage problem.
Removing it removes the problem: a transcript is a few hundred KB of text, and
Postgres holds it comfortably.

## 2. The second decision: no file storage at all

With only transcripts to keep, **there are no stored files anywhere.** Every
download format is a pure function of two database columns:

```
transcripts.content   ──► .txt      (the formatted transcript)
                      ──► .md       (+ language / duration / model header)
transcripts.segments  ──► .json     (segments and word timings)
                      ──► .srt      (rendered from the same timings)
                      ──► .vtt
```

Rendering on demand instead of storing five files per transcript means:

- **no object storage, no signed URLs, no expiry sweeper, no byte quotas** — an
  entire subsystem never gets built;
- adding `.vtt` (or any future format) later works **retroactively for every
  transcript ever made**, with no backfill;
- nothing can drift out of sync with the text it came from.

The cost is a few milliseconds of CPU per download, against a transcript that is
already in memory. That is a good trade.

## 3. Problem & goal

Today the app has no concept of a user: jobs live in one in-memory dict, files
land in a shared folder, everything is lost on restart. Fine on localhost,
unusable in public.

**Goal:** each person signs in, transcribes what they like, and finds every
transcript they've ever made — searchable — whenever they come back. Nobody can
see anyone else's.

## 4. Scope

**In scope**
- Email/password + Google sign-in (Supabase Auth).
- A transcript library: title, date, language, duration, word count.
- Full-text search across one's own transcripts.
- Download any transcript as `.txt`, `.md`, `.json`, `.srt`, `.vtt`.
- Rename and delete a transcript.
- Saved preferences for both features (voice, speed, format, model, language).
- A fair-use quota measured in **transcribed minutes per month** — with no files
  stored, the scarce resource is CPU, not disk.

**Out of scope**
- Any persistence for TTS. Explicitly, permanently.
- Sharing and public links (schema leaves room; policies stay owner-only).
- Teams, roles, collaboration.
- Payments — the quota exists; monetizing it is a later decision.
- Live-mic streaming (F-1) and alternative engines (F-3) — unchanged.

## 5. Data model

Three tables. Every row carries `user_id` and is guarded by Row Level Security.

```
auth.users                  Supabase-managed identity
   │
   ├── profiles             1:1 · display name, remembered defaults, quota
   │
   ├── jobs                 in-flight processing state, BOTH features
   │                          (today's in-memory Job, made durable)
   │                          TTS rows are working state and get pruned
   │
   └── transcripts          THE LIBRARY · the only durable user content
                              content   the readable transcript
                              segments  timestamps -> every download format
```

**One database, not one per user.** Isolation comes from RLS: `auth.uid() =
user_id` on every table means a client query physically cannot return another
user's rows, even if the application has a bug. Database-per-user gives the same
guarantee while making migrations, backups and support far harder — a trade only
worth making for customers demanding physical separation.

**The caveat that matters:** the background worker connects as the service role,
which *bypasses* RLS by design. RLS protects against a careless client query,
not a careless server-side one. Every worker query must filter by `user_id`.

## 6. Application framework

Two seams. Neither pipeline changes.

```
app.py
 ├── auth dependency         verifies the Supabase JWT -> current_user_id
 │                           injected into every route; no route reads a
 │                           user_id from the request body
 │
 ├── Repository (Protocol)   ALL persistence goes through this
 │     ├── PostgresRepository   production (Supabase)
 │     └── MemoryRepository     tests + offline dev, same interface
 │
 └── stt/render.py           NEW · pure functions: TranscriptResult -> txt/md/
                             json/srt/vtt. No I/O, trivially testable, shared by
                             the download routes and the CLI.
```

**Why a repository rather than SQL in the routes:** the existing `Job` dataclass
already carries `kind/status/stage/progress/error`, so the frontend's polling
contract is unchanged — `GET /jobs/{id}` reads from the repository instead of a
dict. It also keeps the app runnable with no database at all, which is how the
tests stay fast and offline.

**This supersedes D-3** ("jobs lost on restart") from `PRD_QUALITY.md`, and
retires D-5's blanket retention in favour of `prune_stale_jobs()`.

### Route changes

| Route | Change |
|---|---|
| `POST /convert` | requires auth; records a TTS job; **saves no output** |
| `POST /transcribe` | requires auth; on success writes a `transcripts` row |
| `GET /jobs/{id}` | reads the repository; 404s on another user's job |
| `GET /library` | **new** — the caller's transcripts, paged, searchable |
| `GET /library/{id}` | **new** — one transcript with its text |
| `GET /library/{id}/download.{fmt}` | **new** — rendered on demand |
| `DELETE /library/{id}` | **new** — hard delete |
| `GET /me` | **new** — profile, preferences, usage against quota |

## 7. Requirements

**Functional**
- Sign up, sign in, sign out, password reset.
- Every transcription is attributed to the signed-in user and appears in their
  library immediately.
- A user sees only their own transcripts, in any view, ever.
- Search returns matches from the caller's transcripts only.
- Delete removes the row; nothing is retained.
- TTS downloads remain available for the life of the browser session's job and
  are not recorded in any library.
- Usage is visible against the monthly quota; exceeding it blocks new
  transcriptions with a message naming the reset date.

**Non-functional**
- Both pipelines keep working unchanged, with their load-bearing rules intact
  (order-by-index, model-loaded-once, serialized transcription, lazy `stt`
  import).
- The app must still run locally with no Supabase connection.
- Transcript `content` is never written to logs.
- Deleting an account cascades to its jobs and transcripts.

## 8. Milestones

1. **M1 — Repository seam.** Introduce the interface; move jobs off the
   in-memory dict. Still single-user, no auth. Fixes restart loss.
2. **M2 — Schema + Postgres.** Apply `db/schema.sql`; `PostgresRepository`.
3. **M3 — Auth.** Supabase Auth, sign-in UI, `user_id` on every write, RLS
   verified by an actual cross-user attempt.
4. **M4 — Library tab.** List, search, view, delete.
5. **M5 — Renderers.** `stt/render.py` plus the five download formats (this also
   closes F-2, subtitles, from `PRD_QUALITY.md`).
6. **M6 — Quota.** Monthly minutes, display and enforcement.

## 9. Edge cases

- A job running when the process dies → `fail_interrupted_jobs()` on startup
  marks it errored; it never shows as perpetually running.
- Transcription succeeds but the transcript insert fails → the job is marked
  errored with the text still recoverable from the job row's params; never
  report success without a saved transcript.
- Silent audio → a transcript row with empty content is still created, flagged
  in the UI as "no speech detected", so the user isn't left wondering.
- A very long recording → `segments` with word timings can reach a few MB of
  JSON; Postgres handles it, but cap the accepted upload duration rather than
  discovering the limit in production.
- Quota exceeded mid-job → let the running job finish; block the next.
- A user deletes a transcript whose job is still running → cancel the write.
- Two devices polling one job → reads are idempotent; safe.

## 10. Legal & privacy — settle before launch

Permanently storing transcripts creates a duty that transient local files did
not. **Transcripts are frequently sensitive** — interviews, meetings, medical
and legal conversations. Supabase encrypts at rest, but a real delete path, a
privacy policy, and restraint about logging are ours to build. Never log
`transcripts.content`.

Copyright exposure drops sharply under this scope: we no longer store the text
of uploaded books at all. The remaining case is a user transcribing copyrighted
audio, which terms of service should place on the uploader.

## 11. Success criteria

- Two users signed in simultaneously see entirely separate libraries — verified
  by attempting a cross-user fetch by id and getting a 404.
- A transcript made before a server restart is still in the library after it.
- All five download formats render correctly from one stored row, and the
  `.srt` syncs against the source audio in a real player.
- Deleting a transcript removes it everywhere.
- A TTS conversion leaves **no** trace in any library or table beyond a pruned
  job row.
- The app still runs locally with no Supabase connection.
