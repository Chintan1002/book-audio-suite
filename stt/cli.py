"""CLI for the STT feature.

    python -m stt.cli transcribe path/to/audio.mp3 [--model small] [--lang en]
    python -m stt.cli listen [--model base]
"""

from __future__ import annotations

import argparse

from .transcribe import DEFAULT_MODEL


def main() -> None:
    parser = argparse.ArgumentParser(prog="stt", description="Speech -> text (local, free)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("transcribe", help="transcribe an audio file")
    t.add_argument("audio")
    t.add_argument("--model", default=DEFAULT_MODEL)
    t.add_argument("--lang", default=None, help="language code, or omit to auto-detect")
    t.add_argument("--out", default=None, help="output stem (default: alongside input)")

    l = sub.add_parser("listen", help="transcribe live from the microphone")
    l.add_argument("--model", default=DEFAULT_MODEL)

    args = parser.parse_args()

    if args.cmd == "transcribe":
        from .pipeline import transcribe_file
        r = transcribe_file(args.audio, out_stem=args.out, model=args.model, language=args.lang)
        print(f"[{r.language}] {r.duration:.1f}s, {len(r.segments)} segments")
    elif args.cmd == "listen":
        from .stream import MicStream
        print("Listening… Ctrl-C to stop.")
        s = MicStream(on_update=lambda p: print(("FINAL: " if p.is_final else "… ") + p.text), model=args.model)
        try:
            s.start()
        except KeyboardInterrupt:
            s.stop()


if __name__ == "__main__":
    main()
