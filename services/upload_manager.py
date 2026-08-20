"""Resumable chunked-upload session management.

Lets a browser upload a large media file in small slices that can be retried or
resumed independently, instead of one giant multipart POST that a proxy can time
out. Each session writes to a single `.part` file on disk; on completion the file
is handed off to the existing local-file transcription job path.

State is held in memory (mirroring JobManager). Sessions do not survive a process
restart — orphaned `.part` files are cleared on boot by main.py's lifespan.
"""
import os
import uuid
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Dict, Optional


class UploadError(Exception):
    """Raised for invalid upload operations (unknown id, misaligned offset, etc.)."""


class UploadManager:
    """Manages resumable upload sessions and their on-disk `.part` files."""

    def __init__(self, uploads_dir: str):
        self.uploads_dir = uploads_dir
        os.makedirs(self.uploads_dir, exist_ok=True)
        self.sessions: Dict[str, dict] = {}
        self.lock = Lock()
        # One lock per upload so concurrent chunk writes to the SAME file are
        # serialized, while different uploads proceed independently.
        self._file_locks: Dict[str, Lock] = {}

    def _part_path(self, upload_id: str, ext: str) -> str:
        return os.path.join(self.uploads_dir, f"{upload_id}{ext}")

    def init(
        self,
        filename: str,
        total_size: Optional[int],
        archive_bucket: Optional[str] = None,
    ) -> dict:
        """Create a new upload session and its empty `.part` file."""
        upload_id = str(uuid.uuid4())
        ext = Path(filename or "upload").suffix.lower()
        part_path = self._part_path(upload_id, ext)

        # Create/truncate the part file so appends start from a known empty state.
        with open(part_path, "wb"):
            pass

        session = {
            "upload_id": upload_id,
            "filename": filename or "upload",
            "ext": ext,
            "part_path": part_path,
            "total_size": int(total_size) if total_size is not None else None,
            "archive_bucket": (archive_bucket or "").strip() or None,
            "received_bytes": 0,
            "created_at": datetime.utcnow().isoformat(),
        }
        with self.lock:
            self.sessions[upload_id] = session
            self._file_locks[upload_id] = Lock()
        return session

    def get(self, upload_id: str) -> Optional[dict]:
        with self.lock:
            return self.sessions.get(upload_id)

    def _require(self, upload_id: str) -> dict:
        session = self.get(upload_id)
        if session is None:
            raise UploadError(f"unknown_upload_id: {upload_id}")
        return session

    def append(self, upload_id: str, offset: int, data: bytes) -> dict:
        """Append one chunk's bytes at `offset`.

        `offset` must equal the bytes already received. A smaller offset means the
        client is retransmitting a chunk we already have — we ack the current state
        without re-writing (idempotent retry). A larger offset is a gap and is rejected.
        The bytes are written straight to the `.part` file, so the full file is never
        held in memory.
        """
        session = self._require(upload_id)
        file_lock = self._file_locks[upload_id]

        with file_lock:
            received = session["received_bytes"]
            if offset > received:
                raise UploadError(
                    f"offset_gap: expected {received}, got {offset}"
                )
            if offset < received:
                # Duplicate / already-applied chunk. Ack current state.
                return session

            with open(session["part_path"], "ab") as f:
                f.write(data)

            session["received_bytes"] = received + len(data)
            return session

    def complete(self, upload_id: str, dest_path: str) -> dict:
        """Move the finished `.part` file to `dest_path` (atomic on same FS) and
        drop the session. Returns the session info. Validates size when known.
        """
        session = self._require(upload_id)

        total = session.get("total_size")
        if total is not None and session["received_bytes"] != total:
            raise UploadError(
                f"incomplete_upload: received {session['received_bytes']} of {total} bytes"
            )

        os.replace(session["part_path"], dest_path)
        with self.lock:
            self.sessions.pop(upload_id, None)
            self._file_locks.pop(upload_id, None)
        return session

    def abort(self, upload_id: str) -> None:
        """Delete a session and its partial file."""
        session = self.get(upload_id)
        if session is None:
            return
        try:
            if os.path.exists(session["part_path"]):
                os.remove(session["part_path"])
        finally:
            with self.lock:
                self.sessions.pop(upload_id, None)
                self._file_locks.pop(upload_id, None)
