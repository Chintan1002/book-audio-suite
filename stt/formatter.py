"""Stage 3 — Format. Turn raw recognizer text into readable prose.

Whisper already emits punctuation, but segments arrive as a flat run. This adds
paragraph breaks on longer pauses and tidies spacing. Kept small and pure —
mirrors cleaner.py on the TTS side.
"""

from __future__ import annotations

import re

from .models import TranscriptResult

PARAGRAPH_GAP_SECONDS = 2.0  # silence between segments that starts a new paragraph


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)   # no space before punctuation
    return text


def format_transcript(result: TranscriptResult, paragraph_gap: float = PARAGRAPH_GAP_SECONDS) -> str:
    """Join segments into paragraphs, breaking where a real pause occurred."""
    paragraphs: list[str] = []
    current: list[str] = []
    prev_end: float | None = None

    for seg in result.segments:
        t = seg.text.strip()
        if not t:
            continue
        if prev_end is not None and (seg.start - prev_end) >= paragraph_gap and current:
            paragraphs.append(" ".join(current))
            current = []
        current.append(t)
        prev_end = seg.end

    if current:
        paragraphs.append(" ".join(current))

    return "\n\n".join(_tidy(p) for p in paragraphs)
