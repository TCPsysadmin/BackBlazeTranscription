# Queue Management & Concurrency Control

## Overview

The transcription service implements a robust job queue system with concurrency control to prevent resource overload and ensure reliable processing.

## How It Works

### Job Submission
1. Client submits a job via `/transcribe` or `/transcribeHTTP`
2. Job is created with status `"queued"`
3. Job ID is returned immediately to the client
4. Job is stored in memory (JobManager)

### Background Processing
1. Worker polls for queued jobs every 5 seconds
2. Worker checks if max concurrent jobs limit is reached
3. If capacity available, worker starts processing the job
4. Job status changes to `"processing"`
5. When complete, job status changes to `"completed"` or `"failed"`

## Concurrency Control

### Configuration
Set the maximum number of concurrent jobs via environment variable:

```bash
MAX_CONCURRENT_JOBS=3  # Default: 3
```

### How It Prevents Overload

**1. Semaphore Limiting**
- Uses `asyncio.Semaphore` to limit concurrent job processing
- Only `MAX_CONCURRENT_JOBS` can run simultaneously
- Additional jobs wait in queue until a slot opens

**2. Active Job Tracking**
- Maintains a set of currently processing job IDs
- Prevents duplicate processing of the same job
- Automatically removes job from active set when complete

**3. Status-Based Filtering**
- Only jobs with status `"queued"` are picked up
- Once status changes to `"processing"`, job won't be picked up again
- Prevents race conditions in the polling loop

### Example Flow

With `MAX_CONCURRENT_JOBS=3`:

```
Time 0s:  Submit 10 jobs → All marked "queued"
Time 0s:  Worker starts jobs 1, 2, 3 → Status: "processing"
Time 5s:  Worker checks queue → Jobs 4-10 still "queued", but max reached
Time 30s: Job 1 completes → Status: "completed"
Time 35s: Worker checks queue → Starts job 4 → Status: "processing"
...and so on
```

## Resource Management

### Per-Job Resources
Each job uses:
- 1 download connection (B2 or HTTP)
- 1 ffmpeg process (for audio extraction/chunking)
- 3 concurrent OpenAI API calls (for chunk transcription)
- Temporary disk space for media files

### Total Concurrent Resources
With `MAX_CONCURRENT_JOBS=3`:
- 3 downloads at once
- 3 ffmpeg processes
- Up to 9 OpenAI API calls (3 jobs × 3 chunks each)

### Recommended Settings

**Small Server (1-2 GB RAM):**
```bash
MAX_CONCURRENT_JOBS=1
```

**Medium Server (4-8 GB RAM):**
```bash
MAX_CONCURRENT_JOBS=3  # Default
```

**Large Server (16+ GB RAM):**
```bash
MAX_CONCURRENT_JOBS=5
```

**Note:** Higher concurrency increases throughput but also increases:
- Memory usage
- Network bandwidth
- OpenAI API rate limit usage
- Disk I/O

## Monitoring

### Check Active Jobs
The worker logs:
```
INFO: Transcription worker started (max concurrent jobs: 3)
INFO: Processing job abc-123
INFO: Job abc-123: Downloading from B2
INFO: Job abc-123: Completed successfully
```

### Queue Status
Jobs remain in queue until:
1. A processing slot becomes available
2. Worker picks them up (every 5 seconds)
3. Processing begins

### Idempotency
Submitting the same job multiple times returns the existing job ID:
- Same bucket + file path + callback URL = Same job
- Prevents duplicate processing
- Safe to retry failed submissions

## Failure Handling

### Job Failures
- Failed jobs are marked `"failed"` with error message
- Failure webhook is sent to callback URL
- Job slot is freed for next queued job
- No automatic retry (client must resubmit)

### Worker Failures
- Worker catches exceptions and continues running
- Individual job failures don't crash the worker
- Worker logs errors for debugging

### Network Issues
- B2 downloads: 3 retries with exponential backoff
- OpenAI transcription: 3 retries per chunk
- Webhook delivery: 3 retries with exponential backoff

## Best Practices

### For High Volume
1. Set appropriate `MAX_CONCURRENT_JOBS` for your server
2. Monitor memory and CPU usage
3. Consider scaling horizontally (multiple instances)
4. Use webhook callbacks instead of polling job status

### For Large Files
1. Lower `MAX_CONCURRENT_JOBS` to avoid memory issues
2. Ensure sufficient disk space for temporary files
3. Monitor OpenAI API rate limits
4. Consider increasing timeout values

### For Reliability
1. Implement webhook retry logic on your end
2. Store job IDs for status checking
3. Handle idempotent resubmissions
4. Monitor failed jobs and investigate errors

## Troubleshooting

### Jobs Not Processing
- Check worker is running: Look for "Transcription worker started" log
- Check max concurrent jobs: May be at capacity
- Check job status: Should be "queued" not "processing"

### Too Slow
- Increase `MAX_CONCURRENT_JOBS` if resources allow
- Check network bandwidth (downloads may be bottleneck)
- Check OpenAI API rate limits

### Out of Memory
- Decrease `MAX_CONCURRENT_JOBS`
- Check for large files (they use more memory)
- Ensure temporary files are being cleaned up

### Rate Limiting
- OpenAI may rate limit if too many concurrent requests
- Decrease `MAX_CONCURRENT_JOBS`
- Spread out job submissions over time
