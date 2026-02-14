# API Examples

Complete examples for using the Media Transcription Service API.

## Authentication

All requests (except `/health`) require the `X-API-KEY` header:

```bash
X-API-KEY: your-secret-api-key
```

## Example 1: Basic Transcription Job

Submit a video file for transcription using B2 credentials:

```bash
curl -X POST http://localhost:8000/transcribe \
  -H "X-API-KEY: your-secret-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "b2_bucket": "TCPTRANSFER",
    "b2_file_path": "TCP07_PORTAL/audio.mp3",
    "callback_url": "https://your-webhook.com/callback"
  }'
```

```bash
curl -X POST http://localhost:8000/transcribeHTTP \
  -H "X-API-KEY: your-secret-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "media_url": "https://f005.backblazeb2.com/file/TCPTRANSFER/folder/audio.mp3",
    "callback_url": "https://your-webhook.com/callback"
  }'
```

**With transcript upload to B2:**

```bash
curl -X POST http://localhost:8000/transcribe \
  -H "X-API-KEY: your-secret-api-key" \
  -H "X-Upload-Transcript: true" \
  -H "Content-Type: application/json" \
  -d '{
    "b2_bucket": "TCPTRANSFER",
    "b2_file_path": "TCP07_PORTAL/audio.mp3",
    "callback_url": "https://your-webhook.com/callback"
  }'
```
```bash
curl -X POST https://asanatranscription.onrender.com/transcribeHTTP \
  -H "X-API-KEY: your-secret-api-key" \
  -H "X-Upload-Transcript: true" \
  -H "Content-Type: application/json" \
  -d '{
    "media_url": "https://f005.backblazeb2.com/file/TCPTRANSFER/TCP01_ARCHIVE/PAWCAST/PAWCast%201%20-%20Call%20to%20Action/PAWCast%20Episode%201_%20A%20Call%20To%20Action.mp3",
    "callback_url": "https://your-webhook.com/callback"
  }'
```

**Response:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued"
}
```

## Example 1b: Transcription via HTTP URL

Submit a file using a direct B2 HTTPS URL (extracts bucket and path automatically):

```bash
curl -X POST http://localhost:8000/transcribeHTTP \
  -H "X-API-KEY: your-secret-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "media_url": "https://f005.backblazeb2.com/file/TCPTRANSFER/TCP01_ARCHIVE/PAWCAST/PAWCast%201%20-%20Call%20to%20Action/audio.mp3",
    "callback_url": "https://your-webhook.com/callback"
  }'
```

**With transcript upload to B2:**

```bash
curl -X POST http://localhost:8000/transcribeHTTP \
  -H "X-API-KEY: your-secret-api-key" \
  -H "X-Upload-Transcript: true" \
  -H "Content-Type: application/json" \
  -d '{
    "media_url": "https://f005.backblazeb2.com/file/TCPTRANSFER/TCP01_ARCHIVE/PAWCAST/PAWCast%201%20-%20Call%20to%20Action/audio.mp3",
    "callback_url": "https://your-webhook.com/callback"
  }'
```

**Response:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued"
}
```

**Note:** The endpoint automatically parses the B2 URL to extract:
- Bucket: `TCPTRANSFER`
- File path: `TCP01_ARCHIVE/PAWCAST/PAWCast 1 - Call to Action/audio.mp3`

## Example 2: Check Job Status

Query the status of a transcription job:

```bash
curl https://your-service.onrender.com/jobs/550e8400-e29b-41d4-a716-446655440000 \
  -H "X-API-KEY: your-api-key"
```

**Response (Processing):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "processing",
  "progress": 45,
  "error": null,
  "transcript": null
}
```

**Response (Completed):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "progress": 100,
  "error": null,
  "transcript": "This is the full transcribed text from the audio file..."
}
```

**Response (Failed):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "failed",
  "progress": 0,
  "error": "file_not_found",
  "transcript": null
}
```

## Example 3: Webhook Callback Handler

Example webhook endpoint to receive transcription results:

### Node.js (Express)

```javascript
const express = require('express');
const app = express();

app.use(express.json());

