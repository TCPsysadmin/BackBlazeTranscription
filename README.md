# Media Transcription Service

A scalable backend service that transcribes long-form media files from Backblaze using OpenAI's Whisper API.

## Project Structure

```
.
├── main.py                 # FastAPI application entry point
├── services/               # Service layer components
│   ├── __init__.py
│   ├── job_manager.py      # Job state management
│   ├── transcription_worker.py  # Background job processor
│   ├── b2_client.py        # Backblaze B2 client
│   ├── openai_client.py    # OpenAI Whisper client
│   ├── media_processor.py  # Audio extraction & chunking
│   └── webhook_client.py   # Webhook callback handler
├── Dockerfile              # Docker container definition
├── docker-compose.yml      # Docker Compose configuration
├── render.yaml             # Render deployment config
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variables template
├── .gitignore              # Git ignore rules
├── .dockerignore           # Docker ignore rules
├── README.md               # This file
├── QUICKSTART.md           # 5-minute getting started guide
├── DEPLOYMENT.md           # Detailed deployment guide
├── CHECKLIST.md            # Deployment checklist
├── TROUBLESHOOTING.md      # Common issues and solutions
├── LARGE_FILES.md          # Large file handling guide
├── API_EXAMPLES.md         # Code examples in multiple languages
└── CHANGELOG.md            # Version history and changes
```

## Features

- Asynchronous job processing with webhook callbacks
- Audio extraction from video files using ffmpeg (optimized for large files)
- Intelligent chunking for large files (600s chunks, stream-based processing)
- Parallel transcription processing
- Automatic transcript upload to B2 (saved as .txt file in same directory)
- Automatic retry logic with exponential backoff
- Idempotent job submission
- API key authentication
- Comprehensive error handling and logging
- Optimized for large files (tested with 3.5GB+ files)

## Quick Start

### Using Start Scripts

**Windows:**
```bash
# 1. Copy environment template
copy .env.example .env

# 2. Edit .env with your API keys
notepad .env

# 3. Run start script
start.bat
```

**Linux/Mac:**
```bash
# 1. Copy environment template
cp .env.example .env

# 2. Edit .env with your API keys
nano .env

# 3. Make script executable and run
chmod +x start.sh
./start.sh
```

### Using Docker

```bash
# 1. Copy environment template
cp .env.example .env

# 2. Edit .env with your API keys

# 3. Start with Docker Compose
docker-compose up -d

# View logs
docker-compose logs -f

# Stop service
docker-compose down
```

## Setup

### Local Development

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Install ffmpeg (required for audio processing):
- Windows: See `WINDOWS_SETUP.md` for detailed instructions
- Linux: `sudo apt-get install ffmpeg`
- Mac: `brew install ffmpeg`

**Note:** ffmpeg is required for efficient processing of large files. The service will fall back to slower methods if ffmpeg is not available, but this is not recommended for production use.

3. Configure environment variables:
```bash
cp .env.example .env
# Edit .env with your credentials
```

4. Run the service:
```bash
python main.py
```

The service will start on `http://localhost:8000`

### Docker Deployment

1. Build the Docker image:
```bash
docker build -t media-transcription-service .
```

2. Run with Docker Compose:
```bash
docker-compose up -d
```

3. Or run directly:
```bash
docker run -p 8000:8000 \
  -e API_KEY=your-api-key \
  -e OPENAI_API_KEY=your-openai-key \
  -e B2_KEY_ID=your-b2-key-id \
  -e B2_APPLICATION_KEY=your-b2-app-key \
  media-transcription-service
```

### Render Deployment

1. Push your code to GitHub

2. Connect your repository to Render:
   - Go to https://dashboard.render.com
   - Click "New +" → "Blueprint"
   - Connect your GitHub repository
   - Render will automatically detect `render.yaml`

3. Set environment variables in Render dashboard:
   - `API_KEY`: Your service API key
   - `OPENAI_API_KEY`: Your OpenAI API key
   - `B2_ARCHIVE_KEY_ID`: Server-only key ID for permanent local-upload storage
   - `B2_ARCHIVE_APPLICATION_KEY`: Server-only application key
   - `B2_ARCHIVE_BUCKET`: Bucket that receives original videos and thumbnails
   - `B2_VIDEO_PREFIX`: Optional video prefix (default `videos`)
   - `B2_THUMBNAIL_PREFIX`: Optional thumbnail prefix (default `thumbnails`)
   - `THUMBNAIL_AT_SECONDS`: Optional frame timestamp (default `3`)

