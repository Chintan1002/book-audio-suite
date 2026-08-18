"""Shared data types for the STT pipeline. Plain dataclasses, no logic."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Word:
    """A single recognized word with its time span (seconds)."""
    text: str
    start: float
    end: float
    probability: Optional[float] = None


@dataclass
class TranscriptSegment:
    """A recognized segment (roughly a sentence/phrase) with timing."""
    text: str
    start: float
    end: float
    words: list[Word] = field(default_factory=list)


@dataclass
class TranscriptResult:
    """The full transcription: ordered segments plus metadata."""
    segments: list[TranscriptSegment]
    language: str
    duration: float
    model: str

    @property
    def text(self) -> str:
        """All segment text joined into one string."""
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())

    def to_dict(self) -> dict:
        """JSON-serializable form (for the timestamped .json output)."""
        return {
            "language": self.language,
            "duration": round(self.duration, 3),
            "model": self.model,
            "text": self.text,
            "segments": [
                {
                    "start": round(s.start, 3),
                    "end": round(s.end, 3),
                    "text": s.text.strip(),
                    "words": [
                        {"text": w.text, "start": round(w.start, 3),
                         "end": round(w.end, 3), "probability": w.probability}
                        for w in s.words
                    ],
                }
                for s in self.segments
            ],
        }
