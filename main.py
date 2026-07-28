"""Media Transcription Service - Main API"""
import asyncio
import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, HttpUrl, Field
from dotenv import load_dotenv
import uvicorn

from urllib.parse import urlparse

from services.job_manager import JobManager
from services.transcription_worker import TranscriptionWorker
from services.upload_manager import UploadManager, UploadError
from services.b2_client import B2Client, B2DownloadError


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

    # Clear orphaned resumable-upload .part files from a previous run. Upload sessions
    # are in-memory, so any leftover file on disk is unreachable and just wastes space.
    try:
        up_dir = _uploads_dir()
        for _name in os.listdir(up_dir):
            try:
                os.remove(os.path.join(up_dir, _name))
            except Exception:
                pass
    except Exception:
        pass

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


class FetchTextRequest(BaseModel):
    media_url: HttpUrl = Field(
        ...,
        description="Direct B2 URL to a UTF-8 text file (e.g. an existing transcript .txt)",
        examples=["https://f005.backblazeb2.com/file/TCPTRANSFER/folder/transcript.txt"]
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


class FetchTextResponse(BaseModel):
    text: str = Field(..., description="The decoded UTF-8 contents of the file")
    bucket: str = Field(..., description="Bucket the file was read from")
    file_path: str = Field(..., description="Path of the file within the bucket")


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
    drive_transcript_file_id: str | None = Field(
        None,
        description="Google Drive file ID of the uploaded transcript (if Drive upload was requested)",
        examples=["1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"]
    )
    drive_transcript_url: str | None = Field(
        None,
        description="Google Drive web view URL of the uploaded transcript",
        examples=["https://drive.google.com/file/d/1Bxi.../view"]
    )
    archived_video_bucket: str | None = Field(
        None, description="B2 bucket containing a permanently archived local upload"
    )
    archived_video_path: str | None = Field(
        None, description="Permanent B2 object path for the original uploaded media"
    )
    thumbnail_b2_path: str | None = Field(
        None, description="B2 object path for the generated WebP thumbnail"
    )
    archive_error: str | None = Field(
        None, description="Permanent archive error, if archival failed"
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
        callback_url=callback_url_str,
        b2_bucket=request.b2_bucket,
        b2_file_path=request.b2_file_path,
        b2_key_id=request.b2_key_id,
        b2_application_key=request.b2_application_key,
        upload_transcript=x_upload_transcript,
        source_type="b2",
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
        callback_url=callback_url_str,
        b2_bucket=bucket_name,
        b2_file_path=file_path,
        b2_key_id=request.b2_key_id,
        b2_application_key=request.b2_application_key,
        upload_transcript=x_upload_transcript,
        source_type="b2",
    )

    return TranscribeResponse(job_id=job_id, status="queued")


@app.post(
    "/fetchText",
    response_model=FetchTextResponse,
    tags=["Transcription"],
    summary="Fetch a text file (e.g. an existing transcript) from B2",
    description="""
    Download a small UTF-8 text file (such as an already-produced transcript) from a
    Backblaze B2 URL and return its contents inline. This lets a browser client read a
    transcript stored in a private bucket without exposing B2 credentials in the browser
    or hitting CORS limits — the server does the authenticated download.

    No transcription is performed. Files larger than 10 MB are rejected.
    """,
    responses={
        200: {"description": "File contents returned"},
        400: {"description": "Invalid B2 URL"},
        401: {"description": "Invalid or missing API key"},
        404: {"description": "Bucket or file not found"},
        413: {"description": "File too large"},
    }
)
async def fetch_text(
    request: FetchTextRequest,
    x_api_key: str = Header(..., alias="X-API-KEY", description="Your API key for authentication"),
):
    """Download and return the contents of a B2 text file (no transcription)."""
    from urllib.parse import urlparse, unquote

    verify_api_key(x_api_key)

    # Parse the B2 friendly URL into bucket + file path (same shape as /transcribeHTTP).
    try:
        parsed = urlparse(str(request.media_url))
        path_parts = parsed.path.split("/")
        if len(path_parts) < 4 or path_parts[1] != "file":
            raise HTTPException(
                status_code=400,
                detail="Invalid B2 URL format. Expected: https://f005.backblazeb2.com/file/BUCKET_NAME/path/to/file"
            )
        bucket_name = path_parts[2]
        file_path = unquote("/".join(path_parts[3:]))
        if not bucket_name or not file_path:
            raise HTTPException(status_code=400, detail="Could not extract bucket name or file path from URL")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse B2 URL: {str(e)}")

    client = B2Client(request.b2_key_id, request.b2_application_key)
    try:
        text = await client.download_text(bucket_name, file_path)
    except B2DownloadError as e:
        msg = str(e)
        if "file_not_found" in msg or "bucket_not_found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "file_too_large" in msg:
            raise HTTPException(status_code=413, detail=msg)
        raise HTTPException(status_code=400, detail=msg)

    return FetchTextResponse(text=text, bucket=bucket_name, file_path=file_path)


# Supported video/audio extensions for direct upload
_SUPPORTED_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac",
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
}

