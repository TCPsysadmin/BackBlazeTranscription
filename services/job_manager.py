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
    
    def create_job(self, b2_bucket: str, b2_file_path: str, callback_url: str, upload_transcript: bool = False) -> str:
        """Create a new transcription job"""
        job_id = str(uuid.uuid4())
        
        with self.lock:
            self.jobs[job_id] = {
                "job_id": job_id,
                "b2_bucket": b2_bucket,
                "b2_file_path": b2_file_path,
                "callback_url": callback_url,
                "upload_transcript": upload_transcript,
                "status": "queued",
                "progress": 0,
                "created_at": datetime.utcnow().isoformat(),
                "error": None,
                "transcript": None,
                "chunks_total": 0,
                "chunks_completed": 0
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
    
    def find_existing_job(self, b2_bucket: str, b2_file_path: str, callback_url: str) -> Optional[dict]:
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
