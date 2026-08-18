# PRD — Feature B: Speech → Text (STT)

The inverse of the TTS audiobook pipeline. Take audio — a file or a live
microphone — and produce clean, readable text plus machine-readable timestamps.
Local, zero-cost, no paid APIs.

---

## 1. Problem & goal

The suite already turns books into audio (TTS). Feature B closes the loop: turn
**audio into text**. A user drops in a recording (lecture, interview, voice memo,
audiobook clip) or speaks into their mic, and gets back a punctuated transcript
they can read, search, and reuse — with timestamps for navigation.

**Goal:** a local, free, accurate transcription pipeline that handles both a
finished audio file (batch) and a live microphone (streaming), producing a clean
`.txt`/`.md` transcript and a timestamped `.json`.

## 2. Users & use cases

- Turn a recorded lecture or meeting into readable notes.
- Transcribe a voice memo or interview.
- Draft-transcribe live speech as it's spoken.

## 3. Scope (locked)

**In scope**
- Single continuous transcript — **no speaker diarization** (who-said-what).
- Two input modes: **file** (batch) and **live mic** (streaming, near-real-time).
- Outputs: **clean transcript** (`.txt` / `.md`) and **timestamped JSON**.
- Local, free engine. No paid cloud APIs.

**Out of scope (for now)**
- Speaker diarization / labels (planned later — see section 8).
- Subtitle files (`.srt`/`.vtt`) — easy to add later from the same timestamps.
- Translation, summarization (downstream features).

## 4. Requirements

**Functional**
- Accept common audio formats (mp3, wav, m4a, flac, ogg, and audio from mp4).
- Normalize any input to mono 16 kHz PCM for the recognizer.
- Transcribe with word/segment timestamps.
- Restore punctuation, casing, and paragraph breaks for readability.
- Write `.txt`, `.md`, and timestamped `.json`.
- Live mode: capture mic, emit rolling partial results, finalize on pause/stop.

**Non-functional**
- Fully offline after model download; no per-use cost.
- Streaming latency target: partial text within ~2-3 s of speech.
- Graceful failure: unreadable audio, no mic, model-not-downloaded, empty audio.
- Modular, typed Python; each stage independently runnable (house style).

## 5. Architecture

```
                +----------- file path (batch) -----------+
audio file --> [1] Ingest --> 16kHz mono --> [2] Transcribe --> segments
                                                  |               |
                                                  v               v
                                        [3] Format --> clean text [5] Output
                                                                  (.txt/.md/.json)
mic --> [4] Stream --> rolling windows --> [2] Transcribe --> partial/final --> [3][5]
```

Both paths converge on the **same transcriber** (stage 2) — the live path just
feeds it audio in rolling chunks instead of one whole file. That's why file-only
is the foundation and streaming wraps it.

## 6. Module map

| # | Module | File | Responsibility |
|---|--------|------|----------------|
| 0 | Data types | `stt/models.py` | `Word`, `TranscriptSegment`, `TranscriptResult` |
| 1 | Ingest | `stt/ingest.py` | Decode + normalize any audio to 16 kHz mono via FFmpeg |
| 2 | Transcribe | `stt/transcribe.py` | faster-whisper wrapper -> segments + word timestamps |
| 3 | Format | `stt/formatter.py` | Punctuation/casing cleanup, paragraph reflow |
| 4 | Stream | `stt/stream.py` | Live mic capture, rolling buffer, partial results |
| 5 | Output | `stt/output.py` | Write `.txt`, `.md`, timestamped `.json` |
| - | Pipeline | `stt/pipeline.py` | File path: ingest -> transcribe -> format -> output |
| - | CLI | `stt/cli.py` | `transcribe <file>` and `listen` (live) entry points |

## 7. Engine choice (free / local)

**Lead: `faster-whisper`** — OpenAI Whisper reimplemented on CTranslate2. Fast,
fully offline, word-level timestamps, runs on CPU (int8) and much faster on GPU.
Model sizes tiny->large-v3 trade speed for accuracy; default to `base` or
`small`, allow override.

Alternatives if constrained: `whisper.cpp` (C++, very light), `vosk` (tiny,
streaming-native but lower accuracy).

**Bottlenecks to respect:** model size vs. VRAM/CPU (transcription is the cost);
in streaming, the window/stride tradeoff drives latency vs. accuracy and words
may revise as more audio arrives.

## 8. Milestones

1. **M1 — File transcript:** ingest -> transcribe -> `.txt`/`.md` from a real file.
2. **M2 — Timestamps:** emit timestamped `.json`; formatter for readable prose.
3. **M3 — Live mic:** streaming capture with rolling partials and a final flush.
4. **M4 — Web app:** second mode/tab alongside the TTS UI.
5. **M5 — Later:** diarization (pyannote), subtitles (.srt/.vtt), translation.

## 9. Edge cases

- Corrupt/unsupported audio -> fail at ingest with a clear message.
- Silent or empty audio -> empty transcript, not a crash.
- No microphone / permission denied (live) -> clear error, fall back to file.
- Model not downloaded -> prompt to fetch on first run.
- Very long file -> stream segments to disk; don't hold the whole transcript in
  memory.
- Non-English / mixed language -> Whisper auto-detects; expose a language override.

## 10. Success criteria

A real recording produces a punctuated, paragraph-formatted transcript plus a
timestamped JSON; live mic shows text within a few seconds of speech and
finalizes cleanly on stop — all locally and free.
