"""File path orchestration: ingest -> transcribe -> format -> output.

The batch entry point. Given an audio file, produce .txt/.md/.json next to it
(or at a chosen stem).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .ingest import load_audio
from .transcribe import Transcriber, DEFAULT_MODEL
from .models import TranscriptResult
from . import output as out


def transcribe_file(
    audio_path: str | Path,
    *,
    out_stem: Optional[str | Path] = None,
    model: str = DEFAULT_MODEL,
    language: Optional[str] = None,
    transcriber: Optional[Transcriber] = None,
) -> TranscriptResult:
    """
    Transcribe one audio file and write .txt/.md/.json. Pass a shared
    `transcriber` to avoid reloading the model across many files.
    """
    audio = load_audio(audio_path)
    tr = transcriber or Transcriber(model=model)
    result = tr.transcribe(audio, language=language)

    stem = Path(out_stem) if out_stem else Path(audio_path).with_suffix("")
    paths = out.write_all(result, stem)
    print(f"wrote: {', '.join(p.name for p in paths)}")
    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m stt.pipeline <audio-file> [model]")
        raise SystemExit(1)
    mdl = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODEL
    r = transcribe_file(sys.argv[1], model=mdl)
    print(f"[{r.language}] {r.duration:.1f}s, {len(r.segments)} segments")
