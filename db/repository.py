"""
db/repository.py — the persistence seam for the Book/Audio Suite.

Everything the web layer stores or reads goes through `Repository`. Two
implementations are planned:

  * MemoryRepository   — in this file. No database, no network. It is what the
                         app uses today (replacing the bare `_jobs` dict) and
                         what the test suite uses forever.
  * PostgresRepository — Supabase / Postgres, matching `db/schema.sql`.

Why a seam at all: it keeps the app runnable with no database (fast, offline
tests and local development), and it means the switch to Supabase is a
constructor change rather than an edit to every route.

Scope note, mirroring PRD_MULTIUSER.md: only STT produces durable content.
A TTS job is working state and is pruned once finished; it never yields a
library row.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal, Protocol

JobKind = Literal["tts", "stt"]
JobStatus = Literal["queued", "running", "done", "error"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# --- Records ------------------------------------------------------------------

@dataclass
class JobRecord:
    """In-flight processing state. Mirrors the `jobs` table and today's Job."""
    id: str
    user_id: str
    kind: JobKind
    title: str
    status: JobStatus = "queued"
    stage: str = ""
    progress: float = 0.0
    error: str | None = None
    source_filename: str | None = None
    params: dict = field(default_factory=dict)
    # Output locations produced by the run. For TTS these are local paths that
    # stay valid until the job is pruned; STT's durable output is a transcript
    # row, so this only carries its working files.
    result: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    completed_at: datetime | None = None


@dataclass
class TranscriptRecord:
    """A library entry — the only durable user content in the product.

    `segments` is the source of truth for every download format: .txt/.md come
    from `content`, and .json/.srt/.vtt are rendered from these timings. Storing
    the structure rather than rendered files means a format added later works
    for transcripts that already exist.
    """
    id: str
    user_id: str
    title: str
    content: str
    segments: list[dict] = field(default_factory=list)
    job_id: str | None = None
    language: str | None = None
    model: str | None = None
    duration_seconds: float | None = None
    word_count: int | None = None
    source_filename: str | None = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    @property
    def is_empty(self) -> bool:
        """True when the audio held no detectable speech."""
        return not self.content.strip()


@dataclass
class Profile:
    """A user plus the form defaults we remember for them."""
    id: str
    display_name: str | None = None
    default_voice: str = "en-US-AndrewNeural"
    default_rate: str = "-12%"
    default_format: str = ".mp3"
    default_stt_model: str = "base"
    default_stt_language: str | None = None
    monthly_minutes_quota: int = 600


@dataclass
class Usage:
    """Quota accounting. With no files stored, the scarce resource is CPU time,
    so usage is measured in transcribed minutes rather than bytes."""
    user_id: str
    minutes_this_month: float
    monthly_minutes_quota: int
    transcript_count: int

    @property
    def over_quota(self) -> bool:
        return self.minutes_this_month >= self.monthly_minutes_quota


# --- The seam -----------------------------------------------------------------

class Repository(Protocol):
    """Every persistence operation the web layer needs.

    Implementations must treat `user_id` as mandatory on every read: the
    background worker runs with credentials that bypass row-level security, so
    isolation cannot be left to the database alone.
    """

    # profiles
    def get_profile(self, user_id: str) -> Profile: ...
    def update_profile(self, user_id: str, **fields) -> Profile: ...
    def get_usage(self, user_id: str) -> Usage: ...

    # jobs
    def create_job(self, user_id: str, kind: JobKind, title: str,
                   source_filename: str | None = None,
                   params: dict | None = None) -> JobRecord: ...
    def get_job(self, user_id: str, job_id: str) -> JobRecord | None: ...
    def update_job(self, job_id: str, **fields) -> None: ...
    def fail_interrupted_jobs(self) -> int:
        """Mark jobs left 'queued'/'running' by a crash as errored. Call once at
        startup so the UI never shows a job that can never finish."""
        ...
    def prune_stale_jobs(self, older_than: timedelta = timedelta(days=7)) -> int: ...

    # transcripts — the library
    def create_transcript(self, user_id: str, **fields) -> TranscriptRecord: ...
    def get_transcript(self, user_id: str, transcript_id: str) -> TranscriptRecord | None: ...
    def list_transcripts(self, user_id: str, *, query: str | None = None,
                         limit: int = 50, offset: int = 0) -> list[TranscriptRecord]: ...
    def rename_transcript(self, user_id: str, transcript_id: str, title: str) -> bool: ...
    def delete_transcript(self, user_id: str, transcript_id: str) -> bool: ...


# --- Memory implementation ----------------------------------------------------

