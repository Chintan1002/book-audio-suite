"""
drive.py — save a finished job's artifacts into the user's own Google Drive.

The storage model in one line: **we store nothing, Google does.** The app writes
each user's creations into *their* Drive; our "library" is a redirect to that
folder. See STORAGE_TASK.md for the full spec.

Scope is `drive.file` ONLY, which means this code can see and search *nothing
but the items it created itself*. That is what makes the design safe to offer:
the app is structurally incapable of reading the rest of someone's Drive.

Layout it maintains (find-or-create, never cached):

    Book·Audio Suite/
    ├── Audiobooks/
    │   └── <Title> — <YYYY-MM-DD>/     one folder per conversion
    │       ├── <Title>.mp3
    │       ├── <Title>_text.pdf        only when OCR ran
    │       └── Chapters/
    └── Transcripts/
        └── <Title> — <YYYY-MM-DD>/

No dependencies beyond the standard library, so this module is runnable on its
own:

    python drive.py <access_token> --smoke
    python drive.py <access_token> --upload path/to/file.mp3 --title "My Book"
"""

from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Literal

DRIVE_API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"

ROOT_FOLDER_NAME = "Book·Audio Suite"
AUDIOBOOKS_FOLDER = "Audiobooks"
TRANSCRIPTS_FOLDER = "Transcripts"
CHAPTERS_FOLDER = "Chapters"

# Google requires resumable chunks to be a multiple of 256 KiB. 8 MiB keeps the
# request count sane for a 300 MB audiobook without hoarding memory.
CHUNK_SIZE = 8 * 1024 * 1024
MAX_RETRIES = 5


class DriveError(RuntimeError):
    """Any Drive API failure, with Google's own message preserved."""


# --- HTTP --------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Resumable uploads answer 308 'Resume Incomplete', which urllib would
    otherwise try to FOLLOW as a redirect. We need to read it, not chase it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _http(method: str, url: str, *, token: str | None = None,
          headers: dict | None = None, body: bytes | None = None,
          timeout: float = 60.0) -> tuple[int, dict, bytes]:
    """One request. Returns (status, headers, body) and never raises on a
    non-2xx that the caller is expected to interpret (e.g. 308, 404)."""
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def _json_or_raise(status: int, raw: bytes, what: str) -> dict:
    if 200 <= status < 300:
        return json.loads(raw) if raw else {}
    try:                                     # surface Google's own wording
        detail = json.loads(raw)["error"]["message"]
    except Exception:
        detail = raw.decode("utf-8", "replace")[:300]
    raise DriveError(f"{what} failed (HTTP {status}): {detail}")


