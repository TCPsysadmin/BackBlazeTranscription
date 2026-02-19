# Troubleshooting Guide

Common issues and their solutions for the Media Transcription Service.

## Job Failures

### Error: `bucket_not_found`

**Symptom:**
```
Job failed: bucket_not_found: my-bucket
```

**Cause:** The specified B2 bucket doesn't exist or the credentials don't have access to it.

**Solutions:**
1. Verify the bucket name is correct (case-sensitive)
2. Check B2 credentials have access to the bucket
3. Ensure the bucket exists in your B2 account
4. Verify B2_KEY_ID and B2_APPLICATION_KEY are correct

**Test:**
```bash
# List your B2 buckets to verify the name
b2 list-buckets
```

---

### Error: `file_not_found`

**Symptom:**
```
Job failed: file_not_found: path/to/file.mp4
```

**Cause:** The file doesn't exist at the specified path in the B2 bucket.

**Solutions:**
1. Verify the file path is correct (case-sensitive)
2. Check the file exists in the bucket
3. Ensure the path doesn't have leading slashes
4. Verify file hasn't been deleted

**Test:**
```bash
# List files in your bucket
b2 ls b2://my-bucket/path/to/
```

---

### Error: `unsupported_format`

**Symptom:**
```
Job failed: unsupported_format: .xyz
Job failed: unsupported_format:  (empty)
```

**Cause:** The file format is not supported, or the file extension wasn't detected.

**Supported Formats:**
- Audio: mp3, wav, m4a, flac, ogg, aac
- Video: mp4, mov, avi, mkv, webm

**Solutions:**
1. Ensure the file in B2 has a proper extension (e.g., `.mp3`, `.mp4`)
2. Convert the file to a supported format
3. Use ffmpeg to convert: `ffmpeg -i input.xyz output.mp4`
4. Check that the filename in B2 includes the extension

**Note:** As of v1.2.0, the service preserves file extensions from B2, so this error should be rare.

---

### Error: `audio_extraction_failed`

**Symptom:**
```
Job failed: audio_extraction_failed: [error details]
Job failed: audio_extraction_failed: ffmpeg extraction failed
```

**Cause:** Could not extract audio from the video file.

**Solutions:**
1. Verify the file is not corrupted
2. Check the file has an audio track
3. Try re-uploading the file to B2
4. Convert to a different format
5. Ensure ffmpeg is installed (required for large files)

**Test:**
```bash
# Check if file has audio track
ffmpeg -i file.mp4

# Check if ffmpeg is installed
ffmpeg -version
```

**Note:** As of v1.2.0, the service uses ffmpeg for better performance. If ffmpeg is not available, it falls back to pydub (slower but works).

---

### Error: `transcription_failed`

**Symptom:**
```
Job failed: transcription_failed: [OpenAI error]
```

**Cause:** OpenAI API error during transcription.

