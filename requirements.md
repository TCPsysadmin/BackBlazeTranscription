# Requirements Document

## Introduction

The Media Transcription Service is a reliable, scalable backend service that transcribes long-form media files stored in Backblaze using OpenAI's transcription API. The service handles asynchronous processing, audio extraction from video, intelligent chunking for large files, parallel transcription, automatic retries.

## Glossary

- **Service**: The Media Transcription Service backend system
- **Job**: A single transcription request with associated processing state
- **Chunk**: A fixed-duration segment of audio extracted from the source media
- **n8n**: The workflow orchestration platform that consumes transcription results
- **Backblaze**: The B2 cloud storage service hosting source media files
- **OpenAI_API**: The OpenAI transcription service used for speech-to-text conversion
- **Callback_URL**: The webhook endpoint provided by n8n to receive job results
- **Job_ID**: A unique identifier assigned to each transcription job

## Requirements

### Requirement 1: Job Creation and Queuing

**User Story:** As an n8n workflow, I want to submit a transcription job asynchronously, so that I can process long media files without blocking.

#### Acceptance Criteria

1. WHEN a POST request is received at /transcribe with valid b2_bucket, b2_file_path, and callback_url, THE Service SHALL create a new Job and return a Job_ID with status "queued"
2. WHEN a POST request is received at /transcribe without a callback_url, THE Service SHALL reject the request with HTTP 400
3. WHEN a POST request is received at /transcribe with an invalid API key, THE Service SHALL reject the request with HTTP 401
4. WHEN a Job is created, THE Service SHALL assign a unique Job_ID
5. WHEN a Job is queued, THE Service SHALL begin processing within 60 seconds

### Requirement 2: Media Download and Validation

**User Story:** As the Service, I want to download and validate media files from Backblaze, so that I can ensure files are accessible and in supported formats before processing.

#### Acceptance Criteria

1. WHEN processing a Job, THE Service SHALL download the media file from the specified b2_bucket and b2_file_path
2. IF the media file does not exist in Backblaze, THEN THE Service SHALL fail the Job with status "failed" and error "file_not_found"
3. IF the media file format is unsupported, THEN THE Service SHALL fail the Job with status "failed" and error "unsupported_format"
4. WHEN a Job fails during download or validation, THE Service SHALL post the failure status to the Callback_URL

### Requirement 3: Audio Extraction

**User Story:** As the Service, I want to extract audio from video files, so that I can transcribe media regardless of whether it is audio-only or video.

#### Acceptance Criteria

1. WHEN the source media is a video file, THE Service SHALL extract the audio track
2. WHEN the source media is an audio file, THE Service SHALL use it directly without extraction
3. WHEN audio extraction fails, THE Service SHALL fail the Job and post the failure to the Callback_URL

### Requirement 4: Audio Chunking

**User Story:** As the Service, I want to split large audio files into fixed-duration chunks, so that I can process files larger than OpenAI's size limits and avoid timeouts.

#### Acceptance Criteria

1. WHEN processing audio longer than 600 seconds, THE Service SHALL split the audio into chunks of 600 seconds duration
2. WHEN creating chunks, THE Service SHALL ensure each chunk is under 20 MB in size
3. WHEN creating chunks, THE Service SHALL maintain chronological order with sequential chunk identifiers
4. WHEN the audio is 600 seconds or shorter, THE Service SHALL process it as a single chunk

### Requirement 5: Parallel Transcription

**User Story:** As the Service, I want to transcribe audio chunks in parallel, so that I can reduce total processing time for long media files.

#### Acceptance Criteria

1. WHEN multiple chunks exist for a Job, THE Service SHALL transcribe chunks concurrently
2. WHEN transcribing chunks, THE Service SHALL respect OpenAI_API rate limits
3. WHEN a chunk transcription completes, THE Service SHALL update the Job progress percentage
4. WHEN all chunks are transcribed, THE Service SHALL proceed to transcript aggregation

### Requirement 6: Retry Logic

**User Story:** As the Service, I want to automatically retry failed transcription requests, so that transient errors do not cause job failures.

#### Acceptance Criteria

