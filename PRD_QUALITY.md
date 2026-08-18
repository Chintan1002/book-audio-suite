# PRD — Quality Pass (Feature A + B hardening)

Both features of the Book/Audio Suite are live in the web app. This document
covers the pass that makes their output *correct* and their operation
*sustainable*. Local, zero-cost, no paid APIs — unchanged.

Companion documents: `QUALITY_TASK.md` (the executable brief) and
`TEST_PLAN.md` (first automated coverage). System context:
`docs/Book-Audio-Suite-Map.pdf`.

---

## 1. Problem & goal

The suite works end to end, and on a real book it produces a genuinely good
audiobook. But three classes of problem are now visible:

1. **Wrong output on a specific class of input.** A PDF without an embedded
   table of contents, whose page numbers sit in the *header*, is sliced into one
   "chapter" per page. Em-dashes are narrated as compound words.
2. **Unsustainable operation.** Finished jobs are never cleaned up (664 MB
   today) and vanish from the UI on restart even though their files survive.
3. **No safety net.** Zero automated tests, so every fix risks a silent
   regression and every past fix can quietly come back.

**Goal:** correct output on the inputs we already claim to support, operation
that survives a restart and doesn't fill the disk, and enough test coverage that
the two correctness bugs can never return.

## 2. Users & use cases

Unchanged from the existing features; this pass serves the same people:

- Someone converting a book they own into an audiobook they can listen to.
- Someone turning a lecture, interview, or voice memo into readable text.

What changes is that the first user stops getting a scrambled chapter list on
certain books, and both stop losing their downloads when the server restarts.

## 3. Scope (locked)

**In scope**
- D-1 chapter over-split · D-2 dash flattening · D-8 OCR path verification
- D-3 job persistence · D-5 storage retention · D-4 real STT progress
- D-9 automated tests for the pure, deterministic stages
- F-2 subtitle export (`.srt`/`.vtt`) — cheap, and the timestamps already exist

**Out of scope (deliberately deferred)**
- F-1 live-microphone streaming — a feature phase, not a quality pass. It needs
  a WebSocket transport and a recording UI; folding it in here would double the
  surface area and delay the correctness fixes.
- F-3 alternative TTS engines (Kokoro/Piper) — the engine seam already exists;
  this is a quality *upgrade*, not a defect.
- P-1…P-3 public deployment (auth, quotas, real queue, shared storage). None of
  it matters until the app leaves localhost.
- Speaker diarization, translation, summarization.

## 4. Requirements

**Functional**
- A PDF with no embedded TOC must produce chapters that correspond to real
  chapter headings, or — when detection is not confident — a small number of
  evenly-sized "Parts", never one chapter per page.
- A chapter title must never contain body text or be a bare page number.
- An em-dash or en-dash must be narrated as a pause, not as a word join. A
  numeric range (`1914–1918`) must read naturally.
- A scanned PDF must complete: OCR extraction, audiobook, and the text PDF.
- A job's downloads must still work after the server restarts.
- Storage must not grow without bound; the operator must be able to reclaim
  space without hand-deleting folders.
- Transcription progress must advance continuously, not sit at one value.
- Transcripts must be exportable as `.srt` and `.vtt`.

**Non-functional**
- No regression in either existing flow. The load-bearing rules in the system
  map (order-by-index, model-loaded-once, serialized transcription, lazy `stt`
  import, filesystem-as-checkpoint) must all still hold.
- Fixes stay in the existing module boundaries; no new architecture.
- Tests must run offline, with no network and no model download.
- Still local, still free.

## 5. Defects — verified against the code

Each entry: symptom · root cause · fix · verification.

### D-1 · Chapter detection collapses to one chapter per page  `[Phase 1]`

**Symptom.** An 8-page test book with two real `Chapter N` headings produced
**eight** chapters, titled `1`, `2`, `3`… — the page numbers. The real headings
were never detected.

**Root cause.** `chapters.py`. `detect_from_headings()` scans the first three
non-empty lines of each page (`_HEADING_SCAN_LINES = 3`) and matches against
`_HEADING_PATTERNS` (`chapters.py:57`), which includes a bare number
`^\s*\d{1,3}\s*$` and a lone roman numeral. When a page number is rendered at
the *top* of the page it lands in those leading lines, matches, and the loop
`break`s — so it both invents a chapter and masks the genuine heading below it.

**Scope of impact — narrower than it first appears.** This is the *fallback*
path only. Books with an embedded TOC are unaffected: a real 19-chapter book
(`Title Page`, `Dedication`, … `About the Publisher`) came through perfectly via
`doc.get_toc()`. The bug bites PDFs with **no TOC and header page numbers**.