TEMP_STORAGE_DIR = os.getenv("TEMP_STORAGE_DIR", "/data")


def _work_base_dir() -> str:
    """Base dir for transcription files — the persistent disk if mounted, else a
    local ./tmp fallback (e.g. local dev without the Render SSD mount)."""
    return TEMP_STORAGE_DIR if os.path.isdir(TEMP_STORAGE_DIR) else os.path.join(os.getcwd(), "tmp")


def _transcription_work_dir() -> str:
    """Where the worker expects in-flight job files (matches /transcribe-file)."""
    d = os.path.join(_work_base_dir(), "transcription_work")
    os.makedirs(d, exist_ok=True)
    return d


def _uploads_dir() -> str:
    """Where resumable-upload .part files are assembled before hand-off."""
    d = os.path.join(_work_base_dir(), "transcription_uploads")
    os.makedirs(d, exist_ok=True)
    return d


# Resumable chunked-upload sessions (in-memory, like JobManager). Lets browsers send
# large files in retryable slices instead of one giant POST. Orphaned .part files are
# cleared on boot by the lifespan handler above.
upload_manager = UploadManager(_uploads_dir())


@app.post(
    "/transcribe-file",
    response_model=TranscribeResponse,
    tags=["Transcription"],
    summary="Upload and transcribe a media file directly",
    description="""
    Upload a video or audio file directly for transcription — no Backblaze B2 required.

    The file is saved temporarily on the server, transcribed with OpenAI Whisper, and
    optionally written to a Google Drive folder when done. All existing B2-based endpoints
    remain fully functional alongside this one.

    **Supported formats:** mp3, wav, m4a, flac, ogg, aac, mp4, mov, avi, mkv, webm

    **Form fields:**
    - `file` — the media file (multipart/form-data)
    - `callback_url` *(optional)* — webhook URL for completion notification
    - `google_drive_folder_id` *(optional)* — Drive folder ID to save `<name>_transcript.txt`

    **Google Drive setup:** set `GOOGLE_SERVICE_ACCOUNT_JSON` or `GOOGLE_SERVICE_ACCOUNT_FILE`
    on the server and share the target folder with the service account email.
    """,
)
async def transcribe_uploaded_file(
    file: UploadFile = File(..., description="Video or audio file to transcribe"),
    callback_url: str | None = Form(None, description="Webhook URL for completion notification"),
    google_drive_folder_id: str | None = Form(None, description="Google Drive folder ID to save transcript"),
    x_api_key: str = Header(..., alias="X-API-KEY", description="Your API key for authentication"),
):
    """Upload a media file and queue it for transcription."""
    verify_api_key(x_api_key)

    filename = file.filename or "upload"
    ext = Path(filename).suffix.lower()
    if ext not in _SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Supported: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}",
        )

    callback_url_str = _resolve_callback_url(callback_url)

    # Generate the job ID here so the uploaded file can share the same UUID prefix.
    # The worker's cleanup system matches files by job_id prefix, so this keeps
    # the uploaded file inside that scope and prevents orphaned files on disk.
    job_id = str(uuid.uuid4())
    work_dir = os.path.join(
        TEMP_STORAGE_DIR if os.path.isdir(TEMP_STORAGE_DIR) else os.path.join(os.getcwd(), "tmp"),
        "transcription_work",
    )
    os.makedirs(work_dir, exist_ok=True)
    local_path = os.path.join(work_dir, f"{job_id}_upload{ext}")

    try:
        with open(local_path, "wb") as dst:
            shutil.copyfileobj(file.file, dst)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save uploaded file: {e}")
    finally:
        await file.close()

    job_manager.create_job(
        job_id=job_id,
        callback_url=callback_url_str,
        source_type="local_file",
        local_file_path=local_path,
        original_filename=filename,
        google_drive_folder_id=google_drive_folder_id or None,
    )

    return TranscribeResponse(job_id=job_id, status="queued")


# ---------------------------------------------------------------------------
# Resumable chunked upload (additive; existing /transcribe-file is untouched)
# ---------------------------------------------------------------------------

class UploadInitRequest(BaseModel):
    filename: str = Field(..., description="Original file name (used for extension + transcript naming)", examples=["interview.mp4"])
    total_size: int | None = Field(None, description="Total file size in bytes (enables completeness validation)", examples=[5368709120])


class UploadSessionResponse(BaseModel):
    upload_id: str = Field(..., description="Identifier for this resumable upload session")
    received_bytes: int = Field(..., description="Bytes received and persisted so far")
    total_size: int | None = Field(None, description="Total size if it was provided at init")


class UploadCompleteRequest(BaseModel):
    callback_url: HttpUrl | None = Field(None, description="Optional webhook for completion notification")
    google_drive_folder_id: str | None = Field(None, description="Optional Drive folder id for the backend to save the transcript")


