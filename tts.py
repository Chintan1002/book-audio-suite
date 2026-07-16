"""
tts.py — Stage 3 of the PDF -> Audiobook pipeline.

Turns narration-ready text (from cleaner.py) into an ordered set of audio
chunk files on disk, ready for stitch.py to concatenate.

Two separate concerns, deliberately kept apart:

  1. CHUNKER — pure & synchronous, no network. Splits prose into ordered
     TextChunks on sentence boundaries so no single TTS call is huge.
  2. SYNTHESIZER — async & network-bound. Turns each TextChunk into an audio
     file via Edge-TTS, with a concurrency cap, per-chunk retry, and timeout.

The engine is swappable: synthesis goes through one coroutine signature
`async (text, out_path) -> None`. Edge-TTS is the default; Kokoro/Piper (which
are VRAM/CPU-bound and want a small sequential pool, not network fan-out) can
slot in behind the same seam without touching the chunker or the harness.

Install (default engine):
    pip install edge-tts        # free, no API key; needs an internet connection

The load-bearing idea: a chunk's position in the book is frozen into its
`index` at CHUNK time, before any TTS call. That index travels with the chunk
into its filename (chunk_00007.mp3). So even though async synthesis finishes
chunks out of order, order is recovered later by a plain sorted glob — never by
completion order.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Iterator

# --- Tunables ----------------------------------------------------------------

# Target upper bound on characters per chunk. Big enough to keep the number of
# TTS calls (and network round-trips) low; small enough that no single call is
# huge or slow to synthesize. ~3k chars is a few paragraphs of prose.
MAX_CHUNK_CHARS = 3000

# Edge-TTS default voice. Override per-call via `voice=` (list all voices with
# `edge-tts --list-voices`).
DEFAULT_VOICE = "en-US-GuyNeural"

# How many chunks may be in flight at once. Edge-TTS is network + rate-limited;
# fire too many and Microsoft's servers throttle or 403. 4 is a safe start.
DEFAULT_CONCURRENCY = 4

# Per-chunk ceiling. One hung request must not stall the whole batch.
CHUNK_TIMEOUT_S = 120.0

# Attempts per chunk before giving up (transient drops / throttling).
MAX_RETRIES = 3


# --- Data structures ---------------------------------------------------------

@dataclass
class TextChunk:
    """One unit of text to synthesize. `index` defines reading order."""
    index: int          # 0-based; frozen here, travels into the filename
    text: str
    char_count: int


@dataclass
class AudioChunk:
    """Result of synthesizing one TextChunk. `index` still defines order."""
    index: int
    path: Path
    ok: bool
    error: str | None = None


# --- Sentence splitting ------------------------------------------------------

# Split AFTER sentence-ending punctuation that is followed by whitespace.
# Naive on purpose: it will mis-split on "Dr.", "e.g.", "3.14". That's a cheap
# error to make here — a false split just synthesizes the two halves adjacently,
# so the only artifact is a slightly longer pause. Not worth a heavy sentence-
# boundary dep (pysbd/nltk) on the fast path. pysbd stays noted as an M5 upgrade.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text: str) -> list[str]:
    """
    Split text into sentences, collapsing internal whitespace so each sentence
    is a clean single line (extraction/cleaning can leave stray newlines that
    the TTS would otherwise read as pauses).
    """
    text = text.strip()
    if not text:
        return []
    sentences = []
    for raw in _SENTENCE_BOUNDARY.split(text):
        s = re.sub(r"\s+", " ", raw).strip()
        if s:
            sentences.append(s)
    return sentences


def _hard_split(sentence: str, max_chars: int) -> Iterator[str]:
    """
    Last-resort splitter for a single sentence longer than max_chars (a run-on,
    an unpunctuated list, or OCR garble). Break on whitespace, packing words up
    to the limit. Degrades gracefully rather than emitting an oversized chunk.

    A single word longer than max_chars (a URL, OCR noise) is yielded alone and
    left over-limit — truncating would silently drop text, which is worse.
    """
    words = sentence.split()
    buf: list[str] = []
    buf_len = 0
    for w in words:
        extra = len(w) + (1 if buf else 0)  # +1 for the joining space
        if buf and buf_len + extra > max_chars:
            yield " ".join(buf)
            buf, buf_len = [], 0
            extra = len(w)
        buf.append(w)
        buf_len += extra
    if buf:
        yield " ".join(buf)


# --- The chunker -------------------------------------------------------------

def chunk_text(
    text: str,
    max_chars: int = MAX_CHUNK_CHARS,
) -> Iterator[TextChunk]:
    """
    Yield ordered TextChunks, each at most `max_chars` characters (except a lone
    over-long word, see _hard_split). Greedy sentence packing: fill a buffer
    with whole sentences until the next one would overflow, then flush.

    A generator by design — a 600-page book must never fully materialize.
    """
    index = 0
    buf: list[str] = []
    buf_len = 0

    def flush() -> Iterator[TextChunk]:
        nonlocal index, buf, buf_len
        if buf:
            chunk = " ".join(buf)
            yield TextChunk(index, chunk, len(chunk))
            index += 1
            buf, buf_len = [], 0

    for sentence in split_sentences(text):
        # A single sentence bigger than the limit can't be packed — flush what
        # we have, then hard-split it into its own chunks.
        if len(sentence) > max_chars:
            yield from flush()
            for piece in _hard_split(sentence, max_chars):
                yield TextChunk(index, piece, len(piece))
                index += 1
            continue

        extra = len(sentence) + (1 if buf else 0)  # +1 for the joining space
        if buf and buf_len + extra > max_chars:
            yield from flush()
            extra = len(sentence)  # buffer is empty now, no leading space
        buf.append(sentence)
        buf_len += extra

    yield from flush()


# --- Synthesizer: the engine seam --------------------------------------------

# Every engine is a coroutine with this shape. The chunker and the concurrency
# harness only know this signature — never which engine is behind it.
Engine = Callable[[str, Path], Awaitable[None]]


def make_edge_engine(
    voice: str = DEFAULT_VOICE,
    *,
    rate: str | None = None,
    volume: str | None = None,
    pitch: str | None = None,
) -> Engine:
    """
    Build an Edge-TTS engine bound to a voice. These are Edge-TTS's only prosody
    knobs (it exposes no SSML break control):
      * rate   — speed, e.g. '-10%' / '+5%'. Slowing softens the clipped,
                 mechanical cadence at punctuation — the most effective lever.
      * volume — loudness, e.g. '+0%' / '-10%'.
      * pitch  — e.g. '-5Hz' / '+10Hz'. A touch lower often reads warmer.
    edge-tts is lazy-imported so the chunker (and any offline-engine user) never
    needs it installed.
    """
    async def _synth(text: str, out_path: Path) -> None:
        try:
            import edge_tts
        except ImportError as e:
            raise RuntimeError(
                "Edge-TTS engine needs 'edge-tts'. Run: pip install edge-tts"
            ) from e
        kwargs = {}
        if rate:
            kwargs["rate"] = rate
        if volume:
            kwargs["volume"] = volume
        if pitch:
            kwargs["pitch"] = pitch
        communicate = edge_tts.Communicate(text, voice, **kwargs)
        await communicate.save(str(out_path))

    return _synth


# --- Synthesizer: the concurrency harness ------------------------------------

async def _synthesize_one(
    chunk: TextChunk,
    engine: Engine,
    out_dir: Path,
    sem: asyncio.Semaphore,
    *,
    timeout: float,
    retries: int,
    skip_existing: bool,
) -> AudioChunk:
    """
    Synthesize one chunk to chunk_<index>.mp3. The index (not completion order)
    fixes the filename, so a plain sorted glob later recovers reading order.

    Retries with exponential backoff on any failure; returns ok=False rather
    than raising, so one bad chunk can't kill the batch. With `skip_existing`,
    an already-synthesized chunk is treated as done — the resume checkpoint.
    """
    out_path = out_dir / f"chunk_{chunk.index:05d}.mp3"
    # Resume: a chunk already on disk is done — don't re-spend a network call
    # (or even a semaphore slot) on it.
    if skip_existing and out_path.exists() and out_path.stat().st_size > 0:
        return AudioChunk(chunk.index, out_path, ok=True)
    async with sem:  # cap in-flight requests, including their backoff waits
        last_err = "unknown error"
        for attempt in range(1, retries + 1):
            try:
                await asyncio.wait_for(engine(chunk.text, out_path), timeout)
                # Guard against a silent write: an engine can return cleanly yet
                # leave no/empty file (e.g. Edge-TTS emitting no audio events).
                if not out_path.exists() or out_path.stat().st_size == 0:
                    raise RuntimeError("engine produced no audio")
                return AudioChunk(chunk.index, out_path, ok=True)
            except Exception as e:  # noqa: BLE001 — record and maybe retry
                last_err = f"{type(e).__name__}: {e}"
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)  # 2s, 4s, ...
        return AudioChunk(chunk.index, out_path, ok=False, error=last_err)


async def synthesize_book(
    chunks: Iterable[TextChunk],
    out_dir: str | Path,
    *,
    voice: str = DEFAULT_VOICE,
    engine: Engine | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = CHUNK_TIMEOUT_S,
    retries: int = MAX_RETRIES,
    skip_existing: bool = False,
    on_done: Callable[[AudioChunk], None] | None = None,
) -> list[AudioChunk]:
    """
    Synthesize all chunks concurrently (capped) into out_dir, returning results
    sorted by index. Pass `voice=` for the default Edge-TTS engine, or a custom
    `engine=` (e.g. a future Kokoro/Piper engine) to bypass Edge-TTS entirely.
    With `skip_existing=True`, chunks already on disk are kept (resume).
    `on_done` (if given) is called with each AudioChunk as it finishes — handy
    for driving a progress bar, since chunks complete out of order.

    The chunk *text* is materialized to schedule tasks — cheap (a whole book of
    text is a few MB). Audio never all lives in memory; each chunk streams
    straight to its own file. For very long books, Stage 5 will feed this in
    per-batch slices rather than one giant gather.
    """
    engine = engine or make_edge_engine(voice)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(concurrency)
    tasks = [
        asyncio.create_task(
            _synthesize_one(c, engine, out_dir, sem, timeout=timeout,
                            retries=retries, skip_existing=skip_existing)
        )
        for c in chunks
    ]
    results: list[AudioChunk] = []
    for fut in asyncio.as_completed(tasks):
        r = await fut
        results.append(r)
        if on_done is not None:
            on_done(r)
    return sorted(results, key=lambda a: a.index)


def synthesize_text(
    text: str,
    out_dir: str | Path,
    *,
    voice: str = DEFAULT_VOICE,
    max_chars: int = MAX_CHUNK_CHARS,
    **kwargs,
) -> list[AudioChunk]:
    """Convenience: chunk then synthesize in one synchronous call."""
    chunks = list(chunk_text(text, max_chars=max_chars))
    return asyncio.run(synthesize_book(chunks, out_dir, voice=voice, **kwargs))


# --- CLI / smoke test --------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path

    _SAMPLE = (
        "Call me Ishmael. Some years ago—never mind how long precisely—"
        "having little or no money in my purse, I thought I would sail "
        "about a little and see the watery part of the world. "
        + "It is a way I have of driving off the spleen. " * 60   # forces a flush
        + "ThisSingleWordHasNoSpaces" + "x" * 3200 + " and then normal text. "
        + "A short tail sentence."
    )

    args = sys.argv[1:]
    # `--synth` (optionally with a .txt path) actually calls Edge-TTS and writes
    # mp3s. Without it, we do a network-free chunker-only dry run.
    do_synth = "--synth" in args
    paths = [a for a in args if not a.startswith("--")]
    text = Path(paths[0]).read_text(encoding="utf-8") if paths else _SAMPLE
    out_dir = paths[1] if len(paths) > 1 else "audio_out"

    chunks = list(chunk_text(text))
    max_seen = max((c.char_count for c in chunks), default=0)
    for c in chunks:
        preview = c.text[:60].replace("\n", " ")
        print(f"[{c.index:05d}] {c.char_count:>5} chars | {preview}...")
    print("-" * 60)
    print(f"{len(chunks)} chunks, largest = {max_seen} chars (limit {MAX_CHUNK_CHARS})")

    if not do_synth:
        print("dry run (chunker only). Add --synth to synthesize with Edge-TTS.")
        raise SystemExit(0)

    print(f"synthesizing -> {out_dir}/  (voice={DEFAULT_VOICE}) ...")
    results = asyncio.run(synthesize_book(chunks, out_dir))
    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    print(f"done: {len(ok)} ok, {len(bad)} failed")
    for r in bad:
        print(f"  [FAIL {r.index:05d}] {r.error}")
