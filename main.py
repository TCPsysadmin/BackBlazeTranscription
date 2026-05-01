"""Media Transcription Service - Main API"""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, HttpUrl, Field
from dotenv import load_dotenv
import uvicorn

from urllib.parse import urlparse

from services.job_manager import JobManager
from services.transcription_worker import TranscriptionWorker


def _resolve_callback_url(url) -> str | None:
    """Return the callback URL as a string, or None if it should be skipped.
    URLs on the RFC 2606 reserved `.invalid` TLD are sentinels meaning "no webhook".
    """
    if not url:
        return None
    s = str(url)
    host = (urlparse(s).hostname or "").lower()
    if host == "invalid" or host.endswith(".invalid"):
        return None
    return s


load_dotenv()
# Configuration
API_KEY = os.getenv("API_KEY", "your-secret-api-key")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# B2 credentials are NOT loaded from env — they are supplied per-request in the API body
# so the same backend can serve multiple B2 accounts.
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "1"))  # 1 = safe for 512MB / limited disk (e.g. Render free)
# Comma-separated list of allowed origins for browser frontends. "*" allows all (no credentials).
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]

job_manager = JobManager()
worker = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    global worker
    # Replace the default ThreadPoolExecutor (which is min(32, cpu_count()+4) — only 5
    # threads on a 1-CPU container) with a larger pool. b2sdk runs synchronously inside
    # run_in_executor, so a single hung TCP read pins one thread until the OS notices
    # the dead socket (can be ~2h). With 53 queued jobs we need headroom so a few leaked
    # threads don't exhaust the pool and starve subsequent jobs.
    asyncio.get_event_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=32, thread_name_prefix="b2-io")
    )

    worker = TranscriptionWorker(
        job_manager,
        OPENAI_API_KEY,
        max_concurrent_jobs=MAX_CONCURRENT_JOBS
    )
    asyncio.create_task(worker.process_jobs())
    yield
    # Cleanup on shutdown
    worker.stop()


app = FastAPI(
    title="Media Transcription Service",
    description="""
    A scalable backend service that transcribes long-form media files from Backblaze using OpenAI's Whisper API.
    
    ## Features
    
    - **Asynchronous Processing**: Submit jobs and receive webhook callbacks when complete
    - **Large File Support**: Automatic chunking for files longer than 10 minutes
    - **Parallel Transcription**: Process multiple chunks simultaneously for faster results
    - **Automatic Retries**: Built-in retry logic for transient failures
    - **Idempotent**: Duplicate submissions return the same job ID
    
    ## Authentication
    
    All endpoints (except `/health`) require an API key passed in the `X-API-KEY` header.
    
    ## Workflow
    
    1. Submit a transcription job via `POST /transcribe`
    2. Receive a job ID and status "queued"
    3. Service processes the job asynchronously
    4. Receive webhook callback when complete or failed
    5. Optionally query job status via `GET /jobs/{job_id}`
    
    ## Webhook Callbacks
    
    When a job completes or fails, the service sends a POST request to your `callback_url` with the following payload:
    
    **Success Payload:**
    ```json
    {
      "job_id": "550e8400-e29b-41d4-a716-446655440000",
      "status": "completed",
      "transcript": "Full transcribed text here..."
    }
    ```
    
    **Failure Payload:**
    ```json
    {
      "job_id": "550e8400-e29b-41d4-a716-446655440000",
      "status": "failed",
      "error": "file_not_found"
    }
    ```
    
    **Common Error Codes:**
    - `bucket_not_found`: B2 bucket doesn't exist
    - `file_not_found`: Media file doesn't exist in B2
    - `unsupported_format`: File format not supported
    - `audio_extraction_failed`: Could not extract audio from video
    - `transcription_failed`: OpenAI API error
    - `download_error`: Network or B2 access error
    - `b2_error`: B2 API error
    
    The service will retry webhook delivery up to 3 times with exponential backoff if your endpoint is unavailable.
    If webhook delivery fails after all retries, the job status can still be retrieved via the GET /jobs/{job_id} endpoint.
    """,
    version="1.0.0",
    contact={
        "name": "API Support",
        "email": "support@example.com",
    },
    license_info={
        "name": "MIT",
    },
    lifespan=lifespan,
    docs_url=None,  # Disable default Swagger UI
    redoc_url=None,  # Disable ReDoc
)

# CORS for browser frontends. With allow_origins=["*"], allow_credentials must be False
# (CORS spec). If you need cookies/auth credentials, set CORS_ORIGINS to specific domains.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)


