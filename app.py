"""
app.py — FastAPI web layer over the PDF -> Audiobook pipeline.

The pipeline takes minutes (rate-limited TTS), so this uses an async job model:
upload starts a background job, the browser polls for progress, then downloads.

    GET  /                      -> the single-page UI (index.html)
    GET  /voices                -> English Edge-TTS voices (for the dropdown)
    POST /convert               -> {job_id}; starts a background conversion
    GET  /jobs/{id}             -> {status, stage, progress, ...}
    GET  /jobs/{id}/full        -> the whole-book audiobook
    GET  /jobs/{id}/chapters.zip-> all per-chapter mp3s, zipped
    GET  /jobs/{id}/textpdf     -> the OCR text PDF (scanned books only)

v1 is local/single-machine: jobs run in a small thread pool and live in memory.
Upgrade path (queue + persistent store) is noted where relevant.

Run:  ./.venv/bin/uvicorn app:app --reload    then open http://localhost:8000
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import shutil
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import edge_tts
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from pipeline import PipelineConfig, run_pipeline
from tts import DEFAULT_VOICE, make_edge_engine

APP_DIR = Path(__file__).parent
JOBS_DIR = APP_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

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

app = FastAPI(title="Audiobook Creator")

# Small pool: conversions are long; a couple in parallel is plenty for local use.
_executor = ThreadPoolExecutor(max_workers=2)
_jobs: dict[str, "Job"] = {}
_voices_cache: list[dict] | None = None


# --- Job registry (in-memory; fine for a single local machine) ---------------

@dataclass
class Job:
    id: str
    title: str
    status: str = "queued"       # queued | running | done | error
    stage: str = ""
    progress: float = 0.0        # 0..1
    error: str | None = None
    full: Path | None = None
    chapters: list[Path] = field(default_factory=list)
    text_pdf: Path | None = None


def _run_job(job_id: str, pdf_path: Path, out_dir: Path, cfg: PipelineConfig) -> None:
    """Runs in a worker thread. run_pipeline calls asyncio.run internally, which
    is safe here because a thread-pool worker has no running event loop."""
    job = _jobs[job_id]
    job.status = "running"

    def progress(stage: str, frac: float) -> None:
        job.stage, job.progress = stage, frac

    cfg.progress = progress
    try:
        r = run_pipeline(pdf_path, out_dir, cfg)
        job.full = r.full_audio
        job.chapters = r.chapter_files
        job.text_pdf = r.text_pdf
        job.status, job.progress, job.stage = "done", 1.0, "done"
    except Exception as e:  # surface to the UI instead of dying silently
        job.status = "error"
        job.error = f"{type(e).__name__}: {e}"


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
    _jobs[job_id] = Job(id=job_id, title=book_title)

    loop = asyncio.get_running_loop()
    loop.run_in_executor(_executor, _run_job, job_id, pdf_path, jdir / "out", cfg)
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    j = _jobs.get(job_id)
    if not j:
        raise HTTPException(404, "No such job.")
    return {
        "id": j.id, "title": j.title, "status": j.status, "stage": j.stage,
        "progress": round(j.progress, 3), "error": j.error,
        "chapters": [p.name for p in j.chapters],
        "has_full": j.full is not None,
        "has_text_pdf": j.text_pdf is not None,
    }


def _job_or_404(job_id: str) -> Job:
    j = _jobs.get(job_id)
    if not j:
        raise HTTPException(404, "No such job.")
    return j


@app.get("/jobs/{job_id}/full")
def download_full(job_id: str) -> FileResponse:
    j = _job_or_404(job_id)
    if not j.full or not j.full.exists():
        raise HTTPException(404, "Audiobook not ready.")
    return FileResponse(j.full, filename=j.full.name)


@app.get("/jobs/{job_id}/textpdf")
def download_textpdf(job_id: str) -> FileResponse:
    j = _job_or_404(job_id)
    if not j.text_pdf or not j.text_pdf.exists():
        raise HTTPException(404, "No text PDF for this job.")
    return FileResponse(j.text_pdf, filename=j.text_pdf.name)


@app.get("/jobs/{job_id}/chapters.zip")
def download_chapters(job_id: str) -> StreamingResponse:
    j = _job_or_404(job_id)
    if not j.chapters:
        raise HTTPException(404, "No chapters for this job.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:  # mp3 already compressed
        for c in j.chapters:
            if c.exists():
                z.write(c, c.name)
    buf.seek(0)
    fname = f"{j.title}_chapters.zip".replace(" ", "_")
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
