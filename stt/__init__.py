"""stt — Speech-to-Text pipeline (Feature B).

Inverse of the TTS audiobook pipeline: audio (file or live mic) -> clean text
plus timestamped JSON. Local, zero-cost, no paid APIs.

Stages: ingest -> transcribe -> format -> output, with a streaming wrapper for
live mic. See PRD_STT.md for the full spec.
"""

from __future__ import annotations

__all__ = ["models", "ingest", "transcribe", "formatter", "output", "stream", "pipeline"]
