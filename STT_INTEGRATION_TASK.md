# TASK BRIEF — Integrate the STT feature into the existing web app

You are picking up a real, in-progress project. Work autonomously through this
brief end to end. Follow the working style in `CLAUDE.md` (mentor tone,
architect-before-code, modular typed Python, verify each stage).

---

## 0. Read these first (do not skip)

Before writing any code, read and internalize:
- `PRD_STT.md` — the full spec for the Speech-to-Text feature (scope, outputs,
  architecture, edge cases). This is the source of truth for WHAT to build.
- `TODO_PLAN.md` — where STT sits in the two-feature (TTS + STT) roadmap.
- `CLAUDE.md` and `PROJECT_PLAN.md` — project context and how I like to work.
- `stt/` — the scaffolded STT package (models, ingest, transcribe, formatter,
  output, pipeline, stream, cli). This is HOW transcription already works.
- `app.py` and `index.html` — the EXISTING web app for the TTS (PDF→audiobook)
  feature. Study exactly how its flow works: file upload, job creation, the
  `jobs/` directory, background processing, status polling, and download
  endpoints.

Then summarize back to me, in a few lines: how the current TTS web flow is
structured, and your plan to mirror it for STT. Wait for nothing — proceed once
you've confirmed the plan against the actual code.

## 1. Objective

Add the STT feature (`stt/` package) to the SAME web app so the site has two
modes: "Book → Audio" (existing TTS) and "Audio → Text" (new STT). A user
uploads an audio file, the server transcribes it with the existing
`stt.pipeline.transcribe_file()`, and the user downloads the transcript
(`.txt`, `.md`) and timestamped `.json`.

## 2. Hard constraints

- MIRROR the existing TTS pattern in `app.py` — reuse the same job model,
  `jobs/` storage, status-polling mechanism, and download approach. Do NOT
  invent a parallel system.
- Local and free only — no paid/cloud APIs. Transcription is `faster-whisper`
  via the existing `stt/` code.
- Load the faster-whisper model ONCE at startup (or first use) and reuse it
  across requests — never reload per request (it's the expensive step).
- Don't break the existing TTS flow. It must keep working unchanged.
- Keep the frontend consistent with the current UI — same styling, same status
  states, same error handling.

## 3. Build order (verify each step before the next)

1. **Backend — STT job endpoint.** In `app.py`, add an audio-upload endpoint
   that saves the file, creates a job (same structure as TTS), and runs
   `stt.pipeline.transcribe_file()` in the background, writing outputs into the
   job folder. Reuse the existing status + download endpoints, extending them
   for the transcript file types (.txt/.md/.json) if needed.
2. **Test the backend** with a real audio file (curl or the CLI) — confirm a job
   runs to completion and the three output files are downloadable — BEFORE
   touching the UI.
3. **Frontend — two modes.** In `index.html`, add a tab/toggle: "Book → Audio"
   (existing) and "Audio → Text" (new). The STT tab accepts an audio file, shows
   job progress exactly like TTS, and offers the transcript downloads on
   completion.
4. **Test end to end** in the browser: upload audio → watch progress → download
   transcript. Confirm the TTS tab still works.

## 4. Acceptance criteria

- Uploading an audio file via the STT tab produces a correct, punctuated
  transcript plus a timestamped JSON, downloadable from the UI.
- The faster-whisper model is loaded once and reused.
- The existing TTS flow is untouched and still works.
- Errors (bad audio, no file, model missing) surface cleanly in the UI, not as
  raw stack traces.

## 5. Edge cases to handle (see PRD section 9)

Unsupported/corrupt audio, silent/empty audio, very long files (stream to disk),
missing FFmpeg, model-not-downloaded on first run, non-English audio
(auto-detect; allow a language override).

## 6. Out of scope for now

Live-microphone streaming (`stt/stream.py`) — that's a later phase (WebSocket
into the UI). Build file-upload only here. Also skip diarization and subtitles.

## 7. Finish

When done: run the app, do one real end-to-end transcription, and report what
you changed (files touched, new endpoints, how to run it). Update `TODO_PLAN.md`
to mark the STT web integration done.
