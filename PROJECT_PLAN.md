# PDF → Audiobook — Project Plan

A local, zero-cost pipeline that turns any PDF (text-based **or** scanned) into a
narrated audiobook. No paid APIs at any stage.

---

## Architecture at a glance

```
PDF ──► [1] Extract ──► raw text ──► [2] Clean ──► clean text
                                                       │
                                                       ▼
              audiobook.m4b ◄── [4] Stitch ◄── audio chunks ◄── [3] Synthesize (TTS)
```

The pipeline is a set of independent modules connected by plain data (strings /
lists of strings). Any stage can be swapped without touching the others — e.g.
switch Edge-TTS for Kokoro without changing the extractor.

---

## Module map

| # | Module | File | Input | Output | Status |
|---|--------|------|-------|--------|--------|
| 1 | Extract | `extractor.py` | PDF path | `Iterator[PageResult]` / text | ✅ Done |
| 2 | Clean | `cleaner.py` | raw text | clean text | ✅ Done (seed) |
| 3 | Synthesize | `tts.py` | clean text | audio chunks (.mp3/.wav) | ✅ Done |
| 4 | Stitch | `stitch.py` | audio chunks | one `.m4b`/`.mp3` | ✅ Done |
| 5 | Orchestrate | `pipeline.py` | PDF path | audiobook file | ✅ Done |

---

## Stage 1 — Extract  ✅

**Contract:** PDF path → per-page text in reading order.
**Approach:** PyMuPDF text layer first; per-page fallback to Tesseract OCR only
when a page is sparse *and* contains an image.
**Key decisions:** per-page (not per-file) detection; generator for memory;
never let one bad page kill the run.
**Deps:** `pymupdf`; optional `pytesseract pillow` + Tesseract binary.
**Done when:** `python extractor.py book.pdf out.txt` prints a sane
`pages by source` tally and produces readable text.

## Stage 2 — Clean  ✅ (seed)

**Contract:** raw extracted text → narration-ready text.
**Approach:** ordered regex pipeline. Each fix is a small pure function so the
order is explicit and testable.
**Handles:** de-hyphenation across line breaks, header/footer & page-number
stripping, whitespace collapse, Unicode/ligature normalization, paragraph
reflow.
**Optional upgrade:** route OCR-heavy text through a local LLM (Ollama, Llama
3.2 3B) to fix garble — kept behind a flag so text PDFs stay fast.
**Done when:** hyphenated words are rejoined, no stray page numbers, and
paragraphs read as continuous prose.

## Stage 3 — Synthesize (TTS)  ✅

**Contract:** clean text → ordered list of audio chunk files.
**Approach:** chunk text on sentence boundaries (~3k chars) so no single TTS
call is huge; synthesize each chunk; name chunks by index (`chunk_00000.mp3`)
so a plain sorted glob recovers reading order regardless of async completion
order.
**Engine choice (all free):**
- `edge-tts` — default, wired up. No GPU, natural voices, fast. Async.
- `kokoro` — fully offline, small local model. Use when privacy/offline matters.
- `piper` — ultralight offline fallback.
Engine sits behind one coroutine seam `async (text, out_path) -> None`, so
Kokoro/Piper drop in without touching the chunker or the concurrency harness.
**Bottleneck respected:** Edge-TTS is network + rate-limited → async with an
`asyncio.Semaphore` cap (default 4), per-chunk retry+backoff and a timeout so
one hung/failed chunk can't stall or kill the batch (returns `ok=False`).
Local models are VRAM/CPU-bound → they'd want a small sequential pool instead.
**Key decisions:** default voice `en-US-GuyNeural` (overridable); order lives in
the index/filename, never in completion order; a lone over-limit word degrades
gracefully rather than dropping text.
**Done when:** ✅ a multi-page text produces N ordered audio chunks that play.

## Stage 4 — Stitch  ✅

**Contract:** ordered audio chunks → one audiobook file.
**Approach:** drive system **FFmpeg** via subprocess (not pydub — pydub decodes
every chunk to in-memory PCM, gigabytes for a long book). Format is chosen from
the output extension: `.mp3` → concat demuxer `-c copy` (lossless stream-copy,
tiny gaps land on sentence pauses); `.m4b/.m4a/.mp4` → concat + AAC re-encode
(the real audiobook container — chapters, bookmarking, metadata — and gapless).
**Chapters:** ready `chapters=` seam. Pass `list[Chapter]` and FFmpeg writes
chapter marks (offsets computed from per-chunk `ffprobe` durations); leave None
for none. Real chapter→chunk boundaries need upstream detection (M5).
**Key decisions:** FFmpeg streams (Python never holds audio); fail loudly on
missing ffmpeg/ffprobe, no/empty chunks, or bad extension; concat-list paths
escaped for spaces/quotes.
**Done when:** ✅ verified — stitched duration is sample-exact vs. summed chunks;
m4b/mp3 containers + metadata + injected chapters all confirmed via ffprobe.

## Stage 5 — Orchestrate  ✅

**Contract:** one command: PDF in, audiobook out.
**Approach:** thin CLI wiring 1→2→3→4 via a `PipelineConfig` (voice, OCR on/off,
chunk size, concurrency, title/author, resume, keep-chunks).
**Memory model (honest):** the *text* of a whole book is a few MB, so we
extract+clean it in full — and must, since header stripping is a whole-document
op. The things that blow up memory are streamed: OCR pixmaps (per-page) and
audio (per-chunk to disk, stitched via FFmpeg). Python never holds the audio.
**Resume:** chunk audio is `chunk_<index>.mp3`, so the filesystem *is* the
checkpoint — `--resume` re-synthesizes only missing chunks. Fail-loud: if any
chunk fails TTS, stop before stitching (never silently drop audio).
**Done when:** ✅ `pipeline.py book.pdf book.m4b` yields a finished audiobook.
Verified end-to-end on a 6-page PDF: 5 chunks, page numbers stripped, resume
re-synthesized only the deleted chunk, final duration sample-exact vs. chunks.

---

## Build order & milestones

1. **M1 — Text out (done):** Stages 1–2 produce clean text from a real PDF.
2. **M2 — Sound out (done):** Stage 3 turns text into ordered audio chunks
   (Edge-TTS proven end-to-end).
3. **M3 — One file (done):** Stage 4 stitches chunks into a playable audiobook
   (`.m4b`/`.mp3`, metadata + chapter seam, verified via ffprobe).
4. **M4 — One command (done):** Stage 5 orchestrates the whole thing end-to-end
   (`pipeline.py book.pdf book.m4b`), with `--resume` for long books.
5. **M5 — Quality pass:** chapter detection (fills the `chapters=` seam), LLM
   cleanup for scanned books, voice/pace tuning, per-batch synth for huge books.

## Edge cases to keep on the radar

- Corrupt / password-protected PDF → fail loudly at open, not mid-run.
- Mixed book (text chapters + scanned plates) → per-page detection covers it.
- Blank pages → skip, don't OCR.
- TTS timeout on a long chunk → chunking + per-chunk retry.
- Weird Unicode / ligatures → normalized in Stage 2 before it reaches TTS.
- Very long book → stream pages; checkpoint so a crash doesn't restart from 0.

## Tech stack (open-source, free)

Extraction: **PyMuPDF**, Tesseract (Marker/EasyOCR for hard scans).
Cleaning: regex + optional **Ollama** local LLM.
TTS: **Edge-TTS** (default), **Kokoro**/**Piper** (offline).
Audio: **pydub** / **FFmpeg**.
Code: modular, typed Python; each stage independently runnable & testable.
