"""
pipeline.py — Stage 5 of the PDF -> Audiobook pipeline. The capstone.

One command: PDF in, audiobook out. Wires the four independent stages together:

    extract_to_text (1) -> clean_text (2) -> chunk_text + synthesize_book (3)
        -> stitch (4)

Memory model (the honest version): the *text* of a whole book is a few MB, so
we extract+clean it in full — and we must, because header/footer stripping in
Stage 2 is a whole-document operation. The things that actually blow up memory
are streamed: OCR pixmaps (per-page in the extractor) and audio (each chunk goes
straight to its own file; stitch streams via FFmpeg). Python never holds the
audio.

Resume: chunk audio is written as chunk_<index>.mp3, so the filesystem *is* the
checkpoint. Re-run a crashed job with `resume=True` and only the missing chunks
are synthesized.

Usage:
    ./.venv/bin/python pipeline.py book.pdf book.m4b --voice en-US-GuyNeural
    ./.venv/bin/python pipeline.py book.pdf book.m4b --resume    # after a crash

Needs: edge-tts (pip) and the FFmpeg binary (brew install ffmpeg). See CLAUDE.md.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from cleaner import clean_text
from extractor import extract_to_text
from stitch import Chapter, stitch
from tts import (
    DEFAULT_CONCURRENCY,
    DEFAULT_VOICE,
    MAX_CHUNK_CHARS,
    chunk_text,
    synthesize_book,
)


# --- Config ------------------------------------------------------------------

@dataclass
class PipelineConfig:
    """Everything tunable about a run. Sensible defaults for the fast path."""
    voice: str = DEFAULT_VOICE
    enable_ocr: bool = True
    max_chars: int = MAX_CHUNK_CHARS
    concurrency: int = DEFAULT_CONCURRENCY
    title: str | None = None            # defaults to the PDF stem
    author: str | None = None
    chapters: list[Chapter] | None = None   # M5 seam; None = no chapter marks
    work_dir: Path | None = None        # chunk scratch; defaults to <out>_chunks
    resume: bool = False                # skip chunks already synthesized
    keep_chunks: bool = False           # keep the chunk dir after stitching


@dataclass
class PipelineResult:
    """What a run produced, for logging / the caller."""
    output: Path
    n_chunks: int
    text_chars: int
    seconds: float
    stage_times: dict[str, float] = field(default_factory=dict)


# --- Orchestration -----------------------------------------------------------

def run_pipeline(
    pdf_path: str | Path,
    out_path: str | Path,
    config: PipelineConfig | None = None,
) -> PipelineResult:
    """
    Run the whole pipeline: PDF -> audiobook. Fails loudly at boundaries (bad
    PDF, empty text, failed chunks) and degrades gracefully mid-run (one bad
    page/chunk doesn't kill the job).
    """
    cfg = config or PipelineConfig()
    pdf = Path(pdf_path)
    out = Path(out_path)
    if not pdf.is_file():
        raise FileNotFoundError(f"No such PDF: {pdf}")

    work_dir = Path(cfg.work_dir) if cfg.work_dir else out.with_name(out.stem + "_chunks")
    title = cfg.title or pdf.stem
    times: dict[str, float] = {}
    t0 = time.perf_counter()

    def _stage(name: str) -> float:
        now = time.perf_counter()
        times[name] = now - _stage.last
        _stage.last = now
        return times[name]
    _stage.last = t0

    # Stage 1 + 2 — extract then clean the full text (cheap, and header
    # stripping needs the whole document).
    print(f"[1/4] extracting text from {pdf.name} (ocr={'on' if cfg.enable_ocr else 'off'}) ...")
    raw = extract_to_text(pdf, enable_ocr=cfg.enable_ocr)
    print(f"      extracted {len(raw):,} chars in {_stage('extract'):.1f}s")

    print("[2/4] cleaning ...")
    text = clean_text(raw)
    if not text.strip():
        raise RuntimeError(
            "No usable text after cleaning. Is this a scanned PDF with OCR "
            "disabled, or an empty/corrupt file?"
        )
    print(f"      {len(text):,} chars of narration-ready text in {_stage('clean'):.1f}s")

    # Stage 3 — chunk, then synthesize (streams to files).
    chunks = list(chunk_text(text, max_chars=cfg.max_chars))
    print(f"[3/4] synthesizing {len(chunks)} chunks -> {work_dir}/ "
          f"(voice={cfg.voice}, concurrency={cfg.concurrency}"
          f"{', resume' if cfg.resume else ''}) ...")
    results = asyncio.run(synthesize_book(
        chunks, work_dir,
        voice=cfg.voice,
        concurrency=cfg.concurrency,
        skip_existing=cfg.resume,
    ))
    failed = [r for r in results if not r.ok]
    if failed:
        idxs = ", ".join(f"{r.index:05d}" for r in failed[:10])
        more = f" (+{len(failed) - 10} more)" if len(failed) > 10 else ""
        raise RuntimeError(
            f"{len(failed)} chunk(s) failed to synthesize: {idxs}{more}. "
            f"Fix connectivity and re-run with resume=True to fill the gaps "
            f"(finished chunks are kept in {work_dir}/)."
        )
    print(f"      {len(results)} chunks ok in {_stage('tts'):.1f}s")

    # Stage 4 — stitch the ordered chunk files into one audiobook.
    print(f"[4/4] stitching -> {out.name} ...")
    audio_files = [r.path for r in results]  # already index-sorted
    stitch(audio_files, out, title=title, author=cfg.author, chapters=cfg.chapters)
    print(f"      wrote {out} in {_stage('stitch'):.1f}s")

    if not cfg.keep_chunks:
        shutil.rmtree(work_dir, ignore_errors=True)

    total = time.perf_counter() - t0
    return PipelineResult(
        output=out, n_chunks=len(chunks), text_chars=len(text),
        seconds=total, stage_times=times,
    )


# --- CLI ---------------------------------------------------------------------

def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Convert a PDF into a narrated audiobook.")
    ap.add_argument("pdf", help="input PDF")
    ap.add_argument("out", help="output audiobook (.m4b or .mp3)")
    ap.add_argument("--voice", default=DEFAULT_VOICE, help=f"Edge-TTS voice (default {DEFAULT_VOICE})")
    ap.add_argument("--no-ocr", action="store_true", help="disable OCR fallback for scanned pages")
    ap.add_argument("--max-chars", type=int, default=MAX_CHUNK_CHARS, help="max characters per TTS chunk")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="concurrent TTS requests")
    ap.add_argument("--title")
    ap.add_argument("--author")
    ap.add_argument("--work-dir", help="where chunk audio goes (default <out>_chunks)")
    ap.add_argument("--resume", action="store_true", help="skip already-synthesized chunks")
    ap.add_argument("--keep-chunks", action="store_true", help="keep the chunk dir after stitching")
    args = ap.parse_args()

    cfg = PipelineConfig(
        voice=args.voice,
        enable_ocr=not args.no_ocr,
        max_chars=args.max_chars,
        concurrency=args.concurrency,
        title=args.title,
        author=args.author,
        work_dir=Path(args.work_dir) if args.work_dir else None,
        resume=args.resume,
        keep_chunks=args.keep_chunks,
    )

    result = run_pipeline(args.pdf, args.out, cfg)
    mins = result.seconds / 60
    stages = "  ".join(f"{k}={v:.1f}s" for k, v in result.stage_times.items())
    print("-" * 60)
    print(f"done: {result.output}  ({result.n_chunks} chunks, "
          f"{result.text_chars:,} chars, {mins:.1f} min wall)")
    print(f"stages: {stages}")


if __name__ == "__main__":
    main()