app.post('/webhooks/transcription', (req, res) => {
  const { job_id, status, b2_bucket, b2_file_path, transcript, transcript_b2_path, error } = req.body;
  
  if (status === 'completed') {
    console.log(`Job ${job_id} completed!`);
    console.log(`File: ${b2_bucket}/${b2_file_path}`);
    console.log(`Transcript: ${transcript}`);
    if (transcript_b2_path) {
      console.log(`Transcript uploaded to: ${b2_bucket}/${transcript_b2_path}`);
    }
    
    // Process the transcript
    // Save to database, send notification, etc.
    
  } else if (status === 'failed') {
    console.error(`Job ${job_id} failed: ${error}`);
    console.error(`File: ${b2_bucket}/${b2_file_path}`);
    
    // Handle failure
    // Retry, notify user, etc.
  }
  
  // Always respond with 200 to acknowledge receipt
  res.status(200).json({ received: true });
});

app.listen(3000);
```

### Python (FastAPI)

```python
from fastapi import FastAPI, Request
from pydantic import BaseModel

app = FastAPI()

class WebhookPayload(BaseModel):
    job_id: str
    status: str
    b2_bucket: str
    b2_file_path: str
    transcript: str | None = None
    transcript_b2_path: str | None = None  # Path to uploaded transcript .txt file
    error: str | None = None

@app.post("/webhooks/transcription")
async def transcription_webhook(payload: WebhookPayload):
    if payload.status == "completed":
        print(f"Job {payload.job_id} completed!")
        print(f"File: {payload.b2_bucket}/{payload.b2_file_path}")
        print(f"Transcript: {payload.transcript}")
        if payload.transcript_b2_path:
            print(f"Transcript uploaded to: {payload.b2_bucket}/{payload.transcript_b2_path}")
        
        # Process the transcript
        # Save to database, send notification, etc.
        
    elif payload.status == "failed":
        print(f"Job {payload.job_id} failed: {payload.error}")
        print(f"File: {payload.b2_bucket}/{payload.b2_file_path}")
        
        # Handle failure
        # Retry, notify user, etc.
    
    # Always respond with 200 to acknowledge receipt
    return {"received": True}
```

### n8n Workflow

1. Add a **Webhook** node:
   - Method: POST
   - Path: `/transcription-callback`
   - Response Mode: "Respond Immediately"

2. Add an **IF** node:
   - Condition: `{{ $json.status }}` equals `completed`

3. **True Branch** - Add your processing nodes:
   - Save transcript to database
   - Send notification
   - Trigger next workflow

4. **False Branch** - Handle errors:
   - Log error
   - Send alert
   - Retry logic

5. Use the webhook URL from step 1 as your `callback_url` when submitting jobs

## Example 4: Python Client

Complete Python client for the transcription service:

```python
import requests
import time

class TranscriptionClient:
    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.headers = {
            'X-API-KEY': api_key,
            'Content-Type': 'application/json'
        }
    
    def submit_job(self, b2_bucket, b2_file_path, callback_url, upload_transcript=False):
        """Submit a transcription job"""
        headers = {
            'X-API-KEY': self.api_key,
            'Content-Type': 'application/json'
        }
        
        if upload_transcript:
            headers['X-Upload-Transcript'] = 'true'
        
        response = requests.post(
            f"{self.base_url}/transcribe",
            headers=headers,
            json={
                'b2_bucket': b2_bucket,
                'b2_file_path': b2_file_path,
                'callback_url': callback_url
            }
        )
        response.raise_for_status()
        return response.json()
    
    def get_status(self, job_id):
        """Get job status"""
        response = requests.get(
            f"{self.base_url}/jobs/{job_id}",
            headers=self.headers
        )
        response.raise_for_status()
        return response.json()
    
    def wait_for_completion(self, job_id, poll_interval=5, timeout=3600):
        """Wait for job to complete (blocking)"""
        start_time = time.time()
        
        while True:
            if time.time() - start_time > timeout:
                raise TimeoutError(f"Job {job_id} did not complete within {timeout}s")
            
            status = self.get_status(job_id)
            
            if status['status'] == 'completed':
                return status['transcript']
            elif status['status'] == 'failed':
                raise Exception(f"Job failed: {status['error']}")
            
            print(f"Progress: {status['progress']}%")
            time.sleep(poll_interval)

# Usage
client = TranscriptionClient(
    base_url='https://your-service.onrender.com',
    api_key='your-api-key'
)

# Submit job
result = client.submit_job(
    b2_bucket='my-bucket',
    b2_file_path='recordings/interview.mp4',
    callback_url='https://your-app.com/webhook',
    upload_transcript=True  # Upload transcript to B2
)
print(f"Job ID: {result['job_id']}")

