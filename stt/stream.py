"""Stage 4 — Live mic streaming (near-real-time).

Wraps the SAME Transcriber as the file path. Captures mic audio into a rolling
buffer and re-transcribes a trailing window on a fixed cadence, emitting partial
text that firms up as more audio arrives. On stop, does a final full-buffer pass.

This is a SCAFFOLD with a working structure and clearly marked decisions to tune
in Claude Code (window length, stride, silence/endpointing). Streaming Whisper is
inherently approximate — see PRD_STT.md, section 7.

Deps: pip install sounddevice numpy
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from .transcribe import Transcriber, DEFAULT_MODEL

SAMPLE_RATE = 16_000
WINDOW_SECONDS = 8.0     # how much trailing audio to transcribe each pass
STRIDE_SECONDS = 2.0     # how often to run a pass (latency vs. CPU)


@dataclass
class PartialResult:
    text: str
    is_final: bool


class MicStream:
    """
    Live transcription from the default microphone.

    Usage:
        stream = MicStream(on_update=lambda p: print(p.text))
        stream.start()   # blocks until Ctrl-C / stop(); calls on_update as text arrives

    on_update receives PartialResult objects: rolling partials (is_final=False)
    then one final (is_final=True) at stop.
    """

    def __init__(
        self,
        on_update: Callable[[PartialResult], None],
        *,
        model: str = DEFAULT_MODEL,
        window_seconds: float = WINDOW_SECONDS,
        stride_seconds: float = STRIDE_SECONDS,
        transcriber: Optional[Transcriber] = None,
    ) -> None:
        self.on_update = on_update
        self.window = int(window_seconds * SAMPLE_RATE)
        self.stride = stride_seconds
        self.transcriber = transcriber or Transcriber(model=model)
        self._audio_q: "queue.Queue[np.ndarray]" = queue.Queue()
        self._buffer = np.zeros(0, dtype=np.float32)
        self._stop = threading.Event()

    def _on_audio(self, indata, frames, time_info, status):  # sounddevice callback
        if status:
            print(f"[mic] {status}")
        self._audio_q.put(indata[:, 0].copy())

    def start(self) -> None:
        try:
            import sounddevice as sd
        except ImportError as e:
            raise RuntimeError("sounddevice not installed. Run: pip install sounddevice") from e

        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                callback=self._on_audio,
            ):
                self._loop()
        except Exception as e:
            raise RuntimeError(f"Could not open microphone: {e}") from e

    def _drain_queue(self) -> None:
        while True:
            try:
                self._buffer = np.concatenate([self._buffer, self._audio_q.get_nowait()])
            except queue.Empty:
                break

    def _loop(self) -> None:
        import time
        last = 0.0
        while not self._stop.is_set():
            self._drain_queue()
            now = time.monotonic()
            if now - last >= self.stride and len(self._buffer) > 0:
                window = self._buffer[-self.window:]
                result = self.transcriber.transcribe(window, vad_filter=True)
                self.on_update(PartialResult(result.text, is_final=False))
                last = now
            time.sleep(0.05)

        # final full-buffer pass
        self._drain_queue()
        if len(self._buffer) > 0:
            final = self.transcriber.transcribe(self._buffer, vad_filter=True)
            self.on_update(PartialResult(final.text, is_final=True))

    def stop(self) -> None:
        self._stop.set()


if __name__ == "__main__":
    print("Listening… press Ctrl-C to stop.")
    stream = MicStream(on_update=lambda p: print(("FINAL: " if p.is_final else "… ") + p.text))
    try:
        stream.start()
    except KeyboardInterrupt:
        stream.stop()