@app.post(
    "/uploads/init",
    response_model=UploadSessionResponse,
    tags=["Transcription"],
    summary="Start a resumable upload",
    description="""
    Begin a resumable, chunked upload of a media file. Returns an `upload_id`; send the
    file in slices via `POST /uploads/{upload_id}/chunk`, then finalize with
    `POST /uploads/{upload_id}/complete` to queue the transcription job.

    This is an alternative to the single-shot `/transcribe-file` for large files where a
    single long request risks proxy timeouts. The existing endpoints are unchanged.
    """,
)
async def uploads_init(
    request: UploadInitRequest,
    x_api_key: str = Header(..., alias="X-API-KEY", description="Your API key for authentication"),
):
    verify_api_key(x_api_key)

    ext = Path(request.filename or "upload").suffix.lower()
    if ext not in _SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Supported: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}",
        )

    session = upload_manager.init(request.filename, request.total_size)
    return UploadSessionResponse(
        upload_id=session["upload_id"],
        received_bytes=session["received_bytes"],
        total_size=session["total_size"],
    )


@app.get(
    "/uploads/{upload_id}",
    response_model=UploadSessionResponse,
    tags=["Transcription"],
    summary="Get resumable upload status",
    description="Return how many bytes have been received so a client can resume from the correct offset.",
)
async def uploads_status(
    upload_id: str,
    x_api_key: str = Header(..., alias="X-API-KEY", description="Your API key for authentication"),
):
    verify_api_key(x_api_key)
    session = upload_manager.get(upload_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Upload session not found")
    return UploadSessionResponse(
        upload_id=upload_id,
        received_bytes=session["received_bytes"],
        total_size=session["total_size"],
    )


@app.post(
    "/uploads/{upload_id}/chunk",
    response_model=UploadSessionResponse,
    tags=["Transcription"],
    summary="Upload one chunk",
    description="""
    Append one slice of the file at byte offset `X-Chunk-Offset`. The request body is the
    raw bytes of the slice (`application/octet-stream`). Offsets must be contiguous; a
    re-sent chunk at an already-received offset is acked idempotently (safe to retry).
    """,
)
async def uploads_chunk(
    upload_id: str,
    request: Request,
    x_chunk_offset: int = Header(..., alias="X-Chunk-Offset", description="Byte offset of this chunk within the file"),
    x_api_key: str = Header(..., alias="X-API-KEY", description="Your API key for authentication"),
):
    verify_api_key(x_api_key)
    if upload_manager.get(upload_id) is None:
        raise HTTPException(status_code=404, detail="Upload session not found")

    # One bounded chunk (~tens of MB) in memory, then straight to disk. We never buffer
    # the whole file. request.body() reads the full slice for this request only.
    data = await request.body()
    try:
        session = upload_manager.append(upload_id, x_chunk_offset, data)
    except UploadError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return UploadSessionResponse(
        upload_id=upload_id,
        received_bytes=session["received_bytes"],
        total_size=session["total_size"],
    )


@app.post(
    "/uploads/{upload_id}/complete",
    response_model=TranscribeResponse,
    tags=["Transcription"],
    summary="Finalize upload and queue transcription",
    description="""
    Finalize a resumable upload: the assembled file is moved into the worker's queue and a
    transcription job is created (same processing path as `/transcribe-file`). Returns the
    `job_id`; poll `GET /jobs/{job_id}` for progress.
    """,
)
async def uploads_complete(
    upload_id: str,
    body: UploadCompleteRequest | None = None,
    x_api_key: str = Header(..., alias="X-API-KEY", description="Your API key for authentication"),
):
    verify_api_key(x_api_key)
    session = upload_manager.get(upload_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Upload session not found")

    # Generate the job id first so the assembled file shares the job_id prefix the worker's
    # cleanup matches on — identical convention to /transcribe-file.
    job_id = str(uuid.uuid4())
    dest_path = os.path.join(_transcription_work_dir(), f"{job_id}_upload{session['ext']}")
    try:
        upload_manager.complete(upload_id, dest_path)
    except UploadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    callback_url = body.callback_url if body else None
    google_drive_folder_id = body.google_drive_folder_id if body else None
    callback_url_str = _resolve_callback_url(str(callback_url) if callback_url else None)

    job_manager.create_job(
        job_id=job_id,
        callback_url=callback_url_str,
        source_type="local_file",
        local_file_path=dest_path,
        original_filename=session["filename"],
        google_drive_folder_id=google_drive_folder_id or None,
    )

    return TranscribeResponse(job_id=job_id, status="queued")


@app.post(
    "/uploads/{upload_id}/abort",
    tags=["Transcription"],
    summary="Abort a resumable upload",
    description="Discard an in-progress upload session and delete its partial file.",
)
async def uploads_abort(
    upload_id: str,
    x_api_key: str = Header(..., alias="X-API-KEY", description="Your API key for authentication"),
):
    verify_api_key(x_api_key)
    upload_manager.abort(upload_id)
    return {"ok": True}


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
        transcript=job.get("transcript"),
        drive_transcript_file_id=job.get("drive_transcript_file_id"),
        drive_transcript_url=job.get("drive_transcript_url"),
        archived_video_bucket=job.get("archived_video_bucket"),
        archived_video_path=job.get("archived_video_path"),
        thumbnail_b2_path=job.get("thumbnail_b2_path"),
        archive_error=job.get("archive_error"),
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