# Option 1: Wait for completion (blocking)
transcript = client.wait_for_completion(result['job_id'])
print(f"Transcript: {transcript}")

# Option 2: Check status manually
status = client.get_status(result['job_id'])
print(f"Status: {status['status']}, Progress: {status['progress']}%")
```

## Example 5: JavaScript Client

```javascript
class TranscriptionClient {
  constructor(baseUrl, apiKey) {
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.apiKey = apiKey;
  }

  async submitJob(b2Bucket, b2FilePath, callbackUrl, uploadTranscript = false) {
    const headers = {
      'X-API-KEY': this.apiKey,
      'Content-Type': 'application/json'
    };
    
    if (uploadTranscript) {
      headers['X-Upload-Transcript'] = 'true';
    }

    const response = await fetch(`${this.baseUrl}/transcribe`, {
      method: 'POST',
      headers: headers,
      body: JSON.stringify({
        b2_bucket: b2Bucket,
        b2_file_path: b2FilePath,
        callback_url: callbackUrl
      })
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${await response.text()}`);
    }

    return await response.json();
  }

  async getStatus(jobId) {
    const response = await fetch(`${this.baseUrl}/jobs/${jobId}`, {
      headers: {
        'X-API-KEY': this.apiKey
      }
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${await response.text()}`);
    }

    return await response.json();
  }

  async waitForCompletion(jobId, pollInterval = 5000, timeout = 3600000) {
    const startTime = Date.now();

    while (true) {
      if (Date.now() - startTime > timeout) {
        throw new Error(`Job ${jobId} did not complete within ${timeout}ms`);
      }

      const status = await this.getStatus(jobId);

      if (status.status === 'completed') {
        return status.transcript;
      } else if (status.status === 'failed') {
        throw new Error(`Job failed: ${status.error}`);
      }

      console.log(`Progress: ${status.progress}%`);
      await new Promise(resolve => setTimeout(resolve, pollInterval));
    }
  }
}

// Usage
const client = new TranscriptionClient(
  'https://your-service.onrender.com',
  'your-api-key'
);

// Submit job
const result = await client.submitJob(
  'my-bucket',
  'recordings/interview.mp4',
  'https://your-app.com/webhook',
  true  // Upload transcript to B2
);
console.log(`Job ID: ${result.job_id}`);

// Wait for completion
const transcript = await client.waitForCompletion(result.job_id);
console.log(`Transcript: ${transcript}`);
```

## Example 6: Health Check

Check if the service is running:

```bash
curl https://your-service.onrender.com/health
```

**Response:**
```json
{
  "status": "healthy"
}
```

## Error Handling

### 401 Unauthorized

```json
{
  "detail": "Invalid API key"
}
```

**Solution:** Check your `X-API-KEY` header

### 404 Not Found

```json
{
  "detail": "Job not found"
}
```

**Solution:** Verify the job ID is correct

### 400 Bad Request

```json
{
  "detail": "callback_url is required"
}
```

**Solution:** Ensure all required fields are provided

## Common Error Codes in Webhooks

| Error Code | Description | Solution |
|------------|-------------|----------|
| `bucket_not_found` | B2 bucket doesn't exist | Verify bucket name is correct |
| `file_not_found` | Media file doesn't exist in B2 | Verify file path is correct |
| `unsupported_format` | File format not supported | Use mp3, mp4, wav, etc. |
| `audio_extraction_failed` | Could not extract audio | Check file is not corrupted |
| `transcription_failed` | OpenAI API error | Check API key and credits |
| `download_error` | Network or B2 access error | Check B2 credentials and network |
| `b2_error` | B2 API error | Check B2 service status |

## Rate Limits

The service respects OpenAI's rate limits automatically. For high-volume usage:
- Submit jobs in batches
- Use webhooks instead of polling
- Consider upgrading your OpenAI plan

## Best Practices

1. **Always use webhooks** - More efficient than polling
2. **Implement idempotency** - Service handles duplicate submissions
3. **Handle webhook retries** - Service retries up to 3 times
4. **Validate webhook payloads** - Check job_id matches your records
5. **Set reasonable timeouts** - Large files take time to process
6. **Monitor your costs** - Track OpenAI and B2 usage

## Support

For more examples and documentation:
- Interactive API Docs: `/docs` endpoint
- Full Documentation: `README.md`
- Deployment Guide: `DEPLOYMENT.md`
