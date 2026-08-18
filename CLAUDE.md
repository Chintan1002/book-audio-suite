# CLAUDE.md — Book/Audio Suite

Context for Claude Code working in this repo. Read `PROJECT_PLAN.md` for the
full module map and current status.

> **Two features, both live in the web app.** Feature A (TTS, PDF → audiobook)
> is built and in a quality pass. **Feature B (STT, audio → text)** is now
> **wired into the site** as a second tab: upload audio → `POST /transcribe` →
> poll → download `.txt`/`.md`/`.json`. Spec in `PRD_STT.md`, code in `stt/`
> (ingest, transcribe via faster-whisper, formatter, output, `pipeline`, `cli`;
> live-mic `stream` is NOT wired up yet — that's the next STT phase).
> Setup: `pip install -r requirements_stt.txt` + install FFmpeg.

## What this project is

A local, zero-cost pipeline converting PDFs (text-based **or** scanned) into
narrated audiobooks. No paid APIs anywhere. Five modules connected by plain
data: extract → clean → synthesize (TTS) → stitch → orchestrate.

## How I want you to work with me

- **Mentor, don't dump.** I'm a capable engineer learning this domain. Give the
  architectural breakdown first; don't hand me a 500-line script unprompted.
- **Build incrementally.** One module at a time. For a broad ask, propose the
  approach and data structures, then confirm before writing code.
- **Be opinionated.** Recommend the best production tool, don't just list
  options (e.g. PyMuPDF over pdfplumber for prose; Marker over raw Tesseract for
  hard scans; Kokoro/Edge-TTS over paid TTS).
- **Constructive reviews.** When I paste code, critique performance, modularity,
  and error handling with actionable nudges — don't silently rewrite it.
- **Verify bottlenecks before optimizing.** Confirm we understand the real
  constraint (LLM rate limits, TTS concurrency, VRAM) before adding complexity.

## Workflow for every module

1. **Scope** — state exactly which module we're building today.
2. **Architect** — approach, data structures, failure points.
3. **Implement** — modular, typed Python functions.
4. **Test & edge cases** — what happens on corrupt PDF, TTS timeout, weird
   Unicode. Don't move on until the current module runs.

## Code conventions

- Python 3.10+, full type hints, `from __future__ import annotations`.
- Small pure functions; each stage independently runnable (`__main__` smoke test).
- Generators / chunking for anything that scales with book length — never load a
  whole book into memory at once.
- Fail loudly at boundaries (file open), degrade gracefully mid-run (one bad
  page/chunk must not kill the job).
- No paid services. Keep heavy/optional deps (OCR, local LLM) lazy-imported and
  behind flags so the fast path stays light.

## Tech stack

- **Extract:** PyMuPDF (`fitz`); Tesseract via `pytesseract`; Marker/EasyOCR for
  difficult scans.
- **Clean:** regex pipeline; optional Ollama (Llama 3.2 3B) for OCR garble.
- **TTS:** Edge-TTS (default, async), Kokoro / Piper (offline).
- **Audio:** pydub / FFmpeg.

## Current status

- ✅ `extractor.py` — Stage 1, done.
- ✅ `cleaner.py` — Stage 2, seed version in place.
- ✅ `tts.py` — Stage 3, done. Chunker (sentence-boundary packing at ~3k chars)
  + async Edge-TTS synthesizer (concurrency cap, per-chunk retry/timeout,
  index-named `chunk_00000.mp3` files). Engine is swappable behind one
  coroutine seam for Kokoro/Piper later.
- ✅ `stitch.py` — Stage 4, done. FFmpeg-subprocess concat → `.mp3` (stream-copy)
  or `.m4b` (AAC re-encode), format from the output extension. Metadata +
  optional `chapters=` seam (real boundaries come at M5). Needs the FFmpeg
  binary (`brew install ffmpeg`).
- ✅ `pipeline.py` — Stage 5, done. Orchestrates 1→2→3→4 via `PipelineConfig`;
  segments pages into chapters, cleans+chunks each, then emits an output dir:
  `full/<title>.m4b` (with chapter marks), `chapters/NN_Title.mp3`, and a
  `<title>_text.pdf` when OCR was used. `--resume`, fail-loud on failed chunks,
  `progress(stage, frac)` hook for the web UI.
- ✅ `chapters.py` — chapter detection: embedded TOC first (`doc.get_toc()`),
  heading heuristics fallback; groups pages into chapter segments.
- ✅ `textpdf.py` — renders extracted text into a paginated PDF (PyMuPDF Story).
  Pipeline emits it only when OCR ran (scanned books).
- ✅ `app.py` + `index.html` — FastAPI web app. Async job model (upload →
  background job → poll → download), `/voices` dropdown from Edge-TTS, per-chunk
  progress. **Full local website works end-to-end.**

Note: fixed a latent bug in `cleaner.py` — `reflow_paragraphs` used a raw NUL
byte as its paragraph sentinel, which is illegal in Python source and made the
module unimportable (never caught because nothing imported it until Stage 5).
Now `"\x00"` as an escape; behavior identical.

## Running the pipeline

Edge-TTS needs a venv (Homebrew Python is externally-managed, PEP 668):

    python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
    # web app (local): open http://localhost:8000
    ./.venv/bin/uvicorn app:app --port 8000
    # or the CLI (out is now a DIRECTORY): full/ chapters/ + text pdf
    ./.venv/bin/python pipeline.py book.pdf out_dir/ --title "..." --author "..."
    ./.venv/bin/python pipeline.py book.pdf out_dir/ --resume    # after a crash
    # stages still independently runnable (tts.py / stitch.py / chapters.py / textpdf.py).

Note: on this machine `pip` and `python3` resolve to different interpreters —
always drive the pipeline through `./.venv/bin/python`. Also needs the FFmpeg
binary on PATH (`brew install ffmpeg`).

## Good next prompt (M5 — quality pass)

"Let's start M5. Add chapter detection in Stage 1/2 (heading heuristics ->
chapter boundaries) and feed it into stitch.py's ready `chapters=` seam so the
audiobook gets real chapter marks. Architect the boundary detection first."
