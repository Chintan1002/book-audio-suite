"""
app.py — FastAPI web layer over BOTH pipelines of the Book/Audio Suite.

Two modes share one job model:
  * TTS  "Book -> Audio":  PDF   -> chaptered audiobook  (pipeline.run_pipeline)
  * STT  "Audio -> Text":  audio -> transcript           (stt.pipeline.transcribe_file)

Both take minutes, so both use the same async job model: upload starts a
background job, the browser polls for progress, then downloads.

    GET  /                       -> the single-page UI (index.html)
    GET  /voices                 -> English Edge-TTS voices (for the dropdown)
    GET  /sample                 -> a voice-preview clip
    POST /convert                -> {job_id}; starts a PDF -> audiobook job
    POST /transcribe             -> {job_id}; starts an audio -> text job
    GET  /stt/status             -> whether the STT deps are available
    GET  /jobs/{id}              -> {status, stage, progress, ...} (both kinds)
    GET  /jobs/{id}/full         -> the whole-book audiobook       (TTS)
    GET  /jobs/{id}/chapters.zip -> all per-chapter mp3s, zipped   (TTS)
    GET  /jobs/{id}/textpdf      -> the OCR text PDF               (TTS, scanned)
    GET  /jobs/{id}/transcript.{fmt} -> transcript .txt/.md/.json  (STT)

No accounts, no sign-in: upload, convert, download. Persistence goes through
`db/repository.py`, which is MemoryRepository by default (process-local, lost on
restart) and PostgresRepository when DATABASE_URL is set.

Google Drive saving and the sign-in gate were built and then removed to ship a
smaller v1 — `drive.py` and `db/schema.sql` remain in the tree for when they
come back. See STORAGE_TASK.md and SECURITY.md.

Run:  ./.venv/bin/uvicorn app:app --reload    then open http://localhost:8000
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import shutil
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import edge_tts
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from db.repository import JobRecord, MemoryRepository, Repository
from pipeline import PipelineConfig, run_pipeline
from tts import DEFAULT_VOICE, make_edge_engine

# NOTE: the `stt` package is imported LAZILY (inside the worker / status check).
# It pulls in numpy + faster-whisper; a missing STT dependency must never break
# the TTS flow or stop the app from starting.

APP_DIR = Path(__file__).parent
JOBS_DIR = APP_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)


def _load_dotenv() -> None:
    """Read .env into the environment so local config doesn't have to be
    exported every session. Real environment variables always win."""
    env_file = APP_DIR / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

# Cache for voice-preview clips, keyed by (voice, rate). Repeat previews are
# then instant instead of re-hitting Edge-TTS.
SAMPLE_DIR = APP_DIR / "voice_samples_cache"
SAMPLE_DIR.mkdir(exist_ok=True)

# The one line spoken for every voice preview. Chosen to show off comma pauses,
# a dash, and a little warmth — the qualities people audition voices for.
SAMPLE_TEXT = (
    "Hello, this is a preview of my voice. I can read your book aloud, "
    "chapter by chapter — clearly, and with a little warmth."
)

# --- STT settings ------------------------------------------------------------

# Audio containers we accept for transcription (PRD section 4).
AUDIO_SUFFIXES = {
    ".mp3", ".wav", ".m4a", ".m4b", ".flac", ".ogg", ".oga", ".opus",
    ".aac", ".wma", ".mp4", ".mov", ".webm",
}
STT_DEFAULT_MODEL = "base"       # tiny|base|small|medium|large-v3

app = FastAPI(title="Book/Audio Suite")

# Small pool: conversions are long; a couple in parallel is plenty for local use.
_executor = ThreadPoolExecutor(max_workers=2)
_voices_cache: list[dict] | None = None

# All job/transcript persistence goes through this seam. Set DATABASE_URL to
# run against Postgres/Supabase (jobs survive restarts, transcripts become real
# per-user rows); leave it unset and the app runs exactly as before, entirely in
# memory, with no database required for local work or tests.
def _make_repository() -> Repository:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        return MemoryRepository()
    try:
        from db.repository import PostgresRepository
        return PostgresRepository(dsn)
    except Exception as e:
        # A database that is configured but unreachable is worth shouting about;
        # silently degrading to memory would look like data loss later.
        raise RuntimeError(
            f"DATABASE_URL is set but the database could not be opened: {e}"
        ) from e


repo: Repository = _make_repository()

# Placeholder owner until real auth lands (M3). Every call already passes a
# user_id, so adding accounts becomes a matter of resolving this per request
# rather than threading a new argument through the whole app.
#
# It is a real UUID because the schema types user_id as uuid referencing
# auth.users; DEV_USER_ID lets you point local work at a specific account.
CURRENT_USER = os.environ.get("DEV_USER_ID", "00000000-0000-0000-0000-000000000001")

# The faster-whisper model is the expensive thing to load, so ONE Transcriber is
# created and reused for every request. `_stt_model_lock` guards construction;
# `_stt_run_lock` serializes actual transcription, because the pool has 2 workers
# and a single WhisperModel is not safe to drive concurrently (and parallel
# transcription would only thrash the CPU anyway).
_transcriber = None              # type: ignore[var-annotated]  (lazy stt import)
_stt_model_lock = threading.Lock()
_stt_run_lock = threading.Lock()


def _get_transcriber(model: str):
    """Return the shared Transcriber, building it once. Imports stt lazily."""
    global _transcriber
    from stt.transcribe import Transcriber      # lazy: needs numpy/faster-whisper

    with _stt_model_lock:
        if _transcriber is None or _transcriber.model_name != model:
            # Constructing is cheap; the weights load on first transcribe() and
            # then stay resident on this instance for every later job.
            _transcriber = Transcriber(model=model)
        return _transcriber


def _run_job(job_id: str, pdf_path: Path, out_dir: Path, cfg: PipelineConfig) -> None:
    """Runs in a worker thread. run_pipeline calls asyncio.run internally, which
    is safe here because a thread-pool worker has no running event loop."""
    repo.update_job(job_id, status="running")

    def progress(stage: str, frac: float) -> None:
        repo.update_job(job_id, stage=stage, progress=frac)

    cfg.progress = progress
    try:
        r = run_pipeline(pdf_path, out_dir, cfg)
        # Paths are stored as strings so the record maps straight onto the
        # jobs.result jsonb column when PostgresRepository arrives.
        repo.update_job(
            job_id,
            result={
                "full": str(r.full_audio),
                "chapters": [str(p) for p in r.chapter_files],
                "text_pdf": str(r.text_pdf) if r.text_pdf else None,
            },
            status="done", progress=1.0, stage="done",
        )
    except Exception as e:  # surface to the UI instead of dying silently
        repo.update_job(job_id, status="error", error=f"{type(e).__name__}: {e}")


def _friendly_stt_error(e: Exception) -> str:
    """Turn the common STT failures into something a user can act on."""
    msg = str(e)
    low = msg.lower()
    if isinstance(e, ImportError) or "faster-whisper" in low or "no module named" in low:
        return ("Speech-to-text libraries are missing. Install them with: "
                "./.venv/bin/python -m pip install -r requirements_stt.txt")
    # Order matters: a decode failure also mentions "ffmpeg", so match the
    # specific case before the generic "FFmpeg is missing" one.
    if "failed to decode" in low or "invalid data" in low:
        return ("That audio file could not be decoded — it may be corrupt, "
                "truncated, or not really audio.")
    if "not found on path" in low or "ffmpeg not found" in low:
        return "FFmpeg not found on PATH. Install it (macOS: brew install ffmpeg)."
    if any(k in low for k in ("connection", "download", "huggingface", "resolve", "network")):
        return (f"Could not download the '{STT_DEFAULT_MODEL}' speech model on first run. "
                "Check your internet connection — it is cached locally afterwards.")
    return f"{type(e).__name__}: {msg}"


def _run_stt_job(job_id: str, audio_path: Path, out_dir: Path,
                 model: str, language: str | None) -> None:
    """
    Runs in a worker thread — the STT mirror of _run_job. Delegates to the
    existing stt.pipeline.transcribe_file(), passing the SHARED transcriber so
    the model is never reloaded per request.

    transcribe_file() has no progress hook, so progress is reported as coarse
    stages; the stage label is what actually tells the user where things are.
    """
    repo.update_job(job_id, status="running")
    try:
        from stt.pipeline import transcribe_file      # lazy import (numpy etc.)

        repo.update_job(job_id, stage="loading", progress=0.08)
        transcriber = _get_transcriber(model)         # weights load on first use

        out_dir.mkdir(parents=True, exist_ok=True)
        stem = out_dir / audio_path.stem

        repo.update_job(job_id, stage="transcribe", progress=0.25)
        with _stt_run_lock:                           # one transcription at a time
            result = transcribe_file(
                audio_path, out_stem=stem, model=model,
                language=language, transcriber=transcriber,
            )

        repo.update_job(job_id, stage="writing", progress=0.92)

        # The durable half: a transcript is the one thing this product keeps.
        # Stored as text + timestamped segments, which is what lets every
        # download format be rendered on demand later (PRD_MULTIUSER M5).
        job = repo.get_job(CURRENT_USER, job_id)
        transcript = repo.create_transcript(
            CURRENT_USER,
            job_id=job_id,
            title=job.title if job else audio_path.stem,
            content=result.text,
            segments=result.to_dict()["segments"],
            language=result.language,
            model=model,
            duration_seconds=result.duration,
            source_filename=audio_path.name,
        )

        repo.update_job(
            job_id,
            result={
                "transcript_id": transcript.id,
                "txt": str(stem.with_suffix(".txt")),
                "md": str(stem.with_suffix(".md")),
                "json": str(stem.with_suffix(".json")),
                "language": result.language,
                "duration": result.duration,
                "empty": transcript.is_empty,        # silent audio != a crash
            },
            status="done", progress=1.0, stage="done",
        )
    except Exception as e:
        repo.update_job(job_id, status="error", error=_friendly_stt_error(e))


# --- Lifecycle ---------------------------------------------------------------

@app.on_event("startup")
def _on_startup() -> None:
    """A job left 'queued'/'running' by a crash can never finish, so mark it
    errored rather than letting the UI poll it forever."""
    # Single-user dev mode against Postgres: the placeholder owner must exist
    # before anything can reference it. Removed at M3, when real sign-in
    # supplies genuine auth.users rows.
    ensure_dev_user = getattr(repo, "ensure_dev_user", None)
    if ensure_dev_user is not None:
        ensure_dev_user(CURRENT_USER)

    stranded = repo.fail_interrupted_jobs()
    if stranded:
        print(f"[startup] marked {stranded} interrupted job(s) as failed")


@app.on_event("shutdown")
def _on_shutdown() -> None:
    """Release the connection pool deliberately; leaving it to the garbage
    collector produces noise at interpreter shutdown."""
    close = getattr(repo, "close", None)
    if close is not None:
        close()


# --- Routes ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (APP_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/voices")
async def voices() -> list[dict]:
    """English Edge-TTS voices with their personality tags, for the dropdown."""
    global _voices_cache
    if _voices_cache is None:
        out = []
        for v in await edge_tts.list_voices():
            if not v["ShortName"].startswith("en-"):
                continue
            tag = v.get("VoiceTag") or {}
            out.append({
                "id": v["ShortName"],
                "gender": v["Gender"],
                "personalities": tag.get("VoicePersonalities", []) or [],
            })
        out.sort(key=lambda x: x["id"])
        _voices_cache = out
    return _voices_cache


@app.get("/sample")
async def sample(voice: str = DEFAULT_VOICE, rate: str = "") -> FileResponse:
    """Synthesize (or serve cached) the preview line for a voice + narration
    speed. Honors the same `rate` the conversion will use, so the preview
    matches what you'll actually get."""
    key = hashlib.md5(f"{voice}|{rate}|{SAMPLE_TEXT}".encode()).hexdigest()[:16]
    path = SAMPLE_DIR / f"{key}.mp3"
    if not path.exists() or path.stat().st_size == 0:
        engine = make_edge_engine(voice, rate=rate or None)
        try:
            await engine(SAMPLE_TEXT, path)          # ~1s network call
        except Exception as e:
            path.unlink(missing_ok=True)
            raise HTTPException(400, f"Could not synthesize preview: {e}")
    return FileResponse(path, media_type="audio/mpeg")


