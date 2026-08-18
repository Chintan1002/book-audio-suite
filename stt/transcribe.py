"""Stage 2 — Transcribe. faster-whisper wrapper. The engine both paths share.

File path passes a whole decoded array; live path passes rolling windows. Either
way this returns TranscriptSegments with word-level timestamps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

import numpy as np

from .models import TranscriptResult, TranscriptSegment, Word

DEFAULT_MODEL = "base"          # tiny|base|small|medium|large-v3
DEFAULT_COMPUTE = "int8"        # int8 (CPU) | float16 (GPU) | int8_float16


class Transcriber:
    """
    Lazy-loaded faster-whisper model. Instantiate once and reuse — model load is
    the expensive step, so the streaming path keeps a single instance alive.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        device: str = "auto",
        compute_type: str = DEFAULT_COMPUTE,
    ) -> None:
        self.model_name = model
        self._device = device
        self._compute = compute_type
        self._model = None  # loaded on first use

    def _ensure_model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise RuntimeError(
                    "faster-whisper not installed. Run: pip install faster-whisper"
                ) from e
            self._model = WhisperModel(
                self.model_name, device=self._device, compute_type=self._compute
            )
        return self._model

    def transcribe(
        self,
        audio: Union[np.ndarray, str, Path],
        *,
        language: Optional[str] = None,
        word_timestamps: bool = True,
        vad_filter: bool = True,
    ) -> TranscriptResult:
        """
        Transcribe a decoded float32 array (preferred) or a path. `language=None`
        auto-detects. `vad_filter` drops silence for cleaner output.
        """
        model = self._ensure_model()
        segments_iter, info = model.transcribe(
            audio,
            language=language,
            word_timestamps=word_timestamps,
            vad_filter=vad_filter,
            beam_size=5,
        )

        segments: list[TranscriptSegment] = []
        for seg in segments_iter:
            words = [
                Word(w.word, w.start, w.end, getattr(w, "probability", None))
                for w in (seg.words or [])
            ] if word_timestamps else []
            segments.append(TranscriptSegment(seg.text, seg.start, seg.end, words))

        return TranscriptResult(
            segments=segments,
            language=getattr(info, "language", language or "unknown"),
            duration=getattr(info, "duration", 0.0),
            model=self.model_name,
        )


if __name__ == "__main__":
    import sys
    from .ingest import load_audio
    if len(sys.argv) < 2:
        print("usage: python -m stt.transcribe <audio-file> [model]")
        raise SystemExit(1)
    mdl = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODEL
    result = Transcriber(model=mdl).transcribe(load_audio(sys.argv[1]))
    print(f"[{result.language}] {result.duration:.1f}s, {len(result.segments)} segments")
    print(result.text[:800])
