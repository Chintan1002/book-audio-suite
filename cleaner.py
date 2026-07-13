"""
cleaner.py — Stage 2 of the PDF -> Audiobook pipeline.

Turns raw extracted text (from extractor.py) into narration-ready prose.

Philosophy: an ORDERED pipeline of small, pure functions. Order matters
(de-hyphenate before reflowing paragraphs; normalize Unicode early), so each
step is its own function and `clean_text` just composes them. That makes every
transform independently testable.

No external deps — pure stdlib regex.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable, Iterable

# --- Individual transforms (each takes text, returns text) -------------------

# Map common ligatures / smart punctuation to plain ASCII the TTS reads cleanly.
_UNICODE_MAP = {
    "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff",
    "ﬃ": "ffi", "ﬄ": "ffl",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...",
    " ": " ",  # non-breaking space
}


def normalize_unicode(text: str) -> str:
    """NFKC-normalize and replace ligatures / smart quotes with plain forms."""
    text = unicodedata.normalize("NFKC", text)
    for bad, good in _UNICODE_MAP.items():
        text = text.replace(bad, good)
    return text


def dehyphenate(text: str) -> str:
    """
    Rejoin words split across a line break: 'clean-\\ning' -> 'cleaning'.
    Only joins when a lowercase letter precedes the hyphen and follows the
    break, so real hyphenated compounds at line end are mostly preserved.
    """
    return re.sub(r"([a-z])-\n\s*([a-z])", r"\1\2", text)


def strip_page_numbers(text: str) -> str:
    """Remove lines that are just a page number (optionally decorated)."""
    return re.sub(r"^\s*[-—]?\s*\d{1,4}\s*[-—]?\s*$", "", text, flags=re.MULTILINE)


def strip_running_headers(text: str, min_repeats: int = 3) -> str:
    """
    Drop short lines that repeat across many pages — classic running
    headers/footers (book title, chapter name). Heuristic: a short line
    (<60 chars, no sentence-ending punctuation) seen >= min_repeats times.
    """
    lines = text.split("\n")
    counts: dict[str, int] = {}
    for ln in lines:
        s = ln.strip()
        if 0 < len(s) < 60 and not re.search(r"[.!?]$", s):
            counts[s] = counts.get(s, 0) + 1
    repeated = {s for s, c in counts.items() if c >= min_repeats}
    return "\n".join(ln for ln in lines if ln.strip() not in repeated)


def reflow_paragraphs(text: str) -> str:
    """
    Collapse single line breaks within a paragraph into spaces, while keeping
    blank-line paragraph separations. Extraction wraps lines at page width; TTS
    wants flowing sentences.
    """
    # Protect paragraph breaks (2+ newlines) with a sentinel.
    text = re.sub(r"\n{2,}", "\x00", text)
    text = text.replace("\n", " ")
    text = text.replace("\x00", "\n\n")
    return text


def collapse_whitespace(text: str) -> str:
    """Squeeze runs of spaces/tabs and trim each paragraph."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


# --- Pipeline composition ----------------------------------------------------

# Order is deliberate. Change with care.
DEFAULT_PIPELINE: tuple[Callable[[str], str], ...] = (
    normalize_unicode,
    dehyphenate,
    strip_page_numbers,
    strip_running_headers,
    reflow_paragraphs,
    collapse_whitespace,
)


def clean_text(
    text: str,
    steps: Iterable[Callable[[str], str]] = DEFAULT_PIPELINE,
) -> str:
    """Run the ordered cleaning pipeline over raw text."""
    for step in steps:
        text = step(text)
    return text


# --- Optional: LLM cleanup for OCR-heavy text (stub, off by default) ---------

def llm_cleanup(text: str, model: str = "llama3.2:3b") -> str:
    """
    Fix OCR garble with a local LLM via Ollama. Kept optional/lazy so text-based
    PDFs never pay this cost. Fill in when we tackle scanned-book quality (M5).
    """
    raise NotImplementedError(
        "LLM cleanup is a Milestone 5 upgrade. Wire up Ollama here when needed."
    )


# --- CLI / smoke test --------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path

    if len(sys.argv) < 2:
        print("usage: python cleaner.py <raw.txt> [clean.txt]")
        raise SystemExit(1)

    raw = Path(sys.argv[1]).read_text(encoding="utf-8")
    cleaned = clean_text(raw)

    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(cleaned, encoding="utf-8")
        print(f"wrote {sys.argv[2]}  ({len(raw)} -> {len(cleaned)} chars)")
    else:
        print(cleaned[:1000])
