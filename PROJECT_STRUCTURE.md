# BackBlaze Transcription Service — Project Structure

## Overview

A FastAPI service that accepts audio/video files stored in Backblaze B2, transcribes them via OpenAI Whisper, and delivers results via webhook or polling. Designed for large files with minimal disk/memory footprint.

---

## File Map

```
BackBlazeTranscription/
├── main.py                     # FastAPI app, all REST endpoints, app lifecycle
├── requirements.txt            # Python dependencies
├── .env.example                # Environment variable template
├── Dockerfile                  # Python 3.11-slim + ffmpeg image
├── docker-compose.yml          # Local Docker deployment
├── render.yaml                 # Render.com deployment config
├── start.sh                    # Linux/Mac startup script
├── start.bat                   # Windows startup script
├── test_api.py                 # API integration test runner
├── test.py                     # Additional test file
│
├── services/
│   ├── __init__.py
│   ├── job_manager.py          # In-memory job state store
│   ├── transcription_worker.py # Background job processor (core logic)
│   ├── b2_client.py            # Backblaze B2 download/upload
│   ├── openai_client.py        # OpenAI Whisper transcription calls
│   ├── media_processor.py      # ffmpeg audio extraction and chunking
│   └── webhook_client.py       # Webhook delivery with retry
│
├── README.md
├── QUICKSTART.md
├── DEPLOYMENT.md
├── LARGE_FILES.md
├── QUEUE_MANAGEMENT.md
├── TROUBLESHOOTING.md
├── WINDOWS_SETUP.md
├── API_EXAMPLES.md
├── CHECKLIST.md
├── CHANGELOG.md
└── requirements.md
```

---

## API Endpoints

All endpoints defined in [main.py](main.py). Authentication via `X-API-Key` header (except `/health`).

