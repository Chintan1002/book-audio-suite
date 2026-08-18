# TASK BRIEF — Quality pass on the Book/Audio Suite

You are picking up a real, working project. Work autonomously through this brief
end to end. Follow the working style in `CLAUDE.md`: mentor tone,
architect-before-code, modular typed Python, verify each stage before moving on.

---

## 0. Read these first (do not skip)

- `PRD_QUALITY.md` — the spec for this pass. Source of truth for WHAT to fix and
  why. Every defect has a verified root cause with file references.
- `TEST_PLAN.md` — the coverage this pass must land.
- `docs/Book-Audio-Suite-Map.pdf` — how the system fits together: user view,
  programmer view, and the load-bearing rules you must not break.
- `CLAUDE.md`, `PROJECT_PLAN.md`, `TODO_PLAN.md` — project context.
- The code you'll touch: `chapters.py`, `cleaner.py`, `app.py`,
  `stt/transcribe.py`, `stt/pipeline.py`, `pipeline.py`, `extractor.py`.

Then confirm each defect against the real code before fixing it. If the PRD and
the code disagree, **the code wins** — say so and adjust.

## 1. Objective

Make the output correct on inputs we already claim to support, make operation
survive a restart without filling the disk, and leave behind tests that stop the
two correctness bugs from returning. No new features beyond `.srt`/`.vtt`
export.

## 2. Hard constraints

- **Do not break either existing flow.** TTS (PDF→audiobook) and STT
  (audio→text) must keep working exactly as they do now. Both have a real
  end-to-end path through the web app; re-verify both before you finish.
- **Respect the load-bearing rules** (system map, §B.3). In particular: order
  comes from the chunk index and never from completion order; the whisper model
  is constructed once and reused; transcription stays serialized behind
  `_stt_run_lock`; `stt` stays lazily imported inside the worker; the filesystem
  remains the resume checkpoint.
- **Local and free only.** No paid APIs, no cloud services.
- **Stay inside existing module boundaries.** This is a quality pass, not a
  rewrite. The one new module is `stt/subtitles.py`.
- **Tests must run offline** — no network, no model download, no FFmpeg
  dependency for the pure-function tests.
- **Keep the frontend consistent** with the current UI: same tokens, same
  progress/error states, same download-card pattern, both themes.

## 3. Build order (verify each step before starting the next)

### Step 1 — D-1, chapter over-split  `chapters.py`
Architect the detector changes with me before writing them. Implement the
standalone-line guard, the page-number rejection rule, title cleaning, and the
confidence gate with a fixed-size "Parts" fallback.
**Verify:** build the repro (8 pages, page numbers in the *header*, two real
`Chapter N` headings, no TOC) — it must yield **2** chapters, not 8. The
existing `toc_book.pdf` must still yield its exact TOC chapters. Run
`python chapters.py <pdf>` on both.

### Step 2 — D-2, dash normalization  `cleaner.py`
Remove `—`/`–` from `_UNICODE_MAP`; add `normalize_dashes()` to
`DEFAULT_PIPELINE` before `reflow_paragraphs`.
**Verify:** unit tests for em-dash → pause and `1914–1918` → "1914 to 1918", and
confirm a plain ASCII hyphen is untouched. Synthesize one short clip and listen
for the pause.

### Step 3 — D-9 + `TEST_PLAN.md`, the safety net
Stand up `pytest` and write the pure-function tests, including frozen fixtures
that fail if D-1 or D-2 regresses.
**Verify:** `pytest` green, offline, in under ~10 seconds.

### Step 4 — D-8, prove the OCR path
Install Tesseract (`brew install tesseract`) plus `pytesseract`/`pillow`. Run one
genuinely scanned PDF end to end.
**Verify:** an audiobook *and* a text PDF are produced, and the text PDF has a
readable text layer. Add a clear UI error when Tesseract is missing, mirroring
the `/stt/status` pattern.

### Step 5 — D-3, job persistence  `app.py`
Write `jobs/<id>/job.json` on terminal states; rehydrate `_jobs` on startup.
Mark jobs that were mid-run as `error` ("interrupted by a restart").
**Verify:** complete a job, restart the server, download it successfully.

### Step 6 — D-5, retention  `app.py`
Sweep job directories older than a configurable TTL (default 7 days) on startup
and after each job. Never delete a non-terminal job.
**Verify:** a seeded old job directory is removed; a fresh one and a running one
are not.

### Step 7 — D-4, real STT progress  `stt/transcribe.py`, `stt/pipeline.py`, `app.py`
Add an optional `on_segment` callback, thread it through `transcribe_file()`, and
report `segment.end / duration` into the 25→92% band.
**Verify:** a multi-minute audio file shows at least five distinct intermediate
progress values in the UI.

### Step 8 — F-2, subtitles  `stt/subtitles.py`, `app.py`, `index.html`
`write_srt` / `write_vtt` from the existing `TranscriptResult`, two routes, two
download cards.
**Verify:** the `.srt` loads in a real player against the source audio and the
captions line up.

## 4. Acceptance criteria

- The D-1 repro yields 2 chapters; `toc_book.pdf` still yields its TOC chapters.
- An em-dash is narrated as a pause; `1914–1918` reads "1914 to 1918".
- A scanned PDF produces an audiobook and a readable text PDF.
- A finished job is still downloadable after a server restart.
- Old job directories are reclaimed automatically; running jobs never are.
- STT progress advances continuously through a long transcription.
- `.srt` and `.vtt` download and sync correctly.
- `pytest` passes offline and fails if D-1 or D-2 is reintroduced.
- **Both original flows still work end to end through the browser.**

## 5. Edge cases to handle (PRD section 9)

Books whose chapters genuinely are one page each; books with both a TOC and
headings; a TOC pointing outside the document; dashes inside URLs or code;
corrupt/half-written job directories at startup; a retention sweep racing a
running job; empty or single-segment audio for subtitle export.

## 6. Out of scope

Live-microphone streaming (F-1), alternative TTS engines (F-3), and everything
under public deployment (P-1…P-3: auth, quotas, real queue, shared storage).
Also: diarization, translation, summarization. Do not start these.

## 7. Finish

Run the app, do one real end-to-end conversion **and** one real transcription
through the browser, and report:
- files touched and why,
- each defect with before/after evidence,
- anything you found that this brief didn't anticipate,
- anything you deliberately left undone.

Then update `TODO_PLAN.md` and `docs/Book-Audio-Suite-Map.pdf`'s source
(`docs/`) to reflect the new state. Do not commit unless I ask.
