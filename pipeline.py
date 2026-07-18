"""
pipeline.py — Stage 5 orchestration. One call: PDF in, audiobook out.

Wires the stages together and organizes the output:

    out_dir/
      full/       <title>.m4b          whole book, with chapter marks
      chapters/   01_<title>.mp3 …      one file per detected chapter
      <title>_text.pdf                  only when OCR was used on the PDF

Design notes:
  * Chapters drive the shape. We extract pages, detect chapters (chapters.py:
    TOC first, headings fallback), group pages into chapter segments, then
    clean+chunk each segment. Chunks get sequential global indices, so each
    chapter maps to an exact chunk-index range — used both for the .m4b chapter
    marks and for slicing per-chapter mp3s.
  * Memory: the book's *text* is materialized (a few MB, and header stripping is
    a whole-document op); OCR pixmaps and audio are streamed. Python never holds
    the audio.
  * Resume: chunk audio is chunk_<index>.mp3, so the filesystem is the
    checkpoint — `resume=True` re-synthesizes only what's missing.
  * `progress(stage, fraction)` (optional) is called through the run so a web UI
    can show a live bar.

Needs: edge-tts + pymupdf (pip) and the FFmpeg binary (brew install ffmpeg).
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from chapters import detect_chapters, group_pages
from cleaner import clean_text
from extractor import PageSource, extract_pages
from stitch import Chapter, stitch
from textpdf import write_text_pdf
from tts import (
    DEFAULT_CONCURRENCY,
    DEFAULT_VOICE,
    MAX_CHUNK_CHARS,
    TextChunk,
    chunk_text,
    make_edge_engine,
    synthesize_book,
)

# stage -> overall progress fraction reached at that stage's end.
ProgressFn = Callable[[str, float], None]


# --- Config / result ---------------------------------------------------------

@dataclass
class PipelineConfig:
    voice: str = DEFAULT_VOICE
    rate: str | None = None             # e.g. '-12%' — slower, softer pauses
    pitch: str | None = None            # e.g. '-4Hz'
    enable_ocr: bool = True
    max_chars: int = MAX_CHUNK_CHARS
    concurrency: int = DEFAULT_CONCURRENCY
    title: str | None = None            # defaults to the PDF stem
    author: str | None = None
    full_format: str = ".mp3"           # whole-book file (.mp3 = universal; .m4b keeps chapter marks)
    resume: bool = False
    keep_chunks: bool = False
    progress: ProgressFn | None = None   # (stage, fraction 0..1)


@dataclass
class PipelineResult:
    out_dir: Path
    full_audio: Path
    chapter_files: list[Path]
    text_pdf: Path | None
    n_chunks: int
    n_chapters: int
    ocr_used: bool
    seconds: float
    stage_times: dict[str, float] = field(default_factory=dict)


# --- Helpers -----------------------------------------------------------------

def _safe_filename(name: str) -> str:
    """Make a chapter title safe for a filename."""
    s = re.sub(r"[^\w\- ]", "", name).strip().replace(" ", "-")
    return s[:60] or "chapter"


def _emit(cfg: PipelineConfig, stage: str, frac: float) -> None:
    if cfg.progress:
        cfg.progress(stage, max(0.0, min(1.0, frac)))


# --- Orchestration -----------------------------------------------------------

def run_pipeline(
    pdf_path: str | Path,
    out_dir: str | Path,
    config: PipelineConfig | None = None,
) -> PipelineResult:
    """
    Convert a PDF into a chaptered audiobook plus (for scanned PDFs) a text PDF.
    Fails loudly at boundaries (bad PDF, no text, failed chunks); degrades
    gracefully mid-run (one bad page/chunk won't kill the job).
    """
    cfg = config or PipelineConfig()
    pdf = Path(pdf_path)
    if not pdf.is_file():
        raise FileNotFoundError(f"No such PDF: {pdf}")

    out = Path(out_dir)
    full_dir = out / "full"
    chapters_dir = out / "chapters"
    work_dir = out / "_chunks"
    title = cfg.title or pdf.stem
    times: dict[str, float] = {}
    t0 = time.perf_counter()
    _mark = t0

    def _stage(name: str) -> None:
        nonlocal _mark
        now = time.perf_counter()
        times[name] = now - _mark
        _mark = now

    # Stage 1 — extract pages (materialized so we can segment by chapter).
    _emit(cfg, "extract", 0.02)
    print(f"[1/5] extracting {pdf.name} (ocr={'on' if cfg.enable_ocr else 'off'}) ...")
    pages = list(extract_pages(pdf, enable_ocr=cfg.enable_ocr))
    ocr_used = any(p.source == PageSource.OCR for p in pages)
    _stage("extract")
    print(f"      {len(pages)} pages, ocr_used={ocr_used} in {times['extract']:.1f}s")

    # Stage 2 — detect chapters, group pages, clean + chunk per chapter.
    print("[2/5] detecting chapters + cleaning ...")
    spans = detect_chapters(pdf, pages)
    segments = group_pages(pages, spans)

    all_chunks: list[TextChunk] = []
    chapter_ranges: list[tuple[str, int, int]] = []   # (title, start, end-exclusive)
    cleaned_parts: list[str] = []
    gi = 0
    for seg in segments:
        cleaned = clean_text(seg.text)
        if not cleaned.strip():
            continue
        cleaned_parts.append(cleaned)
        start = gi
        for ch in chunk_text(cleaned, max_chars=cfg.max_chars):
            all_chunks.append(TextChunk(gi, ch.text, ch.char_count))
            gi += 1
        if gi > start:
            chapter_ranges.append((seg.title, start, gi))

    if not all_chunks:
        raise RuntimeError(
            "No usable text after cleaning. Scanned PDF with OCR disabled, or an "
            "empty/corrupt file?"
        )
    _stage("chapters")
    print(f"      {len(chapter_ranges)} chapters, {len(all_chunks)} chunks "
          f"in {times['chapters']:.1f}s")
    _emit(cfg, "chapters", 0.18)

    # Stage 3 — synthesize all chunks (streams to work_dir). Progress per chunk.
    print(f"[3/5] synthesizing {len(all_chunks)} chunks (voice={cfg.voice}"
          f"{', resume' if cfg.resume else ''}) ...")
    engine = make_edge_engine(cfg.voice, rate=cfg.rate, pitch=cfg.pitch)
    done = 0

    def _on_done(_chunk) -> None:
        nonlocal done
        done += 1
        _emit(cfg, "tts", 0.18 + 0.62 * (done / len(all_chunks)))

    results = asyncio.run(synthesize_book(
        all_chunks, work_dir,
        engine=engine, concurrency=cfg.concurrency,
        skip_existing=cfg.resume, on_done=_on_done,
    ))
    failed = [r for r in results if not r.ok]
    if failed:
        idxs = ", ".join(f"{r.index:05d}" for r in failed[:10])
        more = f" (+{len(failed) - 10} more)" if len(failed) > 10 else ""
        raise RuntimeError(
            f"{len(failed)} chunk(s) failed to synthesize: {idxs}{more}. "
            f"Re-run with resume=True to fill the gaps (finished chunks kept "
            f"in {work_dir}/)."
        )
    chunk_paths = [r.path for r in results]  # index-sorted
    _stage("tts")
    print(f"      {len(results)} chunks ok in {times['tts']:.1f}s")

    # Stage 4 — stitch: whole book (with chapter marks) + per-chapter mp3s.
    print("[4/5] stitching full book + chapters ...")
    full_audio = full_dir / f"{_safe_filename(title)}{cfg.full_format}"
    marks = [Chapter(t, start) for (t, start, _end) in chapter_ranges]
    stitch(chunk_paths, full_audio, title=title, author=cfg.author, chapters=marks)

    chapter_files: list[Path] = []
    for n, (ct, start, end) in enumerate(chapter_ranges, start=1):
        cpath = chapters_dir / f"{n:02d}_{_safe_filename(ct)}.mp3"
        stitch(chunk_paths[start:end], cpath, title=ct, author=cfg.author)
        chapter_files.append(cpath)
    _stage("stitch")
    _emit(cfg, "stitch", 0.95)
    print(f"      full -> {full_audio}, {len(chapter_files)} chapters "
          f"in {times['stitch']:.1f}s")

    # Stage 5 — text PDF, only if the book was scanned (OCR used).
    text_pdf: Path | None = None
    if ocr_used:
        print("[5/5] OCR was used -> writing text PDF ...")
        text_pdf = write_text_pdf(
            "\n\n".join(cleaned_parts), out / f"{_safe_filename(title)}_text.pdf",
            title=title,
        )
        _stage("textpdf")
        print(f"      wrote {text_pdf}")
    else:
        print("[5/5] text-based PDF -> skipping text PDF")

    if not cfg.keep_chunks:
        shutil.rmtree(work_dir, ignore_errors=True)

    _emit(cfg, "done", 1.0)
    return PipelineResult(
        out_dir=out, full_audio=full_audio, chapter_files=chapter_files,
        text_pdf=text_pdf, n_chunks=len(all_chunks), n_chapters=len(chapter_ranges),
        ocr_used=ocr_used, seconds=time.perf_counter() - t0, stage_times=times,
    )


# --- CLI ---------------------------------------------------------------------

def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Convert a PDF into a chaptered audiobook.")
    ap.add_argument("pdf", help="input PDF")
    ap.add_argument("out_dir", help="output directory (full/, chapters/, text pdf)")
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--rate", help="narration speed, e.g. -12%% or +5%%")
    ap.add_argument("--pitch", help="voice pitch, e.g. -4Hz")
    ap.add_argument("--no-ocr", action="store_true", help="disable OCR fallback")
    ap.add_argument("--max-chars", type=int, default=MAX_CHUNK_CHARS)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--title")
    ap.add_argument("--author")
    ap.add_argument("--full-format", default=".mp3", choices=[".m4b", ".mp3"],
                    help="whole-book file format (default .mp3, universal; .m4b keeps chapter marks)")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--keep-chunks", action="store_true")
    args = ap.parse_args()

    cfg = PipelineConfig(
        voice=args.voice, rate=args.rate, pitch=args.pitch,
        enable_ocr=not args.no_ocr, max_chars=args.max_chars,
        concurrency=args.concurrency, title=args.title, author=args.author,
        full_format=args.full_format, resume=args.resume, keep_chunks=args.keep_chunks,
    )
    r = run_pipeline(args.pdf, args.out_dir, cfg)
    print("-" * 60)
    print(f"done in {r.seconds/60:.1f} min: {r.n_chapters} chapters, {r.n_chunks} chunks")
    print(f"  full:     {r.full_audio}")
    print(f"  chapters: {r.chapter_files[0].parent}/  ({len(r.chapter_files)} files)")
    if r.text_pdf:
        print(f"  text pdf: {r.text_pdf}")


if __name__ == "__main__":
    main()
