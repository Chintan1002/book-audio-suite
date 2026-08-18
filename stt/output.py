"""Stage 5 — Output. Write transcript (.txt/.md) and timestamped .json."""

from __future__ import annotations

import json
from pathlib import Path

from .models import TranscriptResult
from .formatter import format_transcript


def _ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def write_txt(result: TranscriptResult, path: str | Path) -> Path:
    p = Path(path)
    p.write_text(format_transcript(result), encoding="utf-8")
    return p


def write_md(result: TranscriptResult, path: str | Path, title: str = "Transcript") -> Path:
    p = Path(path)
    body = format_transcript(result)
    header = (
        f"# {title}\n\n"
        f"_Language: {result.language} · Duration: {_ts(result.duration)} · "
        f"Model: {result.model}_\n\n"
    )
    p.write_text(header + body + "\n", encoding="utf-8")
    return p


def write_json(result: TranscriptResult, path: str | Path) -> Path:
    p = Path(path)
    p.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def write_all(result: TranscriptResult, stem: str | Path) -> list[Path]:
    """Write <stem>.txt, <stem>.md, and <stem>.json. Returns the paths."""
    stem = Path(stem)
    return [
        write_txt(result, stem.with_suffix(".txt")),
        write_md(result, stem.with_suffix(".md")),
        write_json(result, stem.with_suffix(".json")),
    ]
