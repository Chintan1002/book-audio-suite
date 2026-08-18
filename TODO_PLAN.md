# TODO & Plan — Book/Audio Suite

> ✅ **UPDATE:** Both features are now live in the web app. Feature A (**TTS —
> PDF → audiobook**) and Feature B (**STT — audio → text**) each have their own
> tab at `http://localhost:8000`. STT file-upload transcription is **done**;
> what remains for Feature B is live-mic streaming (see below).

---

## The product, reframed

This is a two-way **book ⇄ audio suite**:

- **Feature A — TTS (Text → Speech):** turn a PDF book into a narrated
  audiobook. *(Exists — the current pipeline.)*
- **Feature B — STT (Speech → Text):** turn spoken audio into text — the
  inverse pipeline. *(New — to be built.)*

Both share the same philosophy: local, zero-cost, no paid APIs, modular Python
stages connected by plain data.

---

## Feature A — TTS (PDF → Audiobook)

**Status: functional, in quality-pass phase.**

Done:
- ✅ `extractor.py` — text-layer + OCR-fallback extraction (Stage 1)
- ✅ `cleaner.py` — regex cleanup pipeline (Stage 2)
- ✅ `tts.py` — chunker + async Edge-TTS synthesis (Stage 3)
- ✅ `stitch.py` — FFmpeg concat → .m4b / .mp3 (Stage 4)
- ✅ `pipeline.py` — end-to-end orchestration (Stage 5)
- ✅ `chapters.py` — chapter detection (TOC + heading heuristics)
- ✅ `app.py` + `index.html` — local FastAPI web app

Open work:
- [ ] **Chapter detection is buggy** — heading heuristics over-split on page
      numbers (bare-number / roman-numeral patterns match page numbers, cutting
      one "chapter" per page); titles get polluted with body text. Fix: add a
      standalone-line guard (short line + sparse surroundings), clean the title,
      and add a confidence gate that falls back to fixed-size "Parts" when
      detection looks unreliable.
- [ ] Voice/pace tuning and preview polish
- [ ] Optional local-LLM cleanup for OCR-heavy scanned books
- [ ] Immersive UI redesign (riso-brutalist book→cassette concept; Stitch +
      Three.js/GSAP scroll build)

---

## Feature B — STT (Audio → Text)

**Status: file-upload transcription shipped and wired into the web app.**

Done:
- ✅ `stt/` package — ingest (FFmpeg → 16 kHz mono), transcribe (faster-whisper
      with word timestamps), formatter, output (`.txt`/`.md`/`.json`), pipeline, CLI
- ✅ **Web integration** — "Audio → Text" tab beside the TTS tab. `POST /transcribe`
      creates a job in the same registry/thread-pool as TTS, `GET /jobs/{id}` polls
      it, `GET /jobs/{id}/transcript.{txt,md,json}` downloads. Model/language
      pickers, inline transcript preview, `GET /stt/status` for a clear message
      when deps are missing.
- ✅ The faster-whisper model is loaded **once** and reused across requests
      (transcription is serialized by a lock — one WhisperModel, 2 pool workers).

Open work:
- [ ] **Live-mic streaming** (`stt/stream.py`) — WebSocket into the UI, rolling
      partials. The one remaining piece of the PRD.
- [ ] Real progress *during* transcription — `transcribe_file()` has no progress
      hook, so the bar reports coarse stages (load → transcribe → write). Fix by
      iterating faster-whisper's segment generator and reporting `segment.end /
      duration`.
- [ ] Optional: subtitles (`.srt`/`.vtt`) — trivial from the timestamps we already
      emit; diarization (pyannote) is the bigger later item.

Goal: take an audio file (or recording) and produce clean, readable text —
transcripts, notes, or a text version of spoken content. The mirror image of
the TTS pipeline.

Proposed stages (to architect together — don't build blind):
- [ ] **Ingest** — accept common audio formats (mp3/wav/m4a/etc.); normalize
      with FFmpeg (mono, 16 kHz) for the recognizer.
- [ ] **Transcribe** — local, free STT engine. Lead candidate:
      **faster-whisper** (Whisper, CTranslate2 backend — fast, offline, good
      accuracy; runs on CPU, faster on GPU). Alternatives: `whisper.cpp`,
      Vosk (ultra-light).
- [ ] **Segment & timestamp** — keep word/segment timestamps for navigation and
      optional subtitles (.srt/.vtt).
- [ ] **Clean & format** — punctuation/casing restoration, paragraph breaks,
      speaker turns; reuse ideas from `cleaner.py`.
- [ ] **Output** — plain .txt / .md transcript, optional .srt captions.
- [ ] **Wire into the web app** — a second mode/tab alongside the TTS flow.

Open scope questions to settle first:
- Single speaker or diarization (who-said-what)?
- Just transcripts, or also subtitle files?
- Live/streaming input, or file-only to start?

---

## Suggested next-session prompt

"Let's start Feature B — the STT pipeline (audio → text). Architect the stages
first (ingest → transcribe with faster-whisper → segment → clean → output),
then we build the ingest + transcribe module. Keep it local and free like the
TTS side."