@app.post("/convert")
async def convert(
    pdf: UploadFile = File(...),
    voice: str = Form(DEFAULT_VOICE),
    rate: str = Form(""),
    title: str = Form(""),
    author: str = Form(""),
    full_format: str = Form(".mp3"),
) -> dict:
    if not (pdf.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a .pdf file.")

    job_id = uuid.uuid4().hex[:12]
    jdir = JOBS_DIR / job_id
    jdir.mkdir(parents=True, exist_ok=True)
    pdf_path = jdir / "input.pdf"
    with open(pdf_path, "wb") as f:
        shutil.copyfileobj(pdf.file, f)

    book_title = title or Path(pdf.filename).stem
    cfg = PipelineConfig(
        voice=voice, rate=rate or None, title=book_title,
        author=author or None, full_format=full_format,
    )
    job = repo.create_job(
        CURRENT_USER, "tts", book_title,
        source_filename=pdf.filename,
        params={"voice": voice, "rate": rate, "full_format": full_format,
                "author": author},
    )

    loop = asyncio.get_running_loop()
    loop.run_in_executor(_executor, _run_job, job.id, pdf_path, jdir / "out", cfg)
    return {"job_id": job.id}


@app.get("/stt/status")
def stt_status() -> dict:
    """Is speech-to-text usable on this machine? Lets the UI say so up front
    instead of failing only after an upload."""
    ready, detail = True, "ready"
    try:
        import faster_whisper  # noqa: F401
        import numpy           # noqa: F401
    except ImportError as e:
        ready, detail = False, (
            f"Missing '{e.name}'. Install with: "
            "./.venv/bin/python -m pip install -r requirements_stt.txt"
        )
    if ready and shutil.which("ffmpeg") is None:
        ready, detail = False, "FFmpeg not found on PATH (macOS: brew install ffmpeg)."
    return {
        "ready": ready, "detail": detail,
        "default_model": STT_DEFAULT_MODEL,
        "model_loaded": _transcriber is not None,
    }


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    model: str = Form(STT_DEFAULT_MODEL),
    language: str = Form(""),
    title: str = Form(""),
) -> dict:
    """Start an audio -> text job. Mirrors /convert: save upload, register a
    job, run it in the background, return the id to poll."""
    name = audio.filename or ""
    suffix = Path(name).suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise HTTPException(
            400,
            f"Unsupported audio type '{suffix or name}'. Try: "
            + ", ".join(sorted(s.lstrip('.') for s in AUDIO_SUFFIXES)),
        )

    job_id = uuid.uuid4().hex[:12]
    jdir = JOBS_DIR / job_id
    jdir.mkdir(parents=True, exist_ok=True)
    # Stem the saved file on the upload's name so outputs are named after it.
    safe_stem = Path(name).stem.replace("/", "_")[:60] or "audio"
    audio_path = jdir / f"{safe_stem}{suffix}"
    with open(audio_path, "wb") as f:                  # streamed copy: long files
        shutil.copyfileobj(audio.file, f)              # never sit in memory

    if audio_path.stat().st_size == 0:
        raise HTTPException(400, "That file is empty.")

    job = repo.create_job(
        CURRENT_USER, "stt", title or safe_stem,
        source_filename=name,
        params={"model": model, "language": language or None},
    )
    loop = asyncio.get_running_loop()
    loop.run_in_executor(
        _executor, _run_stt_job, job.id, audio_path, jdir / "out",
        model, language or None,                       # "" -> auto-detect
    )
    return {"job_id": job.id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    """Unchanged response shape — the frontend polls this every 1.5s and must
    not notice that the source moved from a dict to the repository."""
    j = _job_or_404(job_id)
    out = j.result or {}
    base = {
        "id": j.id, "title": j.title, "kind": j.kind, "status": j.status,
        "stage": j.stage, "progress": round(j.progress, 3), "error": j.error,
    }
    if j.kind == "stt":
        duration = out.get("duration")
        return base | {
            "has_txt": "txt" in out,
            "has_md": "md" in out,
            "has_json": "json" in out,
            "language": out.get("language"),
            "duration": round(duration, 2) if duration is not None else None,
            "empty": bool(out.get("empty", False)),
            "transcript_id": out.get("transcript_id"),
        }
    return base | {
        "chapters": [Path(c).name for c in out.get("chapters", [])],
        "has_full": bool(out.get("full")),
        "has_text_pdf": bool(out.get("text_pdf")),
    }


def _job_or_404(job_id: str) -> JobRecord:
    """Ownership is enforced in the repository: another user's job reads as
    absent rather than forbidden, which is also what we want once auth lands."""
    j = repo.get_job(CURRENT_USER, job_id)
    if not j:
        raise HTTPException(404, "No such job.")
    return j


@app.get("/jobs/{job_id}/full")
def download_full(job_id: str) -> FileResponse:
    j = _job_or_404(job_id)
    full = Path(j.result["full"]) if j.result.get("full") else None
    if not full or not full.exists():
        raise HTTPException(404, "Audiobook not ready.")
    return FileResponse(full, filename=full.name)


@app.get("/jobs/{job_id}/textpdf")
def download_textpdf(job_id: str) -> FileResponse:
    j = _job_or_404(job_id)
    pdf_path = Path(j.result["text_pdf"]) if j.result.get("text_pdf") else None
    if not pdf_path or not pdf_path.exists():
        raise HTTPException(404, "No text PDF for this job.")
    return FileResponse(pdf_path, filename=pdf_path.name)


@app.get("/jobs/{job_id}/transcript.{fmt}")
def download_transcript(job_id: str, fmt: str) -> FileResponse:
    """Download an STT transcript as .txt, .md, or .json."""
    j = _job_or_404(job_id)
    media = {"txt": "text/plain", "md": "text/markdown", "json": "application/json"}
    if fmt not in media:
        raise HTTPException(400, "Format must be one of: txt, md, json.")
    raw = j.result.get(fmt)
    path = Path(raw) if raw else None
    if not path or not path.exists():
        raise HTTPException(404, "Transcript not ready.")
    return FileResponse(path, media_type=media[fmt],
                        filename=f"{j.title}.{fmt}".replace("/", "_"))


@app.get("/jobs/{job_id}/chapters.zip")
def download_chapters(job_id: str) -> StreamingResponse:
    j = _job_or_404(job_id)
    chapters = [Path(c) for c in j.result.get("chapters", [])]
    if not chapters:
        raise HTTPException(404, "No chapters for this job.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:  # mp3 already compressed
        for c in chapters:
            if c.exists():
                z.write(c, c.name)
    buf.seek(0)
    fname = f"{j.title}_chapters.zip".replace(" ", "_")
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