def _escape(value: str) -> str:
    """Escape a literal for a Drive query string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


# Python's mimetypes doesn't know .m4b, and Drive shows an octet-stream as an
# unplayable blob instead of offering its audio preview — so pin the formats we
# actually produce.
_MIME_OVERRIDES = {
    ".m4b": "audio/mp4",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".md": "text/markdown",
    ".srt": "text/plain",
    ".vtt": "text/vtt",
}


def _guess_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[suffix]
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def safe_component(name: str) -> str:
    """Drive tolerates most characters, but '/' reads as a path separator in
    many clients. Keep names human — they are the only thing the user browses
    by — while removing what breaks."""
    cleaned = name.replace("/", "-").replace("\\", "-").strip()
    return (cleaned or "Untitled")[:120]


# --- Results ------------------------------------------------------------------

@dataclass
class SavedFile:
    name: str
    file_id: str
    bytes: int


@dataclass
class SaveResult:
    """A save is many uploads, so partial success is a real outcome and is
    reported rather than collapsed into a single pass/fail."""
    folder_id: str
    folder_url: str
    saved: list[SavedFile] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)   # (name, error)

    @property
    def ok(self) -> bool:
        return not self.failed


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


# --- Client -------------------------------------------------------------------

class DriveClient:
    """Talks to Drive with a short-lived `drive.file` access token.

    The token is held for the life of this object and never written anywhere —
    not to disk, not to a database, not to a log line.
    """

    def __init__(self, access_token: str, *, timeout: float = 60.0) -> None:
        if not access_token:
            raise DriveError("A Google access token is required.")
        self._token = access_token
        self._timeout = timeout

    # -- queries ---------------------------------------------------------------

    def _search(self, query: str, *, page_size: int = 100) -> list[dict]:
        params = urllib.parse.urlencode({
            "q": query,
            "fields": "files(id,name,mimeType,createdTime)",
            "pageSize": page_size,
            "orderBy": "createdTime",          # oldest first: pick the original
            "spaces": "drive",
        })
        status, _, raw = _http("GET", f"{DRIVE_API}/files?{params}",
                               token=self._token, timeout=self._timeout)
        return _json_or_raise(status, raw, "Drive search").get("files", [])

    def find_folder(self, name: str, parent: str | None = None) -> str | None:
        """Find a folder this app created. Note there is deliberately NO parent
        constraint on the root lookup: the user may have moved our folder, and
        `drive.file` can only see our own items anyway."""
        clauses = [
            f"name = '{_escape(name)}'",
            f"mimeType = '{FOLDER_MIME}'",
            "trashed = false",
        ]
        if parent:
            clauses.append(f"'{parent}' in parents")
        found = self._search(" and ".join(clauses))
        return found[0]["id"] if found else None

    def create_folder(self, name: str, parent: str | None = None) -> str:
        meta: dict = {"name": name, "mimeType": FOLDER_MIME}
        if parent:
            meta["parents"] = [parent]
        status, _, raw = _http(
            "POST", f"{DRIVE_API}/files?fields=id",
            token=self._token,
            headers={"Content-Type": "application/json"},
            body=json.dumps(meta).encode(),
            timeout=self._timeout,
        )
        return _json_or_raise(status, raw, f"Creating folder '{name}'")["id"]

    def find_or_create_folder(self, name: str, parent: str | None = None) -> str:
        existing = self.find_folder(name, parent)
        if existing:
            return existing
        created = self.create_folder(name, parent)
        # Two concurrent first-saves can both create the folder. Re-search and
        # keep the oldest so everyone converges on one.
        winner = self.find_folder(name, parent)
        return winner or created

    # -- structure -------------------------------------------------------------

    def ensure_structure(self) -> dict[str, str]:
        """Find-or-create the root and its two feature folders. Nothing is
        cached: the IDs are rederived from Drive on every call, which is what
        keeps this design free of stored folder state."""
        root = self.find_or_create_folder(ROOT_FOLDER_NAME)
        return {
            "root": root,
            "audiobooks": self.find_or_create_folder(AUDIOBOOKS_FOLDER, root),
            "transcripts": self.find_or_create_folder(TRANSCRIPTS_FOLDER, root),
        }

    def ensure_conversion_folder(
        self, kind: Literal["tts", "stt"], title: str,
        when: date | None = None, structure: dict | None = None,
    ) -> str:
        """One folder per conversion, named '<Title> — <YYYY-MM-DD>'.

        Find-or-create, so re-saving the same job updates that folder instead of
        creating a second one. Two conversions sharing a title on the same day
        share a folder — an accepted trade for names that stay readable.
        """
        structure = structure or self.ensure_structure()
        parent = structure["audiobooks"] if kind == "tts" else structure["transcripts"]
        stamp = (when or date.today()).isoformat()
        return self.find_or_create_folder(
            f"{safe_component(title)} — {stamp}", parent)

    # -- upload ----------------------------------------------------------------

    def upload_resumable(
        self, path: str | Path, parent_id: str, *,
        name: str | None = None, mime: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> SavedFile:
        """Upload one file with Drive's resumable protocol.

        Resumable rather than simple upload because an audiobook can be hundreds
        of megabytes: a dropped connection resumes from the last acknowledged
        byte instead of restarting.
        """
        src = Path(path)
        if not src.is_file():
            raise DriveError(f"No such file to upload: {src}")
        total = src.stat().st_size
        filename = safe_component(name or src.name)
        content_type = mime or _guess_mime(src)

        session_url = self._begin_session(filename, parent_id, content_type, total)

        uploaded = 0
        attempts = 0
        with open(src, "rb") as fh:
            while uploaded < total:
                fh.seek(uploaded)
                chunk = fh.read(min(CHUNK_SIZE, total - uploaded))
                end = uploaded + len(chunk) - 1
                try:
                    status, headers, raw = _http(
                        "PUT", session_url,
                        headers={
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {uploaded}-{end}/{total}",
                        },
                        body=chunk, timeout=max(self._timeout, 300.0),
                    )
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    attempts += 1
                    if attempts > MAX_RETRIES:
                        raise DriveError(f"Upload of '{filename}' failed: {e}") from e
                    time.sleep(min(2 ** attempts, 30))
                    uploaded = self._session_progress(session_url, total)
                    continue

                if status in (200, 201):
                    info = _json_or_raise(status, raw, "Upload")
                    if on_progress:
                        on_progress(total, total)
                    return SavedFile(filename, info["id"], total)

                if status == 308:               # 'Resume Incomplete' — keep going
                    attempts = 0
                    uploaded = self._parse_range(headers, fallback=uploaded + len(chunk))
                    if on_progress:
                        on_progress(uploaded, total)
                    continue

                if status in (500, 502, 503, 504):
                    attempts += 1
                    if attempts > MAX_RETRIES:
                        raise DriveError(
                            f"Upload of '{filename}' failed after {MAX_RETRIES} "
                            f"retries (HTTP {status}).")
                    time.sleep(min(2 ** attempts, 30))
                    uploaded = self._session_progress(session_url, total)
                    continue

                _json_or_raise(status, raw, f"Uploading '{filename}'")

        raise DriveError(f"Upload of '{filename}' ended without confirmation.")

    def _begin_session(self, name: str, parent_id: str,
                       content_type: str, total: int) -> str:
        meta = {"name": name, "parents": [parent_id]}
        status, headers, raw = _http(
            "POST", f"{UPLOAD_API}/files?uploadType=resumable",
            token=self._token,
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": content_type,
                "X-Upload-Content-Length": str(total),
            },
            body=json.dumps(meta).encode(),
            timeout=self._timeout,
        )
        if status not in (200, 201):
            _json_or_raise(status, raw, f"Starting upload of '{name}'")
        location = headers.get("Location") or headers.get("location")
        if not location:
            raise DriveError(f"Drive did not return an upload session for '{name}'.")
        return location

    def _session_progress(self, session_url: str, total: int) -> int:
        """Ask Drive how much it actually has, so a retry resumes instead of
        restarting."""
        status, headers, _ = _http(
            "PUT", session_url,
            headers={"Content-Length": "0", "Content-Range": f"bytes */{total}"},
            timeout=self._timeout,
        )
        if status in (200, 201):
            return total
        return self._parse_range(headers, fallback=0)

    @staticmethod
    def _parse_range(headers: dict, *, fallback: int) -> int:
        """Drive reports progress as 'Range: bytes=0-N' (N is inclusive)."""
        rng = headers.get("Range") or headers.get("range")
        if not rng or "-" not in rng:
            return fallback
        try:
            return int(rng.split("-")[-1]) + 1
        except ValueError:
            return fallback

    # -- the whole job ---------------------------------------------------------

    def save_artifacts(
        self, kind: Literal["tts", "stt"], title: str,
        files: Iterable[tuple[str, Path]], *, when: date | None = None,
        on_file: Callable[[str, int, int], None] | None = None,
    ) -> SaveResult:
        """Save every artifact of one conversion into its own dated folder.

        `files` is (subfolder, path) pairs — subfolder '' means the conversion
        folder itself, 'Chapters' nests one level deeper so 19 chapter files
        don't bury the audiobook and the text PDF.

        One failed file does not abandon the rest: it is recorded and the save
        continues, because losing 20 uploads to one bad byte would be absurd.
        """
        structure = self.ensure_structure()
        conv = self.ensure_conversion_folder(kind, title, when, structure)
        result = SaveResult(folder_id=conv, folder_url=folder_url(conv))

        items = list(files)
        subfolders: dict[str, str] = {}
        for index, (sub, path) in enumerate(items, start=1):
            if on_file:
                on_file(Path(path).name, index, len(items))
            try:
                parent = conv
                if sub:
                    if sub not in subfolders:
                        subfolders[sub] = self.find_or_create_folder(sub, conv)
                    parent = subfolders[sub]
                result.saved.append(self.upload_resumable(path, parent))
            except Exception as e:                       # keep going
                result.failed.append((Path(path).name, str(e)))
        return result


# --- CLI / smoke test ---------------------------------------------------------

def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Save files into a user's Google Drive.")
    ap.add_argument("token", help="a drive.file access token")
    ap.add_argument("--smoke", action="store_true",
                    help="create the folder structure and report the IDs")
    ap.add_argument("--upload", nargs="*", default=[],
                    help="file(s) to upload into a conversion folder")
    ap.add_argument("--kind", choices=["tts", "stt"], default="stt")
    ap.add_argument("--title", default="Drive Smoke Test")
    args = ap.parse_args()

    client = DriveClient(args.token)

    structure = client.ensure_structure()
    print("folder structure:")
    for label, fid in structure.items():
        print(f"  {label:<12} {fid}")
    print("library url:", folder_url(structure["root"]))

    # Idempotency is the property worth proving: a second call must return the
    # same ids rather than creating duplicates.
    again = client.ensure_structure()
    print("stable on re-run:", again == structure)

    if args.upload:
        files = [("", Path(p)) for p in args.upload]
        res = client.save_artifacts(
            args.kind, args.title, files,
            on_file=lambda n, i, t: print(f"  uploading {i}/{t}: {n}"),
        )
        print(f"saved {len(res.saved)} file(s), {len(res.failed)} failed")
        for f in res.saved:
            print(f"  ok   {f.name}  ({f.bytes:,} bytes)  {f.file_id}")
        for name, err in res.failed:
            print(f"  FAIL {name}: {err}")
        print("conversion folder:", res.folder_url)


if __name__ == "__main__":
    _main()
