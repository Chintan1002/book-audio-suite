"""Stage 1 — Ingest. Decode/normalize any audio to 16 kHz mono PCM via FFmpeg.

Whisper wants 16 kHz mono float32. Rather than depend on many decoders, we shell
out to FFmpeg (already a project dependency for the TTS side) to handle every
format uniformly and return a numpy float32 array in [-1, 1].
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np

TARGET_SR = 16_000


def _require_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError(
            "FFmpeg not found on PATH. Install it (macOS: brew install ffmpeg)."
        )
    return exe


def load_audio(path: str | Path, sr: int = TARGET_SR) -> np.ndarray:
    """
    Decode `path` to a mono float32 numpy array at `sr` Hz.

    Raises RuntimeError on a missing/corrupt file or FFmpeg failure. Returns an
    empty array for zero-length audio (caller treats as empty transcript).
    """
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"No such audio file: {src}")

    ffmpeg = _require_ffmpeg()
    cmd = [
        ffmpeg, "-nostdin", "-threads", "0", "-i", str(src),
        "-f", "f32le", "-ac", "1", "-ar", str(sr), "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(
            f"FFmpeg failed to decode '{src}':\n{proc.stderr.decode(errors='ignore')[-500:]}"
        )
    return np.frombuffer(proc.stdout, dtype=np.float32)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m stt.ingest <audio-file>")
        raise SystemExit(1)
    audio = load_audio(sys.argv[1])
    secs = len(audio) / TARGET_SR
    print(f"decoded {len(audio):,} samples = {secs:.1f}s at {TARGET_SR} Hz mono")