When archive credentials are configured, local video uploads are saved under
`<video-prefix>/<job-id>/<original-name>` before transcription. Video uploads also
receive a 640×360 WebP thumbnail under
`<thumbnail-prefix>/<job-id>/thumbnail.webp`. The completed job response returns
both paths so downstream ingestion can associate them with the video record.

4. Deploy! Render will build and deploy automatically.

Your service will be available at: `https://your-service-name.onrender.com`

## Testing

Run the test script to verify your deployment:

```bash
# Local testing
python test_api.py

# Test remote deployment
BASE_URL=https://your-service-name.onrender.com python test_api.py
```

The test script will:
- Check health endpoint
- Verify authentication
- Test job creation
- Test job status retrieval

## API Endpoints

### Interactive Documentation

Visit `/docs` when the service is running to access the beautiful Scalar API documentation with:
- Complete endpoint descriptions with examples
- Request/response schemas
- Try-it-out functionality (test endpoints directly from the browser)
- Webhook payload documentation
- Code generation in multiple languages

**Local:** http://localhost:8000/docs  
**Production:** https://your-service.onrender.com/docs

The root URL (`/`) automatically redirects to the documentation.

For complete code examples in multiple languages, see `API_EXAMPLES.md`.

### POST /transcribe
Submit a transcription job.

**Headers:**
- `X-API-KEY`: Your API key

**Request Body:**
```json
{
  "b2_bucket": "my-bucket",
  "b2_file_path": "path/to/media.mp4",
  "callback_url": "https://your-webhook.com/callback"
}
```

**Response:**
```json
{
  "job_id": "uuid",
  "status": "queued"
}
```

### GET /jobs/{job_id}
Get job status and progress.

**Headers:**
- `X-API-KEY`: Your API key

**Response:**
```json
{
  "job_id": "uuid",
  "status": "processing",
  "progress": 45,
  "error": null,
  "transcript": null
}
```

### Webhook Callback
When a job completes or fails, the service POSTs to your callback URL:

**Success:**
```json
{
  "job_id": "uuid",
  "status": "completed",
  "transcript": "Full transcription text..."
}
```

**Failure:**
```json
{
  "job_id": "uuid",
  "status": "failed",
  "error": "Error description"
}
```

## Architecture

- `main.py`: FastAPI application and API endpoints
- `services/`:
  - `job_manager.py`: Job state management
  - `transcription_worker.py`: Background job processor
  - `b2_client.py`: Backblaze B2 integration
  - `openai_client.py`: OpenAI Whisper API client
  - `media_processor.py`: Audio extraction and chunking
  - `webhook_client.py`: Webhook callback delivery

## Error Handling

The service handles various error scenarios:
- `bucket_not_found`: B2 bucket doesn't exist
- `file_not_found`: Media file doesn't exist in B2
- `unsupported_format`: Media format not supported
- `audio_extraction_failed`: Failed to extract audio
- `chunking_failed`: Failed to split audio
- `transcription_failed`: OpenAI API error
- `download_error`: Network or B2 access error
- `b2_error`: B2 API error

Retryable HTTP errors (408, 429, 500, 502, 503, 504) are automatically retried up to 3 times with exponential backoff.

**Webhook Delivery:** If webhook delivery fails (e.g., 404, network error), the service will:
1. Retry up to 3 times with exponential backoff
2. Skip retries for client errors (4xx except 408, 429)
3. Log the failure but keep job status available via API
4. Allow you to retrieve results via GET /jobs/{job_id}

## Supported Formats

**Audio:** mp3, wav, m4a, flac, ogg, aac
**Video:** mp4, mov, avi, mkv, webm

**File Size:** Optimized for large files (tested with 3.5GB+ files)
- Uses stream-based processing (doesn't load entire file into memory)
- ffmpeg-based extraction and chunking for performance
- Automatic fallback to pydub if ffmpeg not available

For detailed information on large file handling, see `LARGE_FILES.md`.

## Troubleshooting

For detailed troubleshooting of common issues, see `TROUBLESHOOTING.md`.

Quick checks:
- Verify environment variables are set correctly
- Check API keys are valid
- Ensure B2 bucket and file paths are correct
- Test webhook URL is accessible
- Review service logs for detailed error messages

## Configuration

- Chunk duration: 600 seconds (10 minutes)
- Max chunk size: 20 MB
- Max retries: 3
- Webhook retries: 3
- Job check interval: 5 seconds