class TranscribeRequest(BaseModel):
    b2_bucket: str = Field(
        ...,
        description="Backblaze B2 bucket name where the media file is stored",
        examples=["my-media-bucket"]
    )
    b2_file_path: str = Field(
        ...,
        description="Path to the media file within the B2 bucket",
        examples=["recordings/2024/interview.mp4"]
    )
    b2_key_id: str = Field(
        ...,
        description="Backblaze B2 application key ID for accessing the bucket",
        examples=["005abc1234567890000000001"]
    )
    b2_application_key: str = Field(
        ...,
        description="Backblaze B2 application key (secret) paired with b2_key_id",
        examples=["K005abcdefghijklmnopqrstuvwxyz0123"]
    )
    callback_url: HttpUrl | None = Field(
        None,
        description="Webhook URL to receive job completion/failure notifications (optional; omit to skip webhook)",
        examples=["https://your-app.com/webhooks/transcription"]
    )


class TranscribeHTTPRequest(BaseModel):
    media_url: HttpUrl = Field(
        ...,
        description="Direct HTTPS URL to the media file (e.g., Backblaze B2 public URL)",
        examples=["https://f005.backblazeb2.com/file/TCPTRANSFER/folder/audio.mp3"]
    )
    b2_key_id: str = Field(
        ...,
        description="Backblaze B2 application key ID for accessing the bucket",
        examples=["005abc1234567890000000001"]
    )
    b2_application_key: str = Field(
        ...,
        description="Backblaze B2 application key (secret) paired with b2_key_id",
        examples=["K005abcdefghijklmnopqrstuvwxyz0123"]
    )
    callback_url: HttpUrl | None = Field(
        None,
        description="Webhook URL to receive job completion/failure notifications (optional; omit to skip webhook)",
        examples=["https://your-app.com/webhooks/transcription"]
    )


class TranscribeResponse(BaseModel):
    job_id: str = Field(
        ...,
        description="Unique identifier for the transcription job",
        examples=["550e8400-e29b-41d4-a716-446655440000"]
    )
    status: str = Field(
        ...,
        description="Current job status",
        examples=["queued"]
    )


class QueuedJobInfo(BaseModel):
    job_id: str = Field(
        ...,
        description="Unique identifier for the transcription job",
        examples=["550e8400-e29b-41d4-a716-446655440000"]
    )
    b2_bucket: str = Field(
        ...,
        description="Backblaze B2 bucket name",
        examples=["my-media-bucket"]
    )
    b2_file_path: str = Field(
        ...,
        description="Path to the media file within the B2 bucket",
        examples=["recordings/2024/interview.mp4"]
    )
    status: str = Field(..., description="Current job status", examples=["queued"])
    progress: int = Field(..., description="Job progress percentage (0-100)", examples=[0])
    created_at: str = Field(
        ...,
        description="ISO-8601 UTC timestamp of when the job was created",
        examples=["2026-04-30T12:34:56.789012"]
    )


class QueueResponse(BaseModel):
    count: int = Field(..., description="Total number of jobs currently queued", examples=[2])
    jobs: list[QueuedJobInfo] = Field(..., description="List of queued jobs in submission order")


class JobStatusResponse(BaseModel):
    job_id: str = Field(
        ...,
        description="Unique identifier for the transcription job",
        examples=["550e8400-e29b-41d4-a716-446655440000"]
    )
    status: str = Field(
        ...,
        description="Current job status: queued, processing, completed, or failed",
        examples=["processing"]
    )
    progress: int = Field(
        ...,
        description="Job progress percentage (0-100)",
        examples=[45]
    )
    error: str | None = Field(
        None,
        description="Error message if job failed",
        examples=["file_not_found"]
    )
    transcript: str | None = Field(
        None,
        description="Full transcript text (only available when status is completed)",
        examples=["This is the transcribed text from the audio file."]
    )


