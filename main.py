"""Media Transcription Service - Main API"""
import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from pydantic import BaseModel, HttpUrl
import uvicorn

from services.job_manager import JobManager
from services.transcription_worker import TranscriptionWorker


# Configuration
API_KEY = os.getenv("API_KEY", "your-secret-api-key")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
B2_KEY_ID = os.getenv("B2_KEY_ID")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY")

job_manager = JobManager()
worker = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    global worker
    worker = TranscriptionWorker(job_manager, OPENAI_API_KEY, B2_KEY_ID, B2_APPLICATION_KEY)
    asyncio.create_task(worker.process_jobs())
    yield
    # Cleanup on shutdown
    worker.stop()


app = FastAPI(title="Media Transcription Service", lifespan=lifespan)


class TranscribeRequest(BaseModel):
    b2_bucket: str
    b2_file_path: str
    callback_url: HttpUrl


class TranscribeResponse(BaseModel):
    job_id: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: int
    error: str | None = None
    transcript: str | None = None


def verify_api_key(x_api_key: str = Header(...)):
    """Verify API key from header"""
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.post("/transcribe", response_model=TranscribeResponse)
async def create_transcription_job(
    request: TranscribeRequest,
    background_tasks: BackgroundTasks,
    x_api_key: str = Header(..., alias="X-API-KEY")
):
    """Create a new transcription job"""
    verify_api_key(x_api_key)
    
    if not request.callback_url:
        raise HTTPException(status_code=400, detail="callback_url is required")
    
    # Check for existing job (idempotency)
    existing_job = job_manager.find_existing_job(
        request.b2_bucket,
        request.b2_file_path,
        str(request.callback_url)
    )
    
    if existing_job:
        return TranscribeResponse(job_id=existing_job["job_id"], status=existing_job["status"])
    
    # Create new job
    job_id = job_manager.create_job(
        b2_bucket=request.b2_bucket,
        b2_file_path=request.b2_file_path,
        callback_url=str(request.callback_url)
    )
    
    return TranscribeResponse(job_id=job_id, status="queued")


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(
    job_id: str,
    x_api_key: str = Header(..., alias="X-API-KEY")
):
    """Get job status and progress"""
    verify_api_key(x_api_key)
    
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return JobStatusResponse(
        job_id=job["job_id"],
        status=job["status"],
        progress=job["progress"],
        error=job.get("error"),
        transcript=job.get("transcript")
    )


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