**Common Causes:**
1. **Insufficient credits:** OpenAI account has no credits
2. **Rate limit:** Too many requests to OpenAI
3. **Invalid API key:** OPENAI_API_KEY is incorrect
4. **File too large:** Chunk exceeds 25MB (shouldn't happen with 600s chunks)

**Solutions:**
1. Check OpenAI account has credits
2. Verify OPENAI_API_KEY is correct
3. Wait and retry if rate limited
4. Check OpenAI API status: https://status.openai.com

---

### Error: `download_error` or `b2_error`

**Symptom:**
```
Job failed: download_error: [network error]
Job failed: b2_error: [B2 API error]
```

**Cause:** Network issue or B2 service error.

**Solutions:**
1. Check internet connectivity
2. Verify B2 service status
3. Check B2 credentials are valid
4. Retry the job
5. Check firewall/proxy settings

---

## Webhook Issues

### Webhook Not Received

**Symptom:** Job completes but webhook never arrives.

**Logs:**
```
WARNING: Webhook failed (attempt 1/3): HTTP 404 for url
ERROR: Webhook failed after 3 attempts
```

**Causes:**
1. **Invalid URL:** Webhook URL doesn't exist (404)
2. **Network issue:** Webhook endpoint unreachable
3. **Timeout:** Endpoint takes too long to respond
4. **SSL issue:** HTTPS certificate problem

**Solutions:**

1. **Verify webhook URL is correct:**
   ```bash
   # Test your webhook endpoint
   curl -X POST https://your-webhook.com/callback \
     -H "Content-Type: application/json" \
     -d '{"test": "data"}'
   ```

2. **Use webhook testing service:**
   - https://webhook.site - Get a test URL
   - Submit job with test URL
   - Verify payload is received

3. **Check webhook endpoint logs:**
   - Ensure endpoint is running
   - Check for errors in your webhook handler
   - Verify endpoint returns 200 status

4. **Retrieve results via API:**
   ```bash
   # Even if webhook fails, you can get results
   curl https://your-service.onrender.com/jobs/{job_id} \
     -H "X-API-KEY: your-api-key"
   ```

---

### Webhook Returns Error

**Symptom:** Webhook is received but your endpoint returns an error.

**Solutions:**
1. Check your webhook handler code for bugs
2. Verify payload parsing is correct
3. Add error handling to your webhook
4. Return 200 status even if processing fails
5. Process webhook asynchronously

**Example (Python):**
```python
@app.post("/webhook")
async def webhook(request: Request):
    try:
        payload = await request.json()
        # Process in background
        asyncio.create_task(process_transcript(payload))
        # Return immediately
        return {"status": "received"}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        # Still return 200 to acknowledge receipt
        return {"status": "error", "message": str(e)}
```

---

## Service Issues

### Service Won't Start

**Symptom:**
```
Error: No module named 'services'
```

**Solutions:**
1. Ensure you're in the correct directory
2. Check virtual environment is activated
3. Install dependencies: `pip install -r requirements.txt`
4. Verify Python version is 3.11+

---

### Service Crashes

**Symptom:** Service stops unexpectedly.

**Check logs for:**
1. Out of memory errors
2. Missing environment variables
3. Invalid API keys
4. Disk space issues

**Solutions:**
1. Check environment variables are set
2. Verify all API keys are valid
3. Ensure sufficient disk space for temp files
4. Check system resources (RAM, CPU)
5. Review logs: `docker-compose logs -f`

---

### Jobs Stuck in "queued"

**Symptom:** Jobs never start processing.

**Causes:**
1. Worker not running
2. Service crashed
3. Too many concurrent jobs

**Solutions:**
1. Restart the service
2. Check logs for errors
3. Verify worker is running
4. Check system resources

---

## API Issues

### 401 Unauthorized

**Symptom:**
```json
{"detail": "Invalid API key"}
```

**Solutions:**
1. Verify X-API-KEY header is included
2. Check API key matches API_KEY environment variable
3. Ensure no extra spaces in API key
4. Check header name is exactly "X-API-KEY"

**Test:**
```bash
# Correct
curl -H "X-API-KEY: your-key" https://your-service.com/health

# Wrong - missing header
curl https://your-service.com/health
```

---

### 404 Not Found

**Symptom:**
```json
{"detail": "Job not found"}
```

**Solutions:**
1. Verify job ID is correct
2. Check job wasn't created on different instance
3. Ensure job ID is complete (UUID format)

---

### 422 Validation Error

**Symptom:**
```json
{"detail": [{"loc": ["body", "callback_url"], "msg": "field required"}]}
```

**Solutions:**
1. Check all required fields are provided
2. Verify JSON format is correct
3. Ensure callback_url is a valid URL
4. Check Content-Type header is "application/json"

---

## Performance Issues

### Slow Transcription

**Causes:**
1. Large file size
2. OpenAI API rate limits
3. Network speed
4. Server resources
5. Missing ffmpeg (falls back to slower pydub)

**Solutions:**
1. Files are processed in parallel chunks (automatic)
2. Upgrade Render plan for more resources
3. Check OpenAI API rate limits
4. Monitor B2 download speeds
5. Ensure ffmpeg is installed for optimal performance

**Expected Times (with ffmpeg):**
- 10 min audio: ~10-20 minutes
- 1 hour audio: ~30-60 minutes
- 3.5GB file (2-4 hours): ~2-8 hours
- Processing is typically 1-2x real-time

**Performance Comparison:**
- With ffmpeg: Fast, memory-efficient
- Without ffmpeg (pydub fallback): 10x slower, high memory usage

---

### High Memory Usage

**Symptom:** Service crashes with out of memory errors.

**Solutions:**
1. Upgrade Render plan (recommend at least 2GB RAM for large files)
2. Ensure ffmpeg is installed (uses 10x less memory than pydub)
3. Check for memory leaks
4. Verify temp files are being cleaned up
5. Consider file size limits if needed

**Memory Usage (3.5GB file):**
- With ffmpeg: 200-500 MB
- Without ffmpeg: 4-8 GB (may crash)

---

### Render free tier (512MB RAM / limited disk)

**Symptom:** Jobs fail with out-of-memory, or temp storage exceeds 2GB.

The service is optimized for low-memory and minimal temp storage:

1. **Defaults:** `MAX_CONCURRENT_JOBS=1` and `CONCURRENT_CHUNKS=1` keep peak disk and RAM low.
2. **Temp cleanup:** Original media is deleted as soon as audio is extracted; extracted audio is deleted after chunking; each chunk file is deleted immediately after it is transcribed.
3. **ffmpeg required:** On free tier, ffmpeg must be installed. The pydub fallback loads entire files into RAM and will exceed 512MB on large files.

**Recommended env (Render free):**
```bash
MAX_CONCURRENT_JOBS=1
CONCURRENT_CHUNKS=1
```

**If you still hit limits:** Prefer shorter files or use a paid plan with more RAM/disk. Do not increase `CONCURRENT_CHUNKS` or `MAX_CONCURRENT_JOBS` on 512MB.

---

## Large File Issues

### Out of Disk Space

**Symptom:**
```
Job failed: No space left on device
```
Temp storage exceeds available disk (e.g. Render ephemeral filesystem). The app now deletes media after extraction, extracted audio after chunking, and each chunk after transcription to minimize peak usage.

**Cause:** Large files require significant temporary storage.

**Disk Space Required:**
- Original file size
- Extracted audio (~50% of video size)
- Chunks (same as audio size)
- Total: ~2-3x original file size

**Solutions:**
1. Upgrade Render plan with more disk space
2. Ensure temp files are being cleaned up
3. Process smaller files or split large files
4. Monitor disk usage in logs

### ffmpeg Not Found

**Symptom:**
```
WARNING: ffmpeg not found, falling back to pydub (slower for large files)
```

**Impact:** Processing will be 10x slower and use 10x more memory.

**Solutions:**
1. Install ffmpeg on your system
2. For Docker: Already included in Dockerfile
3. For Render: ffmpeg should be available by default
4. For local development: Install ffmpeg separately

**Verify ffmpeg:**
```bash
ffmpeg -version
ffprobe -version
```

---

## Debugging Tips

### Enable Detailed Logging

Add to your `.env`:
```bash
LOG_LEVEL=DEBUG
```

### Check Service Health

```bash
curl https://your-service.onrender.com/health
```

### View Job Status

```bash
curl https://your-service.onrender.com/jobs/{job_id} \
  -H "X-API-KEY: your-api-key"
```

### Test with Small File

Start with a small test file (< 1 minute) to verify the pipeline works.

### Check API Documentation

Visit `/docs` endpoint for interactive API testing.

---

## Getting Help

If you're still stuck:

1. **Check logs:**
   - Local: Console output
   - Docker: `docker-compose logs -f`
   - Render: Dashboard → Logs tab

2. **Verify configuration:**
   - Environment variables set correctly
   - API keys are valid
   - Bucket and file paths are correct

3. **Test components individually:**
   - Health endpoint
   - B2 access
   - OpenAI API
   - Webhook endpoint

4. **Review documentation:**
   - `README.md` - Full documentation
   - `API_EXAMPLES.md` - Code examples
   - `DEPLOYMENT.md` - Deployment guide

5. **Check service status:**
   - OpenAI: https://status.openai.com
   - Backblaze: https://status.backblaze.com
   - Render: https://status.render.com

---

## Common Patterns

### Successful Job Flow

```
1. POST /transcribe → 200 OK (job_id returned)
2. Job status: queued
3. Job status: processing (progress 0-100%)
4. Job status: completed
5. Webhook received with transcript
```

### Failed Job Flow

```
1. POST /transcribe → 200 OK (job_id returned)
2. Job status: queued
3. Job status: processing
4. Job status: failed (error message included)
5. Webhook received with error
6. GET /jobs/{job_id} to retrieve error details
```

### Webhook Failure Flow

```
1. Job completes successfully
2. Webhook delivery fails (404, timeout, etc.)
3. Service retries 3 times
4. Job status remains "completed"
5. Transcript available via GET /jobs/{job_id}
```