**Fix.**
1. **Standalone-line guard** — a heading candidate must be a short line that
   stands alone: no sentence-ending punctuation, under ~60 chars, and not
   immediately followed by prose on the same visual line.
2. **Reject page-number-shaped lines** — a bare number that equals (or is within
   ±1 of) the page's own index is a page number, not a chapter. This single rule
   kills the observed failure.
3. **Title cleaning** — strip trailing body text; if the cleaned title is empty
   or numeric, fall back to `Chapter N`.
4. **Confidence gate** — if detected chapters exceed a sane ratio (e.g. more
   than one per two pages, or the median chapter is a single page), discard the
   heuristic result and fall back to fixed-size `Part 1…N` segments.

**Verification.** The repro PDF (8 pages, header page numbers, two real
headings) yields 2 chapters. The TOC book still yields its exact TOC chapters. A
frozen fixture covers both in `TEST_PLAN.md`.

### D-2 · Dashes are narrated as word joins  `[Phase 1]`

**Symptom.** "precisely—having" is spoken as one hyphenated compound instead of
with the pause the dash represents.

**Root cause.** `cleaner.py:27` — `_UNICODE_MAP` folds `—` and `–` to `-`, and
that runs before the text ever reaches the chunker.

**Fix.** Remove both dashes from `_UNICODE_MAP` and add a `normalize_dashes()`
step to `DEFAULT_PIPELINE`, ordered before `reflow_paragraphs`:
- `—` (and a spaced `–`) → `", "` so the recognizer/synthesizer pauses.
- Digit-flanked `–` → `" to "`, so `1914–1918` reads "1914 to 1918".

**Verification.** Unit tests over the transform; plus one synthesized clip to
confirm the pause is audible.

### D-8 · The OCR path has never run  `[Phase 1]`

**Symptom.** Unknown — that is the problem. Scanned PDFs are a headline feature
and no scanned PDF has ever been through the pipeline.

**Root cause.** Tesseract is not installed, so `extractor._ocr_page()` raises
before doing anything. `textpdf.write_text_pdf()` only triggers when
`ocr_used == True`, so it is unverified in the real flow (its own smoke test
passes).

**Fix.** Install Tesseract and `pytesseract`/`pillow`, then run one genuinely
scanned book end to end and fix what falls out. Document the dependency in
`requirements.txt` comments and surface a clear UI error when it's missing —
matching the pattern `/stt/status` already uses for the STT dependencies.

**Verification.** A scanned PDF produces an audiobook *and* a text PDF whose
text layer is readable.

### D-3 · Jobs are lost on restart  `[Phase 2]`

**Symptom.** Observed directly during testing: a completed audiobook returned
404 after the server restarted, though every file was still on disk.

**Root cause.** `app.py` — `_jobs: dict[str, Job]` is process memory. Nothing is
persisted, and nothing is rehydrated at startup.

