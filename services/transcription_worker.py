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


class TranscriptionWorker:
    """Processes transcription jobs in the background"""
    
    def __init__(self, job_manager, openai_api_key: str, b2_key_id: str, b2_app_key: str):
        self.job_manager = job_manager
        self.openai_client = OpenAITranscriber(openai_api_key)
        self.b2_client = B2Client(b2_key_id, b2_app_key)
        self.webhook_client = WebhookClient()
        self.media_processor = MediaProcessor()
        self.running = True
        self.temp_dir = tempfile.mkdtemp()
    
    def stop(self):
        """Stop the worker"""
        self.running = False
    
    async def process_jobs(self):
        """Main worker loop"""
        logger.info("Transcription worker started")
        
        while self.running:
            try:
                queued_jobs = self.job_manager.get_queued_jobs()
                
                for job in queued_jobs:
                    asyncio.create_task(self.process_job(job["job_id"]))
                
                await asyncio.sleep(5)  # Check for new jobs every 5 seconds
            except Exception as e:
                logger.error(f"Worker error: {e}")
                await asyncio.sleep(5)
    
    async def process_job(self, job_id: str):
        """Process a single transcription job"""
        job = self.job_manager.get_job(job_id)
        if not job or job["status"] != "queued":
            return
        
        logger.info(f"Processing job {job_id}")
        self.job_manager.update_job(job_id, status="processing")
        
        temp_files = []
        
        try:
            # Step 1: Download media from B2
            logger.info(f"Job {job_id}: Downloading from B2")
            media_path = await self._download_media(job)
            temp_files.append(media_path)
            
            # Step 2: Extract audio
            logger.info(f"Job {job_id}: Extracting audio")
            audio_path = await self._extract_audio(media_path)
            if audio_path != media_path:
                temp_files.append(audio_path)
            
            # Step 3: Chunk audio
            logger.info(f"Job {job_id}: Chunking audio")
            chunks = await self._chunk_audio(audio_path)
            temp_files.extend(chunks)
            
            self.job_manager.update_job(job_id, chunks_total=len(chunks))
            
            # Step 4: Transcribe chunks in parallel
            logger.info(f"Job {job_id}: Transcribing {len(chunks)} chunks")
            transcripts = await self._transcribe_chunks(job_id, chunks)
            
            # Step 5: Merge transcripts
            logger.info(f"Job {job_id}: Merging transcripts")
            full_transcript = self._merge_transcripts(transcripts)
            
            # Step 6: Update job and send callback
            self.job_manager.update_job(
                job_id,
                status="completed",
                progress=100,
                transcript=full_transcript
            )
            
            await self.webhook_client.send_callback(
                job["callback_url"],
                {
                    "job_id": job_id,
                    "status": "completed",
                    "transcript": full_transcript
                }
            )
            
            logger.info(f"Job {job_id}: Completed successfully")
            
        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}")
            error_msg = str(e)
            
            self.job_manager.update_job(
                job_id,
                status="failed",
                error=error_msg
            )
            
            await self.webhook_client.send_callback(
                job["callback_url"],
                {
                    "job_id": job_id,
                    "status": "failed",
                    "error": error_msg
                }
            )
        
        finally:
            # Cleanup temp files
            self._cleanup_files(temp_files)
    
    async def _download_media(self, job: dict) -> str:
        """Download media file from B2"""
        try:
            local_path = os.path.join(self.temp_dir, f"{job['job_id']}_media")
            await self.b2_client.download_file(
                job["b2_bucket"],
                job["b2_file_path"],
                local_path
            )
            return local_path
        except FileNotFoundError:
            raise Exception("file_not_found")
        except Exception as e:
            raise Exception(f"download_failed: {e}")
    
    async def _extract_audio(self, media_path: str) -> str:
        """Extract audio from media file"""
        try:
            return await self.media_processor.extract_audio(media_path)
        except Exception as e:
            raise Exception(f"audio_extraction_failed: {e}")
    
    async def _chunk_audio(self, audio_path: str) -> list[str]:
        """Split audio into chunks"""
        try:
            return await self.media_processor.chunk_audio(audio_path, chunk_duration=600)
        except Exception as e:
            raise Exception(f"chunking_failed: {e}")
    
    async def _transcribe_chunks(self, job_id: str, chunks: list[str]) -> list[str]:
        """Transcribe all chunks in parallel"""
        tasks = []
        for i, chunk_path in enumerate(chunks):
            task = self._transcribe_chunk_with_retry(job_id, chunk_path, i, len(chunks))
            tasks.append(task)
        
        return await asyncio.gather(*tasks)
    
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
                error_code = getattr(e, "status_code", None)
                
                # Check if error is retryable
                retryable_codes = [408, 429, 500, 502, 503, 504]
                if error_code not in retryable_codes or retry_count >= max_retries:
                    logger.error(f"Job {job_id}: Chunk {chunk_index} failed: {e}")
                    raise Exception(f"transcription_failed: {e}")
                
                # Exponential backoff
                wait_time = 2 ** retry_count
                logger.warning(f"Job {job_id}: Chunk {chunk_index} retry {retry_count}/{max_retries} after {wait_time}s")
                await asyncio.sleep(wait_time)
        
        raise Exception(f"transcription_failed: max retries exceeded")
    
    def _merge_transcripts(self, transcripts: list[str]) -> str:
        """Merge chunk transcripts in order"""
        return " ".join(transcripts)
    
    def _cleanup_files(self, file_paths: list[str]):
        """Delete temporary files"""
        for path in file_paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
                    logger.debug(f"Deleted temp file: {path}")
            except Exception as e:
                logger.warning(f"Failed to delete {path}: {e}")