def verify_api_key(x_api_key: str = Header(...)):
    """Verify API key from header"""
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.post(
    "/transcribe",
    response_model=TranscribeResponse,
    tags=["Transcription"],
    summary="Submit a transcription job",
    description="""
    Submit a media file for transcription. The service will:
    
    1. Download the file from Backblaze B2
    2. Extract audio (if video file)
    3. Split into 10-minute chunks if needed
    4. Transcribe chunks in parallel using OpenAI Whisper
    5. Merge transcripts and send to callback URL
    
    **Supported Formats:**
    - Audio: mp3, wav, m4a, flac, ogg, aac
    - Video: mp4, mov, avi, mkv, webm
    
    **Processing Time:**
    - Typically 1-2x the duration of the media file
    - Longer files process faster due to parallel chunking
    """,
    responses={
        200: {
            "description": "Job created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "job_id": "550e8400-e29b-41d4-a716-446655440000",
                        "status": "queued"
                    }
                }
            }
        },
        400: {"description": "Invalid request (missing callback_url)"},
        401: {"description": "Invalid or missing API key"},
    }
)
async def create_transcription_job(
    request: TranscribeRequest,
    background_tasks: BackgroundTasks,
    x_api_key: str = Header(..., alias="X-API-KEY", description="Your API key for authentication"),
    x_upload_transcript: bool = Header(False, alias="X-Upload-Transcript", description="Upload transcript to B2 as .txt file (default: false)")
):
    """Submit a new transcription job"""
    verify_api_key(x_api_key)

    callback_url_str = _resolve_callback_url(request.callback_url)

    # Check for existing job (idempotency)
    existing_job = job_manager.find_existing_job(
        request.b2_bucket,
        request.b2_file_path,
        callback_url_str
    )

    if existing_job:
        return TranscribeResponse(job_id=existing_job["job_id"], status=existing_job["status"])

    # Create new job with upload_transcript flag
    job_id = job_manager.create_job(
        b2_bucket=request.b2_bucket,
        b2_file_path=request.b2_file_path,
        callback_url=callback_url_str,
        b2_key_id=request.b2_key_id,
        b2_application_key=request.b2_application_key,
        upload_transcript=x_upload_transcript
    )

    return TranscribeResponse(job_id=job_id, status="queued")


@app.post(
    "/transcribeHTTP",
    response_model=TranscribeResponse,
    tags=["Transcription"],
    summary="Submit a transcription job via HTTP URL",
    description="""
    Submit a media file for transcription using a direct HTTPS URL instead of B2 credentials.
    This is useful for publicly accessible files or when you have a direct download link.
    
    The service will:
    
    1. Download the file from the provided URL
    2. Extract audio (if video file)
    3. Split into 10-minute chunks if needed
    4. Transcribe chunks in parallel using OpenAI Whisper
    5. Merge transcripts and send to callback URL
    
    **Supported Formats:**
    - Audio: mp3, wav, m4a, flac, ogg, aac
    - Video: mp4, mov, avi, mkv, webm
    
    **URL Requirements:**
    - Must be HTTPS
    - Must be publicly accessible or include authentication in the URL
    - File extension should be included in the URL for format detection
    
    **Processing Time:**
    - Typically 1-2x the duration of the media file
    - Longer files process faster due to parallel chunking
    """,
    responses={
        200: {
            "description": "Job created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "job_id": "550e8400-e29b-41d4-a716-446655440000",
                        "status": "queued"
                    }
                }
            }
        },
        400: {"description": "Invalid request (missing callback_url or invalid URL)"},
        401: {"description": "Invalid or missing API key"},
    }
)
async def create_transcription_job_http(
    request: TranscribeHTTPRequest,
    background_tasks: BackgroundTasks,
    x_api_key: str = Header(..., alias="X-API-KEY", description="Your API key for authentication"),
    x_upload_transcript: bool = Header(False, alias="X-Upload-Transcript", description="Upload transcript to B2 as .txt file (default: false)")
):
    """Submit a new transcription job using HTTP URL"""
    from urllib.parse import urlparse, unquote
    
    verify_api_key(x_api_key)

    callback_url_str = _resolve_callback_url(request.callback_url)

    # Parse B2 URL to extract bucket and file path
    try:
        parsed = urlparse(str(request.media_url))
        path_parts = parsed.path.split("/")
        
        # B2 URLs have format: https://f005.backblazeb2.com/file/BUCKET_NAME/file/path
        if len(path_parts) < 4 or path_parts[1] != "file":
            raise HTTPException(
                status_code=400, 
                detail="Invalid B2 URL format. Expected: https://f005.backblazeb2.com/file/BUCKET_NAME/path/to/file"
            )
        
        bucket_name = path_parts[2]
        file_path = unquote("/".join(path_parts[3:]))
        
        if not bucket_name or not file_path:
            raise HTTPException(status_code=400, detail="Could not extract bucket name or file path from URL")
            
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse B2 URL: {str(e)}")
    
    # Check for existing job (idempotency)
    existing_job = job_manager.find_existing_job(
        bucket_name,
        file_path,
        callback_url_str
    )

    if existing_job:
        return TranscribeResponse(job_id=existing_job["job_id"], status=existing_job["status"])

    # Create new job using extracted bucket and file path
    job_id = job_manager.create_job(
        b2_bucket=bucket_name,
        b2_file_path=file_path,
        callback_url=callback_url_str,
        b2_key_id=request.b2_key_id,
        b2_application_key=request.b2_application_key,
        upload_transcript=x_upload_transcript
    )
    
    return TranscribeResponse(job_id=job_id, status="queued")