**Fix.** Write `jobs/<id>/job.json` whenever a job reaches a terminal state, and
scan `jobs/` on startup to rebuild `_jobs`. Jobs that were mid-run when the
process died are marked `error` with an explanatory message ("interrupted by a
restart") rather than being resumed — the TTS pipeline already has `--resume`
for that, and wiring it to the web layer is a separate decision.

**Verification.** Complete a job, restart the server, download it successfully.

### D-5 · Storage grows without bound  `[Phase 2]`

**Symptom.** `jobs/` is 664 MB across 15 jobs.

**Root cause — corrected from the initial read.** Intermediate chunks are *not*
the problem: `run_pipeline()` removes `_chunks/` after stitching and zero
leftover chunk directories exist. The actual causes are:
1. **No retention policy.** Completed jobs live forever.
2. **Duplicate work is stored twice over.** The same book was converted twice,
   consuming 330 MB + 276 MB.
3. **Each job stores its audio twice by design** — 192 MB `full/` plus 137 MB
   `chapters/` are the same narration in two layouts — plus the original upload.

The third is intentional (users want both), so the fix is retention, not
restructuring.

**Fix.** A retention sweep on startup and after each job: delete job directories
older than N days (configurable, default 7), and expose the total in
`/stt/status`-style diagnostics. Keep the design that stores full + chapters.

**Verification.** A seeded old job directory is removed on startup; a fresh one
is not.

### D-4 · Transcription progress is coarse  `[Phase 2]`

**Symptom.** The STT bar jumps to 25% and sits there through the entire
transcription — the slowest part of the job — then jumps to 92%.

**Root cause.** `stt/pipeline.py::transcribe_file()` exposes no progress hook,
and `app.py::_run_stt_job()` can therefore only bracket the call with coarse
stages.

**Fix.** faster-whisper returns a *generator* of segments; work happens as it is
consumed. Add an optional `on_segment` callback to
`Transcriber.transcribe()` and thread it through `transcribe_file()`, reporting
`segment.end / info.duration`. `app.py` maps that onto the 25→92% band. This
mirrors the TTS side's per-chunk `on_done` callback exactly.

**Verification.** A multi-minute file shows at least five distinct intermediate
progress values.

### D-9 · No automated tests  `[Phase 2]`

**Symptom.** Every stage is hand-verified; nothing prevents regression.

**Fix.** See `TEST_PLAN.md`. Priority is the deterministic, offline-safe pure
functions plus frozen regression fixtures for D-1 and D-2.

### F-2 · Subtitle export  `[Phase 2, opportunistic]`

Timestamps are already written to the transcript JSON. Add
`stt/subtitles.py` (`write_srt`, `write_vtt`) fed by the existing
`TranscriptResult`, two download routes, and two buttons. Smallest change on
this list with visible user value.

## 6. Architecture of the fixes

No new architecture. Every change lands inside an existing module boundary:

```
chapters.py    D-1  guard + title cleaning + confidence gate (pure, testable)
cleaner.py     D-2  new normalize_dashes() step in DEFAULT_PIPELINE
extractor.py   D-8  no code change expected; dependency + verification
stt/transcribe.py
stt/pipeline.py D-4 optional on_segment callback threaded through
app.py         D-3  persist/rehydrate jobs
               D-5  retention sweep
               D-4  map segment progress into the reported band
               F-2  two download routes
stt/subtitles.py    NEW — .srt/.vtt writers
tests/              NEW — see TEST_PLAN.md
```

## 7. Approach choices, and why

- **Confidence gate over cleverer heuristics (D-1).** No heuristic will parse
  every book. The valuable property is *never producing nonsense*: when the
  signal is weak, fixed-size Parts are honest and useful, while 300 one-page
  chapters are neither. Prefer a conservative detector plus a visible fallback.
- **A page-number rule, specifically.** Comparing a bare-number candidate to the
  page's own index is a targeted, low-risk rule that kills the observed failure
  without weakening genuine `Chapter N` detection.
- **Comma, not `<break>`, for dashes (D-2).** Edge-TTS accepts no SSML break
  tags (verified: `Communicate` takes only text/voice/rate/volume/pitch), so
  punctuation is the only pause lever available.
- **JSON sidecar over a database (D-3).** One file per job beside its outputs
  keeps the filesystem the single source of truth, matching the existing
  filesystem-as-checkpoint design. A database is the right answer only when
  deployment (P-2) arrives.
- **Retention, not deduplication (D-5).** Content-hashing uploads to avoid
  re-conversion is a real idea, but it adds a lookup layer for a problem a TTL
  solves. Revisit if repeat conversions become common.

## 8. Milestones

1. **M1 — Correct output.** D-1, D-2 fixed with fixtures proving both. The
   pipeline produces sane chapters and natural pauses.
2. **M2 — Scanned books proven.** D-8: Tesseract installed, one real scanned
   book through extraction → audiobook → text PDF.
3. **M3 — Survives operation.** D-3 persistence, D-5 retention.
4. **M4 — Honest progress + coverage.** D-4 real STT progress, D-9 test suite
   green in CI-less local runs.
5. **M5 — Opportunistic.** F-2 subtitles.

## 9. Edge cases

- A book whose real chapters genuinely are one page each (poetry, aphorisms) —
  the confidence gate must not mangle it; prefer erring toward fewer splits.
- A book with both a TOC *and* headings — TOC wins, unchanged.
- A TOC pointing at pages outside the document — already guarded; keep it.
- Dashes inside code, URLs, or already-hyphenated compounds — `normalize_dashes`
  must not touch a plain ASCII hyphen.
- A job directory that is corrupt or half-written when persistence scans it —
  skip it, don't crash startup.
- A retention sweep racing a running job — never delete a job that isn't in a
  terminal state.
- Zero-length or single-segment audio for subtitle export — emit a valid, empty
  file rather than failing.

## 10. Success criteria

- The D-1 repro PDF yields 2 chapters, not 8; the real TOC book still yields its
  19 correct chapters.
- An em-dash sentence is narrated with an audible pause; `1914–1918` reads
  "1914 to 1918".
- A scanned PDF produces both an audiobook and a readable text PDF.
- A completed job downloads successfully after a server restart.
- `jobs/` self-limits; old jobs disappear on schedule.
- The STT bar moves continuously through a long transcription.
- `pytest` runs offline and green, and fails if D-1 or D-2 is reintroduced.
