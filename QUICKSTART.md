# Quick Start Guide

Get your Media Transcription Service running in 5 minutes!

## Option 1: Local Development (Fastest)

```bash
# 1. Clone and setup
git clone <your-repo>
cd media-transcription-service

# 2. Configure environment
cp .env.example .env
# Edit .env with your API keys

# 3. Run (Windows)
start.bat

# OR Run (Linux/Mac)
chmod +x start.sh
./start.sh
```

Service runs at: `http://localhost:8000`

## Option 2: Docker (Recommended)

```bash
# 1. Setup environment
cp .env.example .env
# Edit .env with your API keys

# 2. Start service
docker-compose up -d

# 3. View logs
docker-compose logs -f
```

Service runs at: `http://localhost:8000`

## Option 3: Deploy to Render (Production)

```bash
# 1. Push to GitHub
git init
git add .
git commit -m "Initial commit"
git remote add origin <your-github-repo>
git push -u origin main

# 2. Deploy on Render
# - Go to https://dashboard.render.com
# - Click "New +" → "Blueprint"
# - Select your repository
# - Add environment variables in dashboard
# - Deploy!
```

Service runs at: `https://your-service.onrender.com`

## Test Your Service

```bash
# Test health
curl http://localhost:8000/health

# Run test suite
python test_api.py
```

## Submit Your First Job

```bash
curl -X POST http://localhost:8000/transcribe \
  -H "X-API-KEY: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "b2_bucket": "your-bucket",
    "b2_file_path": "path/to/video.mp4",
    "callback_url": "https://your-webhook.com/callback"
  }'
```

Response:
```json
{
  "job_id": "abc-123-def",
  "status": "queued"
}
```

## Check Job Status

```bash
curl http://localhost:8000/jobs/abc-123-def \
  -H "X-API-KEY: your-api-key"
```

## What Happens Next?

1. Service downloads media from Backblaze
2. Extracts audio (if video)
3. Splits into 10-minute chunks
4. Transcribes chunks in parallel
5. Merges transcripts
6. Sends result to your webhook

## Need Help?

- **Full Documentation:** See `README.md`
- **Deployment Guide:** See `DEPLOYMENT.md`
- **Deployment Checklist:** See `CHECKLIST.md`
- **API Reference:** Visit `/docs` endpoint when service is running

## Common Issues

**"Connection refused"**
- Service not running. Run `start.bat` or `docker-compose up`

**"Invalid API key"**
- Check `.env` file has correct `API_KEY`
- Verify `X-API-KEY` header matches

**"File not found"**
- Verify B2 bucket and file path are correct
- Check B2 credentials in `.env`

**"Transcription failed"**
- Verify OpenAI API key has credits
- Check OpenAI API status

## Next Steps

1. ✅ Get service running locally
2. ✅ Test with sample file
3. ✅ Deploy to Render
4. ✅ Integrate with your workflow
5. ✅ Monitor and scale as needed

Happy transcribing! 🎙️