class MemoryRepository:
    """Process-local Repository. No database, no network.

    Used by the tests and by local development. Its one real limitation is the
    obvious one: nothing survives a restart, which is precisely the defect
    PostgresRepository exists to fix.

    Thread-safe because the web layer mutates jobs from pool workers while the
    event loop reads them for polling.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._profiles: dict[str, Profile] = {}
        self._jobs: dict[str, JobRecord] = {}
        self._transcripts: dict[str, TranscriptRecord] = {}

    # profiles
    def get_profile(self, user_id: str) -> Profile:
        with self._lock:
            return self._profiles.setdefault(user_id, Profile(id=user_id))

    def update_profile(self, user_id: str, **fields) -> Profile:
        with self._lock:
            current = self.get_profile(user_id)
            updated = replace(current, **fields)
            self._profiles[user_id] = updated
            return updated

    def get_usage(self, user_id: str) -> Usage:
        with self._lock:
            month_start = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            mine = [t for t in self._transcripts.values() if t.user_id == user_id]
            minutes = sum((t.duration_seconds or 0) for t in mine
                          if t.created_at >= month_start) / 60.0
            return Usage(
                user_id=user_id,
                minutes_this_month=round(minutes, 1),
                monthly_minutes_quota=self.get_profile(user_id).monthly_minutes_quota,
                transcript_count=len(mine),
            )

    # jobs
    def create_job(self, user_id: str, kind: JobKind, title: str,
                   source_filename: str | None = None,
                   params: dict | None = None) -> JobRecord:
        with self._lock:
            job = JobRecord(
                id=_new_id(), user_id=user_id, kind=kind, title=title,
                source_filename=source_filename, params=params or {},
            )
            self._jobs[job.id] = job
            return job

    def get_job(self, user_id: str, job_id: str) -> JobRecord | None:
        with self._lock:
            job = self._jobs.get(job_id)
            # Ownership is checked here, not left to the caller.
            return job if job and job.user_id == user_id else None

    def update_job(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for k, v in fields.items():
                setattr(job, k, v)
            job.updated_at = _now()
            if job.status in ("done", "error") and job.completed_at is None:
                job.completed_at = job.updated_at

    def fail_interrupted_jobs(self) -> int:
        with self._lock:
            n = 0
            for job in self._jobs.values():
                if job.status in ("queued", "running"):
                    job.status = "error"
                    job.error = "Interrupted by a server restart. Please run it again."
                    job.completed_at = _now()
                    n += 1
            return n

    def prune_stale_jobs(self, older_than: timedelta = timedelta(days=7)) -> int:
        with self._lock:
            cutoff = _now() - older_than
            stale = [
                jid for jid, j in self._jobs.items()
                if j.status in ("done", "error")
                and (j.completed_at or j.updated_at) < cutoff
            ]
            for jid in stale:
                del self._jobs[jid]
            return len(stale)

    # transcripts
    def create_transcript(self, user_id: str, **fields) -> TranscriptRecord:
        with self._lock:
            content = fields.pop("content", "")
            record = TranscriptRecord(
                id=_new_id(), user_id=user_id, content=content,
                word_count=fields.pop("word_count", None) or len(content.split()),
                **fields,
            )
            self._transcripts[record.id] = record
            return record

    def get_transcript(self, user_id: str, transcript_id: str) -> TranscriptRecord | None:
        with self._lock:
            t = self._transcripts.get(transcript_id)
            return t if t and t.user_id == user_id else None

    def list_transcripts(self, user_id: str, *, query: str | None = None,
                         limit: int = 50, offset: int = 0) -> list[TranscriptRecord]:
        with self._lock:
            mine: Iterable[TranscriptRecord] = (
                t for t in self._transcripts.values() if t.user_id == user_id
            )
            if query:
                q = query.lower()
                mine = (t for t in mine
                        if q in t.title.lower() or q in t.content.lower())
            ordered = sorted(mine, key=lambda t: t.created_at, reverse=True)
            return ordered[offset:offset + limit]

    def rename_transcript(self, user_id: str, transcript_id: str, title: str) -> bool:
        with self._lock:
            t = self.get_transcript(user_id, transcript_id)
            if t is None:
                return False
            t.title = title
            t.updated_at = _now()
            return True

    def delete_transcript(self, user_id: str, transcript_id: str) -> bool:
        with self._lock:
            if self.get_transcript(user_id, transcript_id) is None:
                return False
            del self._transcripts[transcript_id]
            return True


# --- Postgres implementation (M2) --------------------------------------------

class PostgresRepository:
    """Postgres / Supabase implementation of `Repository` — see db/schema.sql.

    Connection handling: a psycopg_pool ConnectionPool, because the web layer
    reads jobs from the event loop while pool workers write to them; sharing one
    connection across threads is not safe.

    Two rules this class exists to enforce:

    * EVERY read filters by user_id in SQL. The worker connects with the service
      role, which bypasses row-level security by design — RLS defends against a
      careless client query, never a careless server-side one.
    * `content` is never logged. Transcripts are routinely sensitive.
    """

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8,
                 connect_timeout: float = 5.0) -> None:
        try:
            from psycopg_pool import ConnectionPool
            from psycopg.rows import dict_row
        except ImportError as e:                     # keep the app bootable
            raise RuntimeError(
                "PostgresRepository needs psycopg. Install it with: "
                "./.venv/bin/python -m pip install 'psycopg[binary,pool]'"
            ) from e
        self._dict_row = dict_row
        # open=False + an explicit wait so an unreachable database raises here,
        # at startup, instead of retrying on a background thread and surfacing
        # as a confusing error much later.
        self._pool = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=False,
            kwargs={"row_factory": dict_row},
        )
        try:
            self._pool.open(wait=True, timeout=connect_timeout)
        except Exception:
            self._pool.close()
            raise

    # -- plumbing -------------------------------------------------------------

    def _fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _execute(self, sql: str, params: tuple = ()) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def close(self) -> None:
        self._pool.close()

    # -- row -> record --------------------------------------------------------

    @staticmethod
    def _job(row: dict) -> JobRecord:
        return JobRecord(
            id=str(row["id"]), user_id=str(row["user_id"]), kind=row["kind"],
            title=row["title"], status=row["status"], stage=row["stage"] or "",
            progress=float(row["progress"]), error=row["error"],
            source_filename=row["source_filename"],
            params=row["params"] or {}, result=row["result"] or {},
            created_at=row["created_at"], updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _transcript(row: dict) -> TranscriptRecord:
        return TranscriptRecord(
            id=str(row["id"]), user_id=str(row["user_id"]),
            title=row["title"], content=row["content"],
            segments=row["segments"] or [],
            job_id=str(row["job_id"]) if row["job_id"] else None,
            language=row["language"], model=row["model"],
            duration_seconds=row["duration_seconds"], word_count=row["word_count"],
            source_filename=row["source_filename"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    # -- dev convenience ------------------------------------------------------

    def ensure_dev_user(self, user_id: str) -> None:
        """Create the placeholder auth.users row that single-user local runs
        write against. A stopgap for the pre-auth milestones: once real sign-in
        lands (M3) Supabase owns auth.users and this is never called.
        """
        self._execute(
            "insert into auth.users (id, email) values (%s, %s) "
            "on conflict (id) do nothing",
            (user_id, f"dev+{user_id[:8]}@localhost"),
        )
        self.get_profile(user_id)          # trigger normally does this; be safe

    # -- profiles -------------------------------------------------------------

    def get_profile(self, user_id: str) -> Profile:
        row = self._fetchone("select * from profiles where id = %s", (user_id,))
        if row is None:
            # The auth trigger normally creates this; insert defensively so a
            # profile fetch can never 500 on a freshly-created user.
            row = self._fetchone(
                "insert into profiles (id) values (%s) "
                "on conflict (id) do update set id = excluded.id returning *",
                (user_id,),
            )
        return Profile(
            id=str(row["id"]), display_name=row["display_name"],
            default_voice=row["default_voice"], default_rate=row["default_rate"],
            default_format=row["default_format"],
            default_stt_model=row["default_stt_model"],
            default_stt_language=row["default_stt_language"],
            monthly_minutes_quota=row["monthly_minutes_quota"],
        )

    def update_profile(self, user_id: str, **fields) -> Profile:
        allowed = {
            "display_name", "default_voice", "default_rate", "default_format",
            "default_stt_model", "default_stt_language", "monthly_minutes_quota",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_profile(user_id)
        # Column names come from the allow-list above, never from user input.
        sets = ", ".join(f"{k} = %s" for k in updates)
        self._execute(f"update profiles set {sets} where id = %s",
                      (*updates.values(), user_id))
        return self.get_profile(user_id)

    def get_usage(self, user_id: str) -> Usage:
        row = self._fetchone("select * from user_usage where user_id = %s", (user_id,))
        if row is None:
            self.get_profile(user_id)                     # create, then re-read
            row = self._fetchone("select * from user_usage where user_id = %s", (user_id,))
        return Usage(
            user_id=str(row["user_id"]),
            minutes_this_month=float(row["minutes_this_month"] or 0),
            monthly_minutes_quota=row["monthly_minutes_quota"],
            transcript_count=row["transcript_count"],
        )

    # -- jobs -----------------------------------------------------------------

    def create_job(self, user_id: str, kind: JobKind, title: str,
                   source_filename: str | None = None,
                   params: dict | None = None) -> JobRecord:
        from psycopg.types.json import Jsonb
        row = self._fetchone(
            "insert into jobs (user_id, kind, title, source_filename, params) "
            "values (%s, %s, %s, %s, %s) returning *",
            (user_id, kind, title, source_filename, Jsonb(params or {})),
        )
        return self._job(row)

    def get_job(self, user_id: str, job_id: str) -> JobRecord | None:
        # user_id in the WHERE clause is the isolation guarantee for the worker,
        # which bypasses RLS. Another user's job reads as absent.
        row = self._fetchone(
            "select * from jobs where id = %s and user_id = %s", (job_id, user_id))
        return self._job(row) if row else None

    def update_job(self, job_id: str, **fields) -> None:
        from psycopg.types.json import Jsonb
        allowed = {"status", "stage", "progress", "error", "result", "title"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if "result" in updates:
            updates["result"] = Jsonb(updates["result"])
        sets = ", ".join(f"{k} = %s" for k in updates)          # keys allow-listed
        # completed_at is set the first time a job reaches a terminal state.
        terminal = ", completed_at = coalesce(completed_at, now())" \
            if updates.get("status") in ("done", "error") else ""
        self._execute(f"update jobs set {sets}{terminal} where id = %s",
                      (*updates.values(), job_id))

    def fail_interrupted_jobs(self) -> int:
        row = self._fetchone("select fail_interrupted_jobs() as n")
        return int(row["n"])

    def prune_stale_jobs(self, older_than: timedelta = timedelta(days=7)) -> int:
        row = self._fetchone("select prune_stale_jobs(%s) as n", (older_than,))
        return int(row["n"])

    # -- transcripts ----------------------------------------------------------

    def create_transcript(self, user_id: str, **fields) -> TranscriptRecord:
        from psycopg.types.json import Jsonb
        content = fields.get("content", "") or ""
        row = self._fetchone(
            "insert into transcripts (user_id, job_id, title, content, segments, "
            "language, model, duration_seconds, word_count, source_filename) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *",
            (
                user_id, fields.get("job_id"), fields.get("title") or "Untitled",
                content, Jsonb(fields.get("segments") or []),
                fields.get("language"), fields.get("model"),
                fields.get("duration_seconds"),
                fields.get("word_count") or len(content.split()),
                fields.get("source_filename"),
            ),
        )
        return self._transcript(row)

    def get_transcript(self, user_id: str, transcript_id: str) -> TranscriptRecord | None:
        row = self._fetchone(
            "select * from transcripts where id = %s and user_id = %s",
            (transcript_id, user_id))
        return self._transcript(row) if row else None

    def list_transcripts(self, user_id: str, *, query: str | None = None,
                         limit: int = 50, offset: int = 0) -> list[TranscriptRecord]:
        if query:
            # Uses the search_vector GIN index rather than a scanning ILIKE.
            rows = self._fetchall(
                "select * from transcripts "
                " where user_id = %s and search_vector @@ plainto_tsquery('english', %s) "
                " order by ts_rank(search_vector, plainto_tsquery('english', %s)) desc, "
                "          created_at desc "
                " limit %s offset %s",
                (user_id, query, query, limit, offset))
        else:
            rows = self._fetchall(
                "select * from transcripts where user_id = %s "
                " order by created_at desc limit %s offset %s",
                (user_id, limit, offset))
        return [self._transcript(r) for r in rows]

    def rename_transcript(self, user_id: str, transcript_id: str, title: str) -> bool:
        return self._execute(
            "update transcripts set title = %s where id = %s and user_id = %s",
            (title, transcript_id, user_id)) > 0

    def delete_transcript(self, user_id: str, transcript_id: str) -> bool:
        return self._execute(
            "delete from transcripts where id = %s and user_id = %s",
            (transcript_id, user_id)) > 0


# --- Smoke test ---------------------------------------------------------------

if __name__ == "__main__":
    repo = MemoryRepository()
    alice, bob = "user-alice", "user-bob"

    job = repo.create_job(alice, "stt", "Team standup", params={"model": "base"})
    repo.update_job(job.id, status="running", stage="transcribe", progress=0.4)
    repo.update_job(job.id, status="done", progress=1.0)

    repo.create_transcript(
        alice, job_id=job.id, title="Team standup",
        content="We shipped the transcript library today.",
        segments=[{"start": 0.0, "end": 3.2, "text": "We shipped the transcript library today."}],
        language="en", model="base", duration_seconds=3.2,
    )
    repo.create_transcript(bob, title="Bob's private notes", content="Secret.",
                           language="en", duration_seconds=60.0)

    print("alice library:", [t.title for t in repo.list_transcripts(alice)])
    print("bob library  :", [t.title for t in repo.list_transcripts(bob)])
    print("search 'library':", [t.title for t in repo.list_transcripts(alice, query="library")])

    # The isolation the whole design turns on.
    bobs = repo.list_transcripts(bob)[0]
    print("alice reading bob's transcript by id:", repo.get_transcript(alice, bobs.id))
    print("alice's job seen by bob:", repo.get_job(bob, job.id))

    print("usage:", repo.get_usage(alice))
    print("interrupted jobs failed:", repo.fail_interrupted_jobs())
