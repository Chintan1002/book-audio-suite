"""
stitch.py — Stage 4 of the PDF -> Audiobook pipeline.

Concatenates the ordered audio chunks from tts.py (chunk_00000.mp3, ...) into a
single audiobook file. Output format is chosen from the extension:

  * .mp3            -> concat demuxer with `-c copy`: no re-encode, fast,
                       lossless. Tiny inter-file gaps are possible but land on
                       the sentence-boundary pauses, so they're inaudible.
  * .m4b/.m4a/.mp4  -> concat + re-encode to AAC: the real audiobook container
                       (chapters, position bookmarking, cover/metadata). The
                       re-encode fuses everything into one continuous, gapless
                       stream.

Why FFmpeg directly and not pydub: pydub decodes every chunk into an in-memory
PCM AudioSegment before joining — gigabytes of RAM for a long book, violating
the "never hold the whole book in memory" rule. FFmpeg streams through; Python
never touches the audio.

Chapters: FFmpeg writes chapter marks from an ffmetadata file. We have per-chunk
durations (ffprobe) but NOT chapter->chunk boundaries yet — Stage 3 packs by
sentence, not chapter. So `chapters=` is a ready seam: pass a list and they're
injected; leave it None (default) and there are none. Real boundary detection
is a Milestone 5 upgrade that fills this in.

Install:
    # the FFmpeg binaries (includes ffprobe):
    #   macOS:  brew install ffmpeg
    #   Ubuntu: sudo apt install ffmpeg
No Python deps — this drives the system FFmpeg via subprocess.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# --- Tunables ----------------------------------------------------------------

# Chunk files are named chunk_<index>.mp3 by tts.py; zero-padding makes a plain
# sorted glob equal to reading order.
CHUNK_GLOB = "chunk_*.mp3"

# AAC bitrate for the .m4b re-encode. 64k mono is plenty for speech.
DEFAULT_BITRATE = "64k"

# Extensions that mean "AAC in an MP4 container" (re-encode path).
_MP4_SUFFIXES = {".m4b", ".m4a", ".mp4"}


# --- Data structures ---------------------------------------------------------

@dataclass
class Chapter:
    """A chapter marker. `start_index` is the first chunk belonging to it."""
    title: str
    start_index: int


# --- Tool / input guards (fail loudly, up front) -----------------------------

def _require_tools(*, need_ffprobe: bool) -> None:
    """Fail before doing any work if FFmpeg (and optionally ffprobe) is absent."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "FFmpeg not found on PATH. Install it: `brew install ffmpeg` "
            "(macOS) or `sudo apt install ffmpeg` (Ubuntu)."
        )
    if need_ffprobe and shutil.which("ffprobe") is None:
        raise RuntimeError(
            "ffprobe not found on PATH (ships with FFmpeg). Reinstall FFmpeg."
        )


def collect_chunks(audio_dir: str | Path) -> list[Path]:
    """
    Return chunk files from a directory in index order, validating each is
    present and non-empty. Fails loudly on an empty/missing directory.
    """
    d = Path(audio_dir)
    if not d.is_dir():
        raise NotADirectoryError(f"No such audio directory: {d}")

    chunks = sorted(d.glob(CHUNK_GLOB))
    if not chunks:
        raise FileNotFoundError(f"No '{CHUNK_GLOB}' files in {d}")

    empty = [c.name for c in chunks if c.stat().st_size == 0]
    if empty:
        raise RuntimeError(f"Refusing to stitch empty chunk(s): {empty}")

    return chunks


# --- FFmpeg plumbing ---------------------------------------------------------

def _run(cmd: list[str]) -> None:
    """Run a subprocess, raising with captured stderr on non-zero exit."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise RuntimeError(f"command failed ({proc.returncode}): {cmd[0]}\n{tail}")


def _probe_duration(path: Path) -> float:
    """Seconds of audio in a file, via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"ffprobe could not read duration of {path.name}")
    return float(out.stdout.strip())


