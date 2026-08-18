# Prompt — generate the planning docs for the quality pass

Paste everything below the line into a fresh session at the repo root.

---

We're starting the **quality pass** on the Book/Audio Suite. Both features (TTS
PDF→audiobook, STT audio→text) are live in the web app, but the map I just
produced lists real defects. Before any code, I want the planning documents.

## 0. Read these first (do not skip)

- `docs/Book-Audio-Suite-Map.pdf` — the current system map: user view, programmer
  view, load-bearing rules, and the defect list (D-1…D-9, F-1…F-3, P-1…P-3).
  This is the source of truth for WHAT IS.
- `CLAUDE.md` — how I work: mentor tone, architect-before-code, modular typed
  Python, verify each stage before moving on.
- `PRD_STT.md` — **use this as the format template.** The new PRD should match
  its structure, depth, and tone. Don't invent a different shape.
- `STT_INTEGRATION_TASK.md` — the template for the task brief (document 2).
- `TODO_PLAN.md` — roadmap context and the two-feature framing.
- The code the defects actually live in: `chapters.py`, `cleaner.py`, `app.py`,
  `pipeline.py`, `stt/transcribe.py`, `stt/pipeline.py`.

Confirm each defect against the real code before writing about it. If the map
and the code disagree, the code wins — say so.

## 1. Produce three documents

### A. `PRD_QUALITY.md` — the source of truth for WHAT to fix

Mirror `PRD_STT.md`'s structure: problem & goal · users & use cases · scope
(locked: in/out) · requirements (functional + non-functional) · architecture ·
module map · approach choices with reasoning · milestones · edge cases ·
success criteria.

It must cover these **verified** defects:

**Phase 1 — correctness (output is wrong today)**
- **D-1 · Chapter detection over-splits.** `chapters.py:57` — the fallback
  patterns `^\s*\d{1,3}\s*$` and `^\s*[IVXLCDM]{1,7}\.?\s*$` match bare page
  numbers, so a book without an embedded TOC gets ~one "chapter" per page, and
  captured titles absorb body text. Proposed fix: standalone-line guard (short
  line, sparse surroundings), title cleaning, and a confidence gate that falls
  back to fixed-size "Parts" when detection looks unreliable.
- **D-2 · Dashes flattened before narration.** `cleaner.py:27` maps `—` and `–`
  to `-`, so a pause-signalling dash is narrated as a compound word. Proposed
  fix: a `normalize_dashes()` step — em-dash → comma pause; digit-aware rule so
  `1914–1918` reads "1914 to 1918".
- **D-8 · OCR path never executed.** Tesseract isn't installed, so scanned PDFs
  fail at extraction and `textpdf.py` (which only triggers when OCR ran) is
  unverified end to end.

**Phase 2 — operability**
- **D-3 · Jobs live only in memory.** A restart erases every job while the files
  remain in `jobs/`. Proposed fix: persist `job.json` beside the outputs and
  rehydrate `_jobs` on startup.
- **D-5 · `jobs/` grows without bound** — currently 664 MB across 15 jobs; the
  upload, all intermediate chunks, and all outputs are kept forever. Note that
  `PipelineConfig.keep_chunks` already exists but the web layer never uses it.
- **D-4 · STT progress is coarse.** `stt.pipeline.transcribe_file()` exposes no
  progress hook, so the bar jumps 25% → 92% and sits still through the slowest
  part. Proposed fix: iterate faster-whisper's segment generator and report
  `segment.end / duration`. The TTS side already proves the pattern with its
  per-chunk `on_done` callback.
- **D-9 · No tests at all.**

**Phase 3 — features already in the specs**
- **F-1 · Live-mic transcription** — `stt/stream.py` exists and the CLI drives
  it; the web app doesn't. Needs a WebSocket endpoint + record button.
- **F-2 · Subtitle export (.srt/.vtt)** — timestamps are already in the JSON;
  this is a formatter plus a download button.
- **F-3 · More expressive voices** — confirmed by inspection:
  `edge_tts.Communicate` accepts only text/voice/rate/volume/pitch, with no
  `express-as` style, so emotional range can't be dialled in on the free
  endpoint. Kokoro/Piper drop in behind the existing engine seam.

**Phase 4 — public deployment** (P-1 auth/quotas, P-2 real queue + shared
storage, P-3 Edge-TTS at server scale). Scope this as "later", not now.

### B. `QUALITY_TASK.md` — the executable brief

Mirror `STT_INTEGRATION_TASK.md`: read-first list · objective · hard constraints
· build order with a verification gate after every step · acceptance criteria ·
edge cases · out of scope · finish steps. It must be self-contained enough that
an agent can work through it end to end without re-deriving context.

Hard constraints to carry over:
- Don't break either existing flow — TTS and STT must keep working unchanged.
- Local and free only; no paid APIs.
- Respect the load-bearing rules in the map (order-by-index, model loaded once,
  transcription serialized, `stt` imported lazily, filesystem-as-checkpoint).
- Keep the frontend consistent with the current UI.

### C. `TEST_PLAN.md`

The project has zero automated tests and every stage is hand-verified today.
Plan the cheap, high-value coverage first — the deterministic pure functions
that need no network: the chunker's sentence packing and hard-split, each
`cleaner.py` transform, `chapters.py` grouping, and the formatter's paragraph
rule. Then define regression fixtures for D-1 and D-2 specifically, so those
bugs can't come back. Name the framework and the run command.

## 2. Rules for these documents

- **No implementation code.** These are planning docs. Sketch signatures and
  data shapes where they clarify, not full functions.
- Every defect entry needs: **symptom · root cause (file:line) · proposed fix ·
  how we verify it's fixed.**
- Be opinionated about sequencing and say why — I'd rather fix wrong output
  before building new features, but argue me out of it if you disagree.
- Flag any scope question that would change the work materially, and give me a
  recommendation rather than an open list.

## 3. Finish

Summarize in a few lines what each document covers and what you want me to
decide before implementation starts. Don't write any application code yet.