| Method | Path | Description | Location |
|--------|------|-------------|----------|
| POST | `/transcribe` | Submit job from B2 bucket/path | [main.py:195](main.py#L195) |
| POST | `/transcribeHTTP` | Submit job from B2 HTTPS URL | [main.py:266](main.py#L266) |
| GET | `/jobs/{job_id}` | Poll job status and progress | [main.py:368](main.py#L368) |
| GET | `/health` | Health check (no auth) | [main.py:449](main.py#L449) |
| GET | `/docs` | Scalar API documentation | [main.py:487](main.py#L487) |

---

## Services

### [services/job_manager.py](services/job_manager.py) — `JobManager`

In-memory job store (dict + threading lock). No persistence — jobs lost on restart.

| Method | Line | Purpose |
|--------|------|---------|
| `create_job()` | ~30 | Creates job with UUID, stores metadata |
| `get_job()` | ~55 | Retrieves job dict by ID |
| `update_job()` | ~65 | Thread-safe field updates |
| `find_existing_job()` | ~80 | Idempotency: finds matching queued/processing job |
| `get_queued_jobs()` | ~100 | Returns all jobs with status "queued" |
| `update_progress()` | ~110 | Recalculates progress % from chunk counts |

---

### [services/transcription_worker.py](services/transcription_worker.py) — `TranscriptionWorker`

Background async loop that processes queued jobs. Core orchestration logic lives here.

| Method | Line | Purpose |
|--------|------|---------|
| `process_jobs()` | ~40 | Main loop: polls every 5s, respects `MAX_CONCURRENT_JOBS` |
| `process_job()` | ~70 | Orchestrates full pipeline for a single job |
| `_stream_b2_to_transcription()` | ~130 | **Streaming path**: pipes B2 → ffmpeg (no full file write) |
| `_incremental_chunk_transcription()` | ~200 | **Fallback path**: download → extract → chunk incrementally |
| `_transcribe_chunk_with_retry()` | ~270 | Transcribes one chunk, retries up to 3x on failure |
| `_transcribe_chunks_incremental()` | ~310 | Creates/transcribes/deletes chunks in batches |
| `_create_and_transcribe_chunk()` | ~360 | Creates a single chunk file, transcribes it, deletes immediately |
| `_upload_transcript()` | ~400 | Uploads completed transcript as `.txt` to B2 |
| `_cleanup_files()` | ~430 | Deletes temp files |

**Processing path selection** (in `process_job()`):
- Audio formats (mp3, wav, m4a, flac, ogg, aac) → try streaming path first
- Video formats (mp4, mov, avi, mkv, webm) → incremental path (metadata at end of file prevents streaming)
- Streaming path failure → falls back to incremental

---

### [services/b2_client.py](services/b2_client.py) — `B2Client`

Wraps `b2sdk` for file operations. Uses executor threads for sync SDK calls.

| Method | Line | Purpose |
|--------|------|---------|
| `_get_api()` | ~20 | Lazy-init B2 API with authorization |
| `download_file()` | ~35 | Download to local path with retry |
| `download_to_stream()` | ~80 | Stream download to pipe (used by streaming path) |
| `upload_file()` | ~110 | Upload local file to B2 bucket |

---

### [services/openai_client.py](services/openai_client.py) — `OpenAITranscriber`

Thin wrapper around `AsyncOpenAI`. Calls Whisper API with a 900s timeout.

| Method | Line | Purpose |
|--------|------|---------|
| `transcribe()` | ~15 | Sends audio file to Whisper, returns text |

---

### [services/media_processor.py](services/media_processor.py) — `MediaProcessor`

All ffmpeg/pydub audio operations. ffmpeg is preferred; pydub is the fallback.

| Method | Line | Purpose |
|--------|------|---------|
| `extract_audio()` | ~20 | Returns as-is if audio, else extracts from video |
| `chunk_audio()` | ~50 | Splits audio into fixed-duration chunks |
| `get_chunk_info()` | ~100 | Returns `(duration_seconds, num_chunks)` without creating files |
| `create_single_chunk()` | ~130 | Creates one chunk file at a given index |
| `run_ffmpeg_stream_to_segments()` | ~160 | Async ffmpeg subprocess: reads from pipe, writes segment files |
| `_get_audio_duration()` | ~200 | Uses ffprobe (fallback: pydub) |
| `_chunk_with_ffmpeg()` | ~230 | Fast stream-copy chunking |
| `_chunk_with_pydub()` | ~260 | Fallback: loads entire file to RAM |

---

### [services/webhook_client.py](services/webhook_client.py) — `WebhookClient`

Delivers JSON callbacks via `httpx`. Retries 3 times with exponential backoff.

| Method | Line | Purpose |
|--------|------|---------|
| `send_callback()` | ~15 | POSTs payload to callback URL, handles retries |

---

## Data Flow

```
Client
  │
  ▼
POST /transcribe or /transcribeHTTP
  │  Authenticate → idempotency check → create job (UUID) → return job_id
  ▼
JobManager (in-memory)
  │
  ▼
TranscriptionWorker (background loop, polls every 5s)
  │
  ├─ [Audio file] ──► Streaming path
  │                    B2 download → pipe → ffmpeg → segments
  │
  └─ [Video file] ──► Incremental path
                       B2 download → ffmpeg extract audio
                           → create chunk → transcribe → delete chunk (repeat)
  │
  ▼
OpenAI Whisper (per chunk, concurrent up to CONCURRENT_CHUNKS)
  │  Retry up to 3x on failure
  ▼
Merge transcripts in order
  │
  ├─ (optional) Upload .txt transcript to B2
  │
  ▼
Update job: status=completed, progress=100, transcript=<text>
  │
  ▼
Send webhook POST to callback_url (retry 3x)
  │
  ▼
Client polls GET /jobs/{job_id}  ─OR─  receives webhook callback
```

---

## Configuration

All config via environment variables (see [.env.example](.env.example)):

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | `your-secret-api-key` | Service authentication key |
| `OPENAI_API_KEY` | — | OpenAI API key (required) |
| `B2_KEY_ID` | — | Backblaze key ID (required) |
| `B2_APPLICATION_KEY` | — | Backblaze application key (required) |
| `MAX_CONCURRENT_JOBS` | `1` | Max jobs processed simultaneously |
| `CONCURRENT_CHUNKS` | `1` | Max concurrent Whisper calls per job |
| `CHUNK_DURATION_SECONDS` | `600` | Audio chunk length (10 min default) |
| `B2_STREAM_TIMEOUT` | `7200` | B2 download timeout in seconds |

---

## Supported Formats

- **Audio** (streaming path): mp3, wav, m4a, flac, ogg, aac
- **Video** (incremental path): mp4, mov, avi, mkv, webm

---

## Job States

`queued` → `processing` → `completed` / `failed`

Job objects include: `job_id`, `status`, `progress` (0–100), `transcript`, `error`, `b2_bucket`, `b2_file_path`, `callback_url`, `upload_transcript`.

---

## Key Design Decisions

- **In-memory storage** — simple and fast; single-instance only; no persistence across restarts
- **Streaming path** — audio files never fully written to disk; piped directly through ffmpeg
- **Incremental chunking** — video files chunked one at a time; each chunk deleted after transcription
- **Async throughout** — all I/O is non-blocking; sync SDK calls wrapped in `asyncio.to_thread` / executor
- **Idempotency** — duplicate requests with same bucket/path/callback return the existing job ID
- **Webhook is best-effort** — job completes regardless of webhook success; results always available via polling

---

## External Dependencies

- **ffmpeg + ffprobe** — required for audio extraction and chunking
- **OpenAI Whisper API** — `whisper-1` model via `openai` SDK
- **Backblaze B2** — `b2sdk` for downloads/uploads
- **FastAPI + uvicorn** — web framework and ASGI server
