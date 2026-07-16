"""
chapters.py — chapter detection for the PDF -> Audiobook pipeline (M5).

Finds where chapters begin so the pipeline can (a) mark chapters in the .m4b and
(b) emit one mp3 per chapter. Two signals, best first:

  1. The PDF's embedded outline / table of contents (`doc.get_toc()`). Most
     published-book PDFs ship exact (title, page) per chapter — far more reliable
     than guessing from text, so we use it whenever it exists.
  2. Heading heuristics (fallback, for scanned/OCR books with no TOC): a page
     whose first lines look like "Chapter 3" / "CHAPTER ONE" / a lone roman
     numeral or number starts a new chapter.

Both signals are page-level and read BEFORE cleaning, which is why the pipeline
segments pages into chapters first, then cleans + chunks each chapter — making
the chapter->chunk mapping exact and free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import fitz  # PyMuPDF

from extractor import PageResult


# --- Data structures ---------------------------------------------------------

@dataclass
class ChapterSpan:
    """A detected chapter start. `start_page` is a 0-based page index."""
    title: str
    start_page: int


@dataclass
class ChapterSegment:
    """A chapter's title plus the pages that belong to it, in order."""
    title: str
    pages: list[PageResult]

    @property
    def text(self) -> str:
        """The chapter's raw text (pages joined), ready for cleaning."""
        return "\n\n".join(p.text for p in self.pages if p.text)


# --- Heading heuristics ------------------------------------------------------

# High-precision patterns for a chapter heading line. We favour precision over
# recall — a missed heading merges two chapters (mild); a false one splits a
# chapter mid-flow (worse).
_HEADING_PATTERNS = (
    re.compile(r"^\s*(chapter|part|book|section)\b", re.IGNORECASE),
    re.compile(r"^\s*[IVXLCDM]{1,7}\.?\s*$"),          # lone roman numeral
    re.compile(r"^\s*\d{1,3}\s*$"),                     # lone number
)

# How many leading non-empty lines of a page to inspect (a running header can
# occupy the first line, so we look a little deeper).
_HEADING_SCAN_LINES = 3


def _looks_like_heading(line: str) -> bool:
    return any(p.match(line) for p in _HEADING_PATTERNS)


def _leading_lines(text: str, n: int) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        s = raw.strip()
        if s:
            out.append(s)
            if len(out) >= n:
                break
    return out


def _clean_title(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()[:80]


def detect_from_headings(pages: Sequence[PageResult]) -> list[ChapterSpan]:
    """Fallback: scan the first few lines of each page for a chapter heading."""
    spans: list[ChapterSpan] = []
    for p in pages:
        if not p.text:
            continue
        for line in _leading_lines(p.text, _HEADING_SCAN_LINES):
            if _looks_like_heading(line):
                spans.append(ChapterSpan(_clean_title(line), p.index))
                break
    return spans


# --- Embedded TOC ------------------------------------------------------------

def detect_from_toc(pdf_path: str | Path) -> list[ChapterSpan]:
    """
    Read chapter starts from the PDF's embedded outline. Uses the top outline
    level (the chapters); nested sections are ignored. Returns [] if there's no
    usable TOC.
    """
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []
    try:
        toc = doc.get_toc(simple=True)  # [[level, title, page(1-based)], ...]
    finally:
        doc.close()
    if not toc:
        return []

    top_level = min(entry[0] for entry in toc)
    spans: list[ChapterSpan] = []
    for i, (level, title, page) in enumerate(toc):
        if level != top_level:
            continue
        spans.append(ChapterSpan(
            _clean_title(title) or f"Chapter {i + 1}",
            max(0, page - 1),  # TOC pages are 1-based
        ))
    return spans


# --- Public API --------------------------------------------------------------

def detect_chapters(pdf_path: str | Path, pages: Sequence[PageResult]) -> list[ChapterSpan]:
    """
    Detect chapter starts: embedded TOC first, heading heuristics as fallback.
    Returns spans sorted by page with duplicate start-pages collapsed. May be
    empty (no detectable structure) — the caller treats that as one chapter.
    """
    spans = detect_from_toc(pdf_path)
    if not spans:
        spans = detect_from_headings(pages)
    return _dedupe_sort(spans)


def _dedupe_sort(spans: list[ChapterSpan]) -> list[ChapterSpan]:
    spans = sorted(spans, key=lambda s: s.start_page)
    out: list[ChapterSpan] = []
    seen: set[int] = set()
    for s in spans:
        if s.start_page not in seen:      # first title wins for a given page
            out.append(s)
            seen.add(s.start_page)
    return out


def group_pages(pages: Sequence[PageResult], spans: Sequence[ChapterSpan]) -> list[ChapterSegment]:
    """
    Partition pages into chapter segments using detected start-pages. Pages
    before the first chapter become 'Front Matter'. With no spans, the whole
    book is a single segment.
    """
    pages = list(pages)
    if not pages:
        return []
    if not spans:
        return [ChapterSegment("Full Text", pages)]

    segments: list[ChapterSegment] = []
    last_page = pages[-1].index

    front = [p for p in pages if p.index < spans[0].start_page]
    if any(p.text for p in front):
        segments.append(ChapterSegment("Front Matter", front))

    for i, span in enumerate(spans):
        end = spans[i + 1].start_page if i + 1 < len(spans) else last_page + 1
        seg_pages = [p for p in pages if span.start_page <= p.index < end]
        if any(p.text for p in seg_pages):
            segments.append(ChapterSegment(span.title, seg_pages))
    return segments


# --- CLI / smoke test --------------------------------------------------------

if __name__ == "__main__":
    import sys
    from extractor import extract_pages

    if len(sys.argv) < 2:
        print("usage: python chapters.py <file.pdf>")
        raise SystemExit(1)

    pdf = sys.argv[1]
    pages = list(extract_pages(pdf))
    spans = detect_chapters(pdf, pages)
    used = "TOC" if detect_from_toc(pdf) else ("headings" if spans else "none")
    print(f"detected {len(spans)} chapter(s) via {used}:")
    for s in spans:
        print(f"  p{s.start_page:>4}  {s.title}")

    print("--- segments ---")
    for seg in group_pages(pages, spans):
        chars = sum(len(p.text) for p in seg.pages)
        print(f"  {seg.title:<40} {len(seg.pages)} pages, {chars:,} chars")
