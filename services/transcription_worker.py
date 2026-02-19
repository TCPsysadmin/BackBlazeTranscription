"""Background worker for processing transcription jobs"""
import asyncio
import logging
import os
import tempfile
from pathlib import Path

from services.media_processor import MediaProcessor
from services.openai_client import OpenAITranscriber
from services.b2_client import B2Client
from services.webhook_client import WebhookClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# Reduce noise from B2/requests connection pool (pool full is non-fatal)
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

# Limit concurrent chunk transcriptions per job (1 = minimal disk/RAM; safe for 512MB)
CONCURRENT_CHUNKS_DEFAULT = int(os.getenv("CONCURRENT_CHUNKS", "1"))
# Chunk duration in seconds; longer = fewer chunk files on disk (default 600 = 10 min)
CHUNK_DURATION_SECONDS = int(os.getenv("CHUNK_DURATION_SECONDS", "600"))


class TranscriptionWorker:
    """Processes transcription jobs in the background (optimized for low memory/disk)"""
    
    def __init__(self, job_manager, openai_api_key: str, b2_key_id: str, b2_app_key: str, max_concurrent_jobs: int = 1):
        self.job_manager = job_manager
        self.openai_client = OpenAITranscriber(openai_api_key)
        self.b2_client = B2Client(b2_key_id, b2_app_key)
        self.webhook_client = WebhookClient()
        self.media_processor = MediaProcessor()
        self.running = True
        self.temp_dir = tempfile.mkdtemp()
        self.max_concurrent_jobs = max_concurrent_jobs
        self.active_jobs = set()  # Track currently processing job IDs
        self.semaphore = asyncio.Semaphore(max_concurrent_jobs)  # Limit concurrent jobs
        self.max_concurrent_chunks = CONCURRENT_CHUNKS_DEFAULT
    
    def stop(self):
        """Stop the worker"""
        self.running = False
    
    async def process_jobs(self):
        """Main worker loop"""
        logger.info(f"Transcription worker started (max concurrent jobs: {self.max_concurrent_jobs})")
        
        while self.running:
            try:
                queued_jobs = self.job_manager.get_queued_jobs()
                
                for job in queued_jobs:
                    job_id = job["job_id"]
                    
                    # Skip if already processing
                    if job_id in self.active_jobs:
                        continue
                    
                    # Try to acquire semaphore without blocking
                    if self.semaphore.locked():
                        logger.debug(f"Max concurrent jobs ({self.max_concurrent_jobs}) reached, waiting...")
                        break  # Don't start more jobs this iteration
                    
                    # Mark as active and start processing
                    self.active_jobs.add(job_id)
                    asyncio.create_task(self._process_job_wrapper(job_id))
                
                await asyncio.sleep(5)  # Check for new jobs every 5 seconds
            except Exception as e:
                logger.error(f"Worker error: {e}")
                await asyncio.sleep(5)
    
    async def _process_job_wrapper(self, job_id: str):
        """Wrapper to handle semaphore and cleanup"""
        async with self.semaphore:
            try:
                await self.process_job(job_id)
            finally:
                # Remove from active jobs when done
                self.active_jobs.discard(job_id)
    
    async def process_job(self, job_id: str):
        """Process a single transcription job"""
        job = self.job_manager.get_job(job_id)
        if not job or job["status"] != "queued":
            return
        
        logger.info(f"Processing job {job_id}")
        self.job_manager.update_job(job_id, status="processing")
        
        temp_files = []
        
        try:
            # Stream B2 → ffmpeg → segments: never write full file to disk (avoids 2GB+ eviction)
            logger.info(f"Job {job_id}: Streaming B2 → ffmpeg → segments (no full file on disk)")
            transcripts = await self._stream_b2_to_transcription(job_id, job)
            
            # Merge transcripts
            logger.info(f"Job {job_id}: Merging transcripts")
            full_transcript = self._merge_transcripts(transcripts)
            
            # Upload transcript to B2 (if requested)
            transcript_b2_path = None
            if job.get("upload_transcript", False):
                try:
                    logger.info(f"Job {job_id}: Uploading transcript to B2")
                    transcript_b2_path = await self._upload_transcript(job, full_transcript)
                    logger.info(f"Job {job_id}: Transcript uploaded to {transcript_b2_path}")
                except Exception as upload_error:
                    logger.warning(f"Job {job_id}: Failed to upload transcript to B2: {upload_error}")
                    # Don't fail the job if upload fails, just log it
            else:
                logger.info(f"Job {job_id}: Skipping transcript upload (not requested)")
            
            # Update job and send callback
            self.job_manager.update_job(
                job_id,
                status="completed",
                progress=100,
                transcript=full_transcript
            )
            
            webhook_payload = {
                "job_id": job_id,
                "status": "completed",
                "b2_bucket": job["b2_bucket"],
                "b2_file_path": job["b2_file_path"],
                "transcript": full_transcript
            }
            
            # Add transcript B2 path if upload succeeded
            if transcript_b2_path:
                webhook_payload["transcript_b2_path"] = transcript_b2_path
            
            webhook_success = await self.webhook_client.send_callback(
                job["callback_url"],
                webhook_payload
            )
            
            if webhook_success:
                logger.info(f"Job {job_id}: Completed successfully")
            else:
                logger.warning(f"Job {job_id}: Completed but webhook delivery failed")
            
        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}", exc_info=True)
            error_msg = str(e)
            
            self.job_manager.update_job(
                job_id,
                status="failed",
                error=error_msg
            )
            
            # Try to send failure webhook, but don't fail if it doesn't work
            try:
                webhook_success = await self.webhook_client.send_callback(
                    job["callback_url"],
                    {
                        "job_id": job_id,
                        "status": "failed",
                        "b2_bucket": job["b2_bucket"],
                        "b2_file_path": job["b2_file_path"],
                        "error": error_msg
                    }
                )
                
                if not webhook_success:
                    logger.warning(
                        f"Job {job_id}: Failed and webhook delivery also failed. "
                        f"Job status can be retrieved via API."
                    )
            except Exception as webhook_error:
                logger.error(
                    f"Job {job_id}: Failed and webhook delivery raised exception: {webhook_error}. "
                    f"Job status can be retrieved via API."
                )
        
        finally:
            # Cleanup any temp files (streaming path uses none; kept for consistency)
            self._cleanup_files(temp_files)
    
    async def _stream_b2_to_transcription(self, job_id: str, job: dict) -> list[str]:
        """Stream B2 file through ffmpeg to segment files; transcribe each segment and delete.
        Never writes the full media file to disk, so stays under 2GB temp limit.
        """
        from services.b2_client import B2DownloadError
        
        segment_dir = os.path.join(self.temp_dir, f"{job_id}_segments")
        os.makedirs(segment_dir, exist_ok=True)
        try:
            r, w = os.pipe()
            try:
                # Open write end for B2; B2 will close it when done
                stream = os.fdopen(w, "wb")
                w = -1  # so we don't close it again
                
                # Start B2 download writing to pipe (runs in executor, closes stream when done)
                b2_task = asyncio.create_task(
                    self.b2_client.download_to_stream(
                        job["b2_bucket"], job["b2_file_path"], stream
                    )
                )
                
                # ffmpeg reads from pipe, writes chunk_000.mp3, chunk_001.mp3, ... to segment_dir
                proc = await self.media_processor.run_ffmpeg_stream_to_segments(
                    r, segment_dir, CHUNK_DURATION_SECONDS
                )
                r = -1  # ffmpeg process holds it
                
                # Process segments as they complete: transcribe and delete
                results = {}
                processed = set()
                poll_interval = 2.0
                
                while True:
                    # List current segment files
                    indices = set()
                    for name in os.listdir(segment_dir):
                        if name.startswith("chunk_") and name.endswith(".mp3"):
                            try:
                                idx = int(name[6:9])  # chunk_XXX.mp3
                                indices.add(idx)
                            except ValueError:
                                pass
                    
                    for i in sorted(indices):
                        if i in processed:
                            continue
                        # Chunk i is complete when chunk i+1 exists or ffmpeg has exited
                        if (i + 1) in indices or proc.returncode is not None:
                            path = os.path.join(segment_dir, f"chunk_{i:03d}.mp3")
                            if os.path.exists(path):
                                try:
                                    transcript = await self._transcribe_chunk_with_retry(
                                        job_id, path, i, max(indices) + 1 if indices else 1
                                    )
                                    results[i] = transcript
                                    self.job_manager.update_progress(
                                        job_id, len(results), max(indices) + 1 if indices else 1
                                    )
                                finally:
                                    self._cleanup_files([path])
                                processed.add(i)
                    
                    if proc.returncode is not None:
                        if not indices or len(processed) >= len(indices):
                            break
                    await asyncio.sleep(poll_interval)
                
                await b2_task
                await proc.wait()
                if proc.returncode != 0 and proc.returncode is not None:
                    stderr = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
                    raise Exception(f"ffmpeg_stream_failed: exit {proc.returncode} {stderr[:500]}")
                
                # Return transcripts in order
                num_chunks = max(results.keys()) + 1 if results else 0
                self.job_manager.update_job(job_id, chunks_total=num_chunks)
                return [results.get(i, "") for i in range(num_chunks)]
                
            finally:
                if w >= 0:
                    try:
                        os.close(w)
                    except OSError:
                        pass
                if r >= 0:
                    try:
                        os.close(r)
                    except OSError:
                        pass
        except B2DownloadError as e:
            raise Exception(str(e))
        except Exception as e:
            raise Exception(f"stream_failed: {e}")
        finally:
            # Remove segment dir (any remaining files)
            try:
                for name in os.listdir(segment_dir):
                    self._cleanup_files([os.path.join(segment_dir, name)])
                os.rmdir(segment_dir)
            except Exception as ex:
                logger.warning(f"Cleanup segment dir: {ex}")
    
    async def _download_media(self, job: dict) -> str:
        """Download media file from B2"""
        from services.b2_client import B2DownloadError
        
        try:
            # Extract file extension from B2 file path
            file_extension = Path(job["b2_file_path"]).suffix
            if not file_extension:
                file_extension = ".bin"  # Fallback for files without extension
            
            # Preserve extension in local filename
            local_path = os.path.join(self.temp_dir, f"{job['job_id']}_media{file_extension}")
            
            await self.b2_client.download_file(
                job["b2_bucket"],
                job["b2_file_path"],
                local_path
            )
            return local_path
        except B2DownloadError as e:
            # Re-raise B2 errors with their specific error messages
            raise Exception(str(e))
        except Exception as e:
            raise Exception(f"download_failed: {e}")
    
    async def _extract_audio(self, media_path: str) -> str:
        """Extract audio from media file"""
        try:
            return await self.media_processor.extract_audio(media_path)
        except Exception as e:
            raise Exception(f"audio_extraction_failed: {e}")
    
    async def _chunk_audio(self, audio_path: str) -> list[str]:
        """Split audio into chunks (duration from env for fewer chunks on low-disk)"""
        try:
            return await self.media_processor.chunk_audio(
                audio_path, chunk_duration=CHUNK_DURATION_SECONDS
            )
        except Exception as e:
            raise Exception(f"chunking_failed: {e}")
    
    async def _transcribe_chunks_incremental(
        self, job_id: str, audio_path: str, num_chunks: int, duration_seconds: float
    ) -> list[str]:
        """Transcribe chunks incrementally: create chunk → transcribe → delete → repeat.
        Peak disk usage: audio file + 1 chunk file (or max_concurrent_chunks chunk files).
        This prevents exceeding temp storage limits on large files.
        """
        max_concurrent = max(1, self.max_concurrent_chunks)
        results = [None] * num_chunks
        
        # If only 1 chunk and it's the original audio file, transcribe directly
        if num_chunks == 1:
            logger.info(f"Job {job_id}: Single chunk, transcribing original audio file")
            transcript = await self._transcribe_chunk_with_retry(job_id, audio_path, 0, 1)
            return [transcript]
        
        # Process chunks in batches (but create/delete incrementally within each batch)
        for batch_start in range(0, num_chunks, max_concurrent):
            batch_size = min(max_concurrent, num_chunks - batch_start)
            batch_tasks = []
            
            # Create and transcribe chunks in this batch
            for i in range(batch_size):
                chunk_index = batch_start + i
                task = self._create_and_transcribe_chunk(
                    job_id, audio_path, chunk_index, num_chunks
                )
                batch_tasks.append((chunk_index, task))
            
            # Wait for batch to complete
            batch_transcripts = await asyncio.gather(*[t for _, t in batch_tasks])
            for (chunk_index, _), transcript in zip(batch_tasks, batch_transcripts):
                results[chunk_index] = transcript
            
            logger.info(
                f"Job {job_id}: Completed batch {batch_start // max_concurrent + 1}/{(num_chunks + max_concurrent - 1) // max_concurrent}"
            )
        
        return results
    
    async def _create_and_transcribe_chunk(
        self, job_id: str, audio_path: str, chunk_index: int, total_chunks: int
    ) -> str:
        """Create a single chunk, transcribe it, then delete it immediately.
        Returns the transcript text.
        """
        chunk_path = None
        try:
            # Create chunk on-demand
            chunk_path = await self.media_processor.create_single_chunk(
                audio_path,
                chunk_index,
                CHUNK_DURATION_SECONDS,
                self.temp_dir
            )
            
            if chunk_path is None:
                # Chunk past end of audio (shouldn't happen if num_chunks calculated correctly)
                logger.warning(f"Job {job_id}: Chunk {chunk_index} is past end of audio")
                return ""
            
            # Transcribe chunk
            transcript = await self._transcribe_chunk_with_retry(
                job_id, chunk_path, chunk_index, total_chunks
            )
            
            return transcript
            
        finally:
            # Always delete chunk file immediately after use (even if transcription failed)
            if chunk_path:
                self._cleanup_files([chunk_path])
    
    async def _transcribe_chunks(self, job_id: str, chunks: list[str]) -> list[str]:
        """Legacy method: Transcribe pre-created chunks. Kept for backward compatibility.
        Use _transcribe_chunks_incremental for new code to minimize disk usage.
        """
        max_concurrent = max(1, self.max_concurrent_chunks)
        results = [None] * len(chunks)  # preserve order

        for i in range(0, len(chunks), max_concurrent):
            batch = chunks[i:i + max_concurrent]
            batch_tasks = []
            for chunk_path in batch:
                chunk_index = chunks.index(chunk_path)
                task = self._transcribe_chunk_with_retry(job_id, chunk_path, chunk_index, len(chunks))
                batch_tasks.append((chunk_index, chunk_path, task))

            batch_transcripts = await asyncio.gather(*[t for _, _, t in batch_tasks])
            for (chunk_index, chunk_path, _), transcript in zip(batch_tasks, batch_transcripts):
                results[chunk_index] = transcript
                # Free disk immediately after transcription; chunk no longer needed
                self._cleanup_files([chunk_path])
            logger.info(
                f"Job {job_id}: Completed batch {i // max_concurrent + 1}/{(len(chunks) + max_concurrent - 1) // max_concurrent}"
            )
        return results
    
    async def _transcribe_chunk_with_retry(
        self, job_id: str, chunk_path: str, chunk_index: int, total_chunks: int
    ) -> str:
        """Transcribe a single chunk with retry logic"""
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                transcript = await self.openai_client.transcribe(chunk_path)
                
                # Update progress
                chunks_completed = chunk_index + 1
                self.job_manager.update_progress(job_id, chunks_completed, total_chunks)
                
                logger.info(f"Job {job_id}: Chunk {chunk_index + 1}/{total_chunks} completed")
                return transcript
                
            except Exception as e:
                retry_count += 1
                error_type = type(e).__name__
                error_code = getattr(e, "status_code", None)
                
                # Check if error is retryable
                # Retry on: timeout errors, rate limits, and server errors
                retryable_codes = [408, 429, 500, 502, 503, 504]
                is_timeout = "timeout" in error_type.lower() or "timeout" in str(e).lower()
                is_retryable = error_code in retryable_codes or is_timeout
                
                if not is_retryable or retry_count >= max_retries:
                    logger.error(f"Job {job_id}: Chunk {chunk_index} failed after {retry_count} attempts: {e}")
                    raise Exception(f"transcription_failed: {e}")
                
                # Exponential backoff
                wait_time = 2 ** retry_count
                logger.warning(
                    f"Job {job_id}: Chunk {chunk_index} retry {retry_count}/{max_retries} "
                    f"after {wait_time}s (error: {error_type})"
                )
                await asyncio.sleep(wait_time)
        
        raise Exception(f"transcription_failed: max retries exceeded")
    
    def _merge_transcripts(self, transcripts: list[str]) -> str:
        """Merge chunk transcripts in order"""
        return " ".join(transcripts)
    
    async def _upload_transcript(self, job: dict, transcript: str) -> str:
        """Upload transcript as .txt file to B2 in the same directory as the original file"""
        from services.b2_client import B2UploadError
        
        try:
            # Generate transcript filename based on original file path
            original_path = job["b2_file_path"]
            
            # Remove extension and add .txt
            # e.g., "folder/audio.mp3" -> "folder/audio_transcript.txt"
            path_without_ext = os.path.splitext(original_path)[0]
            transcript_path = f"{path_without_ext}_transcript.txt"
            
            # Create temporary file with transcript content
            temp_transcript_path = os.path.join(self.temp_dir, f"{job['job_id']}_transcript.txt")
            with open(temp_transcript_path, 'w', encoding='utf-8') as f:
                f.write(transcript)
            
            # Upload to B2
            await self.b2_client.upload_file(
                job["b2_bucket"],
                temp_transcript_path,
                transcript_path
            )
            
            # Clean up temp file
            try:
                os.remove(temp_transcript_path)
            except Exception as e:
                logger.warning(f"Failed to delete temp transcript file: {e}")
            
            return transcript_path
            
        except B2UploadError as e:
            raise Exception(f"Failed to upload transcript: {e}")
        except Exception as e:
            raise Exception(f"Failed to upload transcript: {e}")
    
    def _cleanup_files(self, file_paths: list[str]):
        """Delete temporary files"""
        for path in file_paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
                    logger.debug(f"Deleted temp file: {path}")
            except Exception as e:
                logger.warning(f"Failed to delete {path}: {e}")