def _write_concat_list(chunks: list[Path], list_path: Path) -> None:
    """
    Write the concat-demuxer input file. Each line is `file '<abs path>'`; a
    single quote inside a path is escaped as '\\'' per FFmpeg's rules.
    """
    lines = []
    for c in chunks:
        p = str(c.resolve()).replace("'", "'\\''")
        lines.append(f"file '{p}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_metadata(
    chunks: list[Path],
    *,
    title: str | None,
    author: str | None,
    chapters: list[Chapter] | None,
) -> str:
    """
    Build an ffmetadata document (global tags + optional [CHAPTER] blocks).
    Chapter i spans from its first chunk's start offset to the next chapter's
    start (or the end of the book). Times are in milliseconds.
    """
    doc = [";FFMETADATA1"]
    if title:
        doc.append(f"title={_esc_meta(title)}")
    if author:
        doc.append(f"artist={_esc_meta(author)}")
    doc.append("genre=Audiobook")

    if chapters:
        # Cumulative start offset (ms) for every chunk = prefix sum of durations.
        starts_ms: list[int] = []
        acc = 0.0
        for c in chunks:
            starts_ms.append(round(acc * 1000))
            acc += _probe_duration(c)
        total_ms = round(acc * 1000)

        ordered = sorted(chapters, key=lambda ch: ch.start_index)
        for i, ch in enumerate(ordered):
            if not 0 <= ch.start_index < len(chunks):
                raise ValueError(
                    f"Chapter '{ch.title}' start_index {ch.start_index} "
                    f"out of range 0..{len(chunks) - 1}"
                )
            start = starts_ms[ch.start_index]
            end = (starts_ms[ordered[i + 1].start_index]
                   if i + 1 < len(ordered) else total_ms)
            doc += [
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={start}",
                f"END={end}",
                f"title={_esc_meta(ch.title)}",
            ]

    return "\n".join(doc) + "\n"


def _esc_meta(value: str) -> str:
    """Escape ffmetadata special chars (= ; # \\ and newline)."""
    for ch in ("\\", "=", ";", "#"):
        value = value.replace(ch, "\\" + ch)
    return value.replace("\n", " ")


# --- Public API --------------------------------------------------------------

def stitch(
    chunks: list[Path],
    out_path: str | Path,
    *,
    title: str | None = None,
    author: str | None = None,
    chapters: list[Chapter] | None = None,
    bitrate: str = DEFAULT_BITRATE,
) -> Path:
    """
    Concatenate ordered chunk files into one audiobook at out_path. The format
    (stream-copy .mp3 vs. AAC-encoded .m4b) is chosen from the extension.

    Returns the output path. Raises loudly on missing tools, no/empty chunks, an
    unsupported extension, or any FFmpeg failure.
    """
    out = Path(out_path)
    suffix = out.suffix.lower()
    if suffix == ".mp3":
        codec_args = ["-c", "copy"]
    elif suffix in _MP4_SUFFIXES:
        codec_args = ["-c:a", "aac", "-b:a", bitrate]
    else:
        raise ValueError(
            f"Unsupported output extension '{suffix}'. Use .mp3 or .m4b/.m4a/.mp4."
        )

    if not chunks:
        raise ValueError("No chunks to stitch.")
    _require_tools(need_ffprobe=bool(chapters))
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        list_path = tmp_dir / "concat.txt"
        meta_path = tmp_dir / "meta.txt"
        _write_concat_list(chunks, list_path)
        meta_path.write_text(
            _build_metadata(chunks, title=title, author=author, chapters=chapters),
            encoding="utf-8",
        )

        # Input 0 = concatenated audio; input 1 = metadata. `-map_metadata 1`
        # pulls global tags + chapters from the metadata file.
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-i", str(meta_path), "-map_metadata", "1",
            *codec_args,
            str(out),
        ]
        _run(cmd)

    return out


# --- CLI / smoke test --------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Stitch chunk_*.mp3 into an audiobook.")
    ap.add_argument("audio_dir", help="directory of chunk_*.mp3 files")
    ap.add_argument("out", help="output file (.mp3 or .m4b/.m4a/.mp4)")
    ap.add_argument("--title")
    ap.add_argument("--author")
    args = ap.parse_args()

    files = collect_chunks(args.audio_dir)
    print(f"stitching {len(files)} chunks -> {args.out} ...")
    result = stitch(files, args.out, title=args.title, author=args.author)

    size_mb = result.stat().st_size / 1e6
    dur = _probe_duration(result) if shutil.which("ffprobe") else None
    tail = f", {dur/60:.1f} min" if dur else ""
    print(f"wrote {result}  ({size_mb:.1f} MB{tail})")