1. WHEN a chunk transcription fails with HTTP status 408, 429, 500, 502, 503, or 504, THE Service SHALL retry the transcription
2. WHEN retrying a chunk, THE Service SHALL use exponential backoff between attempts
3. WHEN a chunk has been retried 3 times, THE Service SHALL fail the Job
4. WHEN a chunk transcription fails with a non-retryable error, THE Service SHALL fail the Job immediately

### Requirement 7: Transcript Aggregation

**User Story:** As an n8n workflow, I want to receive a single merged transcript, so that I can use the complete transcription without manual assembly.

#### Acceptance Criteria

1. WHEN all chunks are successfully transcribed, THE Service SHALL merge the chunk transcripts in chronological order
2. WHEN merging transcripts, THE Service SHALL preserve the original temporal sequence
3. WHEN the merged transcript is complete, THE Service SHALL update the Job status to "completed"

### Requirement 8: Job Status Tracking

**User Story:** As an n8n workflow, I want to query job status and progress, so that I can monitor transcription progress or recover from callback failures.

#### Acceptance Criteria

1. WHEN a GET request is received at /jobs/{job_id}, THE Service SHALL return the current Job status
2. WHEN a Job is processing, THE Service SHALL return a progress value between 0 and 100
3. WHEN a Job has failed, THE Service SHALL include an error message in the response
4. WHEN a Job_ID does not exist, THE Service SHALL return HTTP 404

### Requirement 9: Webhook Callbacks

**User Story:** As an n8n workflow, I want to receive webhook callbacks when jobs complete or fail, so that I can continue my workflow without polling.

#### Acceptance Criteria

1. WHEN a Job completes successfully, THE Service SHALL POST the Job_ID, status "completed", and full transcript to the Callback_URL within 30 seconds
2. WHEN a Job fails, THE Service SHALL POST the Job_ID, status "failed", and error message to the Callback_URL within 30 seconds
3. IF the webhook POST fails, THEN THE Service SHALL retry the callback delivery
4. WHEN the callback is delivered, THE Service SHALL include the Job_ID in the payload

### Requirement 10: Data Cleanup

**User Story:** As the Service, I want to delete temporary files after job completion, so that I do not accumulate storage costs or retain user data unnecessarily.

#### Acceptance Criteria

1. WHEN a Job completes or fails, THE Service SHALL delete the downloaded media file
2. WHEN a Job completes or fails, THE Service SHALL delete all audio chunks
3. WHEN a Job completes, THE Service SHALL retain the transcript only as long as required to deliver the callback

### Requirement 11: Idempotency

**User Story:** As an n8n workflow, I want duplicate job submissions to be handled gracefully, so that network retries do not create redundant processing.

#### Acceptance Criteria

1. WHEN a Job submission is received with identical b2_bucket, b2_file_path, and callback_url to an existing queued or processing Job, THE Service SHALL return the existing Job_ID
2. WHEN a Job submission is received with identical parameters to a completed Job, THE Service SHALL create a new Job

### Requirement 12: Security and Authentication

**User Story:** As the Service, I want to authenticate API requests, so that only authorized clients can submit transcription jobs.

#### Acceptance Criteria

1. WHEN a request is received without the X-API-KEY header, THE Service SHALL reject the request with HTTP 401
2. WHEN a request is received with an invalid X-API-KEY, THE Service SHALL reject the request with HTTP 401
3. WHEN a request is received with a valid X-API-KEY, THE Service SHALL process the request
4. THE Service SHALL require HTTPS for all API endpoints

### Requirement 13: Error Handling and Logging

**User Story:** As a system operator, I want comprehensive logging of job lifecycle events, so that I can troubleshoot failures and monitor system health.

#### Acceptance Criteria

1. WHEN a Job is created, THE Service SHALL log the Job_ID and request parameters
2. WHEN a chunk transcription succeeds or fails, THE Service SHALL log the chunk identifier and outcome
3. WHEN a retry occurs, THE Service SHALL log the retry attempt number and reason
4. WHEN logging, THE Service SHALL NOT include transcript content or sensitive data
5. WHEN an unexpected error occurs, THE Service SHALL log the error details and fail the Job gracefully
