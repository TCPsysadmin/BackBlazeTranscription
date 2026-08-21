"""Job state management"""
import uuid
from datetime import datetime
from threading import Lock
from typing import Dict, Optional


class JobManager:
    """Manages job state and lifecycle"""
    
    def __init__(self):
        self.jobs: Dict[str, dict] = {}
        self.lock = Lock()
    
    def create_job(
        self,
        callback_url: Optional[str],
        # B2 source fields (used by /transcribe and /transcribeHTTP)
        b2_bucket: Optional[str] = None,
        b2_file_path: Optional[str] = None,
        b2_key_id: Optional[str] = None,
        b2_application_key: Optional[str] = None,
        upload_transcript: bool = False,
        # Local file source fields (used by /transcribe-file)
        source_type: str = "b2",
        local_file_path: Optional[str] = None,
        original_filename: Optional[str] = None,
        # Google Drive output
        google_drive_folder_id: Optional[str] = None,
        # Allow caller to supply a pre-generated ID (e.g. so the upload file can
        # share the same UUID prefix for cleanup matching)
        job_id: Optional[str] = None,
    ) -> str:
        """Create a new transcription job"""
        if job_id is None:
            job_id = str(uuid.uuid4())

        with self.lock:
            self.jobs[job_id] = {
                "job_id": job_id,
                # source
                "source_type": source_type,
                "b2_bucket": b2_bucket or "",
                "b2_file_path": b2_file_path or "",
                "b2_key_id": b2_key_id or "",
                "b2_application_key": b2_application_key or "",
                "local_file_path": local_file_path,
                "original_filename": original_filename,
                # output
                "upload_transcript": upload_transcript,
                "google_drive_folder_id": google_drive_folder_id,
                "drive_transcript_file_id": None,
                "drive_transcript_url": None,
                # B2-source media is already permanently stored. Surface the
                # original object so callers can register it in their library.
                "archived_video_bucket": b2_bucket if source_type == "b2" else None,
                "archived_video_path": b2_file_path if source_type == "b2" else None,
                "thumbnail_b2_path": None,
                "archive_error": None,
                "has_audio": None,
                # state
                "callback_url": callback_url,
                "status": "queued",
                "progress": 0,
                "created_at": datetime.utcnow().isoformat(),
                "error": None,
                "transcript": None,
                "chunks_total": 0,
                "chunks_completed": 0,
            }

        return job_id
    
    def get_job(self, job_id: str) -> Optional[dict]:
        """Get job by ID"""
        with self.lock:
            return self.jobs.get(job_id)
    
    def update_job(self, job_id: str, **kwargs):
        """Update job fields"""
        with self.lock:
            if job_id in self.jobs:
                self.jobs[job_id].update(kwargs)
    
    def find_existing_job(self, b2_bucket: str, b2_file_path: str, callback_url: Optional[str]) -> Optional[dict]:
        """Find existing queued or processing job with same parameters"""
        with self.lock:
            for job in self.jobs.values():
                if (job["b2_bucket"] == b2_bucket and
                    job["b2_file_path"] == b2_file_path and
                    job["callback_url"] == callback_url and
                    job["status"] in ["queued", "processing"]):
                    return job
        return None
    
    def get_queued_jobs(self):
        """Get all queued jobs"""
        with self.lock:
            return [job for job in self.jobs.values() if job["status"] == "queued"]
    
    def update_progress(self, job_id: str, chunks_completed: int, chunks_total: int):
        """Update job progress"""
        progress = int((chunks_completed / chunks_total) * 100) if chunks_total > 0 else 0
        self.update_job(job_id, progress=progress, chunks_completed=chunks_completed)