@app.get(
    "/queue",
    response_model=QueueResponse,
    tags=["Transcription"],
    summary="List queued jobs",
    description="""
    Return all jobs currently in the `queued` state (waiting to be picked up by a worker).

    Credentials (B2 key id / application key) and webhook callback URLs are intentionally
    omitted from the response.
    """,
    responses={
        200: {
            "description": "Queued jobs retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "count": 1,
                        "jobs": [
                            {
                                "job_id": "550e8400-e29b-41d4-a716-446655440000",
                                "b2_bucket": "my-media-bucket",
                                "b2_file_path": "recordings/2024/interview.mp4",
                                "status": "queued",
                                "progress": 0,
                                "created_at": "2026-04-30T12:34:56.789012",
                            }
                        ],
                    }
                }
            },
        },
        401: {"description": "Invalid or missing API key"},
    },
)
async def list_queued_jobs(
    x_api_key: str = Header(..., alias="X-API-KEY", description="Your API key for authentication")
):
    """List all jobs currently in the queue."""
    verify_api_key(x_api_key)

    queued = job_manager.get_queued_jobs()
    queued_sorted = sorted(queued, key=lambda j: j.get("created_at") or "")
    jobs = [
        QueuedJobInfo(
            job_id=job["job_id"],
            b2_bucket=job["b2_bucket"],
            b2_file_path=job["b2_file_path"],
            status=job["status"],
            progress=job["progress"],
            created_at=job["created_at"],
        )
        for job in queued_sorted
    ]
    return QueueResponse(count=len(jobs), jobs=jobs)


@app.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    tags=["Transcription"],
    summary="Get job status",
    description="""
    Query the current status and progress of a transcription job.
    
    **Status Values:**
    - `queued`: Job is waiting to be processed
    - `processing`: Job is currently being transcribed
    - `completed`: Job finished successfully (transcript available)
    - `failed`: Job failed (error message available)
    
    **Note:** The transcript is only included when status is "completed".
    For large transcripts, consider using the webhook callback instead.
    """,
    responses={
        200: {
            "description": "Job status retrieved successfully",
            "content": {
                "application/json": {
                    "examples": {
                        "processing": {
                            "summary": "Job in progress",
                            "value": {
                                "job_id": "550e8400-e29b-41d4-a716-446655440000",
                                "status": "processing",
                                "progress": 45,
                                "error": None,
                                "transcript": None
                            }
                        },
                        "completed": {
                            "summary": "Job completed",
                            "value": {
                                "job_id": "550e8400-e29b-41d4-a716-446655440000",
                                "status": "completed",
                                "progress": 100,
                                "error": None,
                                "transcript": "This is the full transcribed text..."
                            }
                        },
                        "failed": {
                            "summary": "Job failed",
                            "value": {
                                "job_id": "550e8400-e29b-41d4-a716-446655440000",
                                "status": "failed",
                                "progress": 0,
                                "error": "file_not_found",
                                "transcript": None
                            }
                        }
                    }
                }
            }
        },
        401: {"description": "Invalid or missing API key"},
        404: {"description": "Job not found"},
    }
)
async def get_job_status(
    job_id: str,
    x_api_key: str = Header(..., alias="X-API-KEY", description="Your API key for authentication")
):
    """Get the current status and progress of a transcription job"""
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


@app.get(
    "/health",
    tags=["System"],
    summary="Health check",
    description="Check if the service is running and healthy. Does not require authentication.",
    responses={
        200: {
            "description": "Service is healthy",
            "content": {
                "application/json": {
                    "example": {"status": "healthy"}
                }
            }
        }
    }
)
async def health_check():
    """Health check endpoint - no authentication required"""
    return {"status": "healthy"}


@app.get("/", include_in_schema=False)
async def root():
    """Redirect root to API documentation"""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <meta http-equiv="refresh" content="0; url=/docs" />
        <title>Redirecting to API Docs</title>
    </head>
    <body>
        <p>Redirecting to <a href="/docs">API documentation</a>...</p>
    </body>
    </html>
    """)


@app.get("/docs", include_in_schema=False)
async def scalar_html():
    """Scalar API documentation"""
    return HTMLResponse(f"""
    <!doctype html>
    <html>
    <head>
        <title>Media Transcription Service - API Documentation</title>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
    </head>
    <body>
        <script
            id="api-reference"
            data-url="/openapi.json"
            data-configuration='{{"theme":"purple"}}'
        ></script>
        <script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
    </body>
    </html>
    """)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
