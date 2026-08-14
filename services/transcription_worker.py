"""Background worker for processing transcription jobs"""
import asyncio
import logging
import os
import shutil
import tempfile
import re
from pathlib import Path

from services.media_processor import MediaProcessor, NoAudioTrackError
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
# B2 streaming download timeout in seconds (must cover full file transfer; default 7200 = 2 hours)
B2_STREAM_TIMEOUT = float(os.getenv("B2_STREAM_TIMEOUT", "7200"))
# B2 transcript upload timeout (transcripts are tiny, so a low ceiling is safe)
B2_UPLOAD_TIMEOUT = float(os.getenv("B2_UPLOAD_TIMEOUT", "300"))
# Master per-job timeout. Prevents one hung B2/network call from stalling the entire queue.
JOB_TIMEOUT_SECONDS = float(os.getenv("JOB_TIMEOUT_SECONDS", "10800"))  # 3 hours
# Minimum free bytes that must remain on temp disk after factoring in the source file size
# plus expected overhead (extracted audio + 1 chunk). Prevents disk-full hangs.
DISK_SAFETY_MARGIN_BYTES = int(os.getenv("DISK_SAFETY_MARGIN_BYTES", str(512 * 1024 * 1024)))  # 512 MB
# Multiplier applied to source file size to estimate peak disk usage. 1.20 covers source
# + extracted audio (audio is usually ≤15% of video) + 1 active chunk file.
DISK_OVERHEAD_FACTOR = float(os.getenv("DISK_OVERHEAD_FACTOR", "1.20"))
# Base directory for all temp files (Render persistent SSD mount). Falls back to system temp if missing.
TEMP_STORAGE_DIR = os.getenv("TEMP_STORAGE_DIR", "/data")
# Subdirectory inside TEMP_STORAGE_DIR used for in-flight job files; cleared on boot.
TEMP_WORK_SUBDIR = "transcription_work"
B2_KEY_ID = os.getenv("B2_KEY_ID", "").strip()
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY", "").strip()
B2_ARCHIVE_BUCKET = os.getenv("B2_ARCHIVE_BUCKET", "").strip()
B2_VIDEO_PREFIX = os.getenv("B2_VIDEO_PREFIX", "videos").strip().strip("/")
B2_THUMBNAIL_PREFIX = os.getenv("B2_THUMBNAIL_PREFIX", "thumbnails").strip().strip("/")
THUMBNAIL_AT_SECONDS = float(os.getenv("THUMBNAIL_AT_SECONDS", "3"))


class TranscriptionWorker:
    """Processes transcription jobs in the background (optimized for low memory/disk)"""

    def __init__(self, job_manager, openai_api_key: str, max_concurrent_jobs: int = 1):
        self.job_manager = job_manager
        self.openai_client = OpenAITranscriber(openai_api_key)
        # B2 credentials are per-request; B2Client is built per-job inside process_job.
        self.webhook_client = WebhookClient()
        self.media_processor = MediaProcessor()
        self.n8n_drive_url = os.getenv("N8N_DRIVE_WEBHOOK_URL", "")
        self.running = True
        self.temp_dir = self._init_temp_dir()
        self.max_concurrent_jobs = max_concurrent_jobs
        self.active_jobs = set()  # Track currently processing job IDs
        self.semaphore = asyncio.Semaphore(max_concurrent_jobs)  # Limit concurrent jobs
        self.max_concurrent_chunks = CONCURRENT_CHUNKS_DEFAULT

    def _init_temp_dir(self) -> str:
        """Resolve the temp working directory and clear any leftovers from a prior run.
        Prefers the persistent disk at TEMP_STORAGE_DIR (Render SSD). Falls back to system
        temp if the configured path is unavailable (e.g. local dev without the mount).
        Clearing on boot prevents orphaned files from a crashed/restarted worker accumulating.
        """
        if TEMP_STORAGE_DIR and os.path.isdir(TEMP_STORAGE_DIR):
            work_dir = os.path.join(TEMP_STORAGE_DIR, TEMP_WORK_SUBDIR)
        else:
            logger.warning(
                f"TEMP_STORAGE_DIR '{TEMP_STORAGE_DIR}' not available, falling back to system temp"
            )
            work_dir = os.path.join(tempfile.gettempdir(), TEMP_WORK_SUBDIR)

        if os.path.isdir(work_dir):
            self._clear_directory(work_dir)
            logger.info(f"Cleared leftover files in temp dir: {work_dir}")
        os.makedirs(work_dir, exist_ok=True)
        logger.info(f"Using temp dir: {work_dir}")
        return work_dir

    @staticmethod
    def _clear_directory(path: str) -> None:
        """Remove all contents of a directory (but not the directory itself)."""
        for entry in os.listdir(path):
            full = os.path.join(path, entry)
            try:
                if os.path.isdir(full) and not os.path.islink(full):
                    shutil.rmtree(full, ignore_errors=True)
                else:
                    os.remove(full)
            except Exception as e:
                logger.warning(f"Failed to remove {full} during boot cleanup: {e}")
    
    def stop(self):
        """Stop the worker"""
        self.running = False
    
    async def process_jobs(self):
        """Main worker loop"""
        logger.info(f"Transcription worker started (max concurrent jobs: {self.max_concurrent_jobs})")

        while self.running:
            try:
                # Only schedule new tasks when we have free slots. Gating on len(active_jobs)
                # is correct; gating on semaphore.locked() is not, because asyncio.create_task
                # below doesn't yield, so the semaphore stays unlocked until the new task body
                # runs — letting the loop schedule every queued job at once.
                free_slots = self.max_concurrent_jobs - len(self.active_jobs)
                if free_slots > 0:
                    queued_jobs = self.job_manager.get_queued_jobs()
                    for job in queued_jobs:
                        if free_slots <= 0:
                            break
                        job_id = job["job_id"]
                        if job_id in self.active_jobs:
                            continue
                        self.active_jobs.add(job_id)
                        asyncio.create_task(self._process_job_wrapper(job_id))
                        free_slots -= 1

                await asyncio.sleep(5)  # Check for new jobs every 5 seconds
            except Exception as e:
                logger.error(f"Worker error: {e}")
                await asyncio.sleep(5)

    async def _process_job_wrapper(self, job_id: str):
        """Wrapper to handle semaphore, per-job timeout, and cleanup.

        The timeout is critical: a hung b2sdk call (synchronous urllib3 in a thread) can
        otherwise block forever and stall the entire queue, since the semaphore is held
        for the duration of process_job.
        """
        async with self.semaphore:
            try:
                await asyncio.wait_for(self.process_job(job_id), timeout=JOB_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.error(
                    f"Job {job_id}: exceeded master timeout of {JOB_TIMEOUT_SECONDS}s; marking failed "
                    f"so the queue can move on. (Underlying executor thread, if any, is leaked until "
                    f"it exits on its own — Python threads can't be cancelled.)"
                )
                self.job_manager.update_job(
                    job_id,
                    status="failed",
                    error=f"job_timeout: exceeded {int(JOB_TIMEOUT_SECONDS)}s",
                )
            except Exception as e:
                logger.error(f"Job {job_id}: wrapper caught unexpected error: {e}", exc_info=True)
                self.job_manager.update_job(
                    job_id, status="failed", error=f"worker_error: {e}"
                )
            finally:
                # Remove from active jobs and scrub any leftover files for this job.
                # Runs even on timeout, so /data won't fill up with orphaned chunks/audio.
                self.active_jobs.discard(job_id)
                try:
                    self._cleanup_job_files(job_id)
                except Exception as cleanup_error:
                    logger.warning(f"Job {job_id}: post-timeout cleanup failed: {cleanup_error}")
    
    async def process_job(self, job_id: str):
        """Process a single transcription job"""
        job = self.job_manager.get_job(job_id)
        if not job or job["status"] != "queued":
            return

        logger.info(f"Processing job {job_id} (source_type={job.get('source_type', 'b2')})")
        self.job_manager.update_job(job_id, status="processing")

        temp_files = []

        try:
            has_audio = True
            if job.get("source_type") == "local_file":
                await self._archive_local_media(job_id, job)
                try:
                    transcripts = await self._process_local_file(job_id, job)
                except NoAudioTrackError:
                    has_audio = False
                    transcripts = []
                    logger.info(
                        "Job %s: No audio track; retaining archived video-only item",
                        job_id,
                    )
            else:
                # Build a B2 client using the credentials supplied with this job.
                b2_client = B2Client(job["b2_key_id"], job["b2_application_key"])

                # Pre-flight disk check: bail before downloading if the file won't fit.
                await self._assert_disk_space_for_job(job_id, job, b2_client)

                # MP4/MOV etc. have metadata (moov) at end of file → can't parse from pipe.
                ext = Path(job["b2_file_path"]).suffix.lower()
                STREAM_UNSAFE_EXTS = {".mp4", ".mov", ".m4a", ".avi", ".mkv", ".webm", ".3gp", ".3g2", ".mj2"}
                if ext in STREAM_UNSAFE_EXTS:
                    logger.info(f"Job {job_id}: Format {ext} is not pipe-streamable, using incremental path")
                    transcripts = await self._incremental_chunk_transcription(job_id, job, b2_client)
                else:
                    try:
                        logger.info(f"Job {job_id}: Attempting streaming path (B2 → ffmpeg → segments)")
                        transcripts = await self._stream_b2_to_transcription(job_id, job, b2_client)
                    except Exception as stream_error:
                        logger.warning(
                            f"Job {job_id}: Streaming failed ({stream_error}), falling back to incremental chunking"
                        )
                        transcripts = await self._incremental_chunk_transcription(job_id, job, b2_client)

            # Merge transcripts
            logger.info(f"Job {job_id}: Merging transcripts")
            full_transcript = self._merge_transcripts(transcripts) if has_audio else ""

            # --- Output: B2 upload (existing behaviour, only for B2-source jobs) ---
            transcript_b2_path = None
            if (
                has_audio
                and job.get("upload_transcript", False)
                and job.get("source_type") != "local_file"
            ):
                try:
                    logger.info(f"Job {job_id}: Uploading transcript to B2")
                    transcript_b2_path = await asyncio.wait_for(
                        self._upload_transcript(job, full_transcript, b2_client),
                        timeout=B2_UPLOAD_TIMEOUT,
                    )
                    logger.info(f"Job {job_id}: Transcript uploaded to B2: {transcript_b2_path}")
                except asyncio.TimeoutError:
                    logger.warning(
                        f"Job {job_id}: B2 transcript upload timed out after {B2_UPLOAD_TIMEOUT}s; skipping"
                    )
                except Exception as upload_error:
                    logger.warning(f"Job {job_id}: Failed to upload transcript to B2: {upload_error}")

            # --- Output: Google Drive upload ---
            drive_file = None
            if has_audio and job.get("google_drive_folder_id"):
                try:
                    logger.info(f"Job {job_id}: Uploading transcript to Google Drive")
                    drive_file = await self._upload_transcript_to_drive(job, full_transcript)
                    self.job_manager.update_job(
                        job_id,
                        drive_transcript_file_id=drive_file.get("id"),
                        drive_transcript_url=drive_file.get("webViewLink"),
                    )
                    logger.info(f"Job {job_id}: Transcript uploaded to Drive: {drive_file.get('webViewLink')}")
                except Exception as drive_error:
                    logger.warning(f"Job {job_id}: Failed to upload transcript to Drive: {drive_error}")

            # Update job state
            self.job_manager.update_job(
                job_id,
                status="completed",
                progress=100,
                transcript=full_transcript,
                has_audio=has_audio,
            )

            webhook_payload = {
                "job_id": job_id,
                "status": "completed",
                "transcript": full_transcript,
                "has_audio": has_audio,
            }
            if job.get("source_type") != "local_file":
                webhook_payload["b2_bucket"] = job["b2_bucket"]
                webhook_payload["b2_file_path"] = job["b2_file_path"]
            else:
                webhook_payload["original_filename"] = job.get("original_filename")
                if job.get("archived_video_path"):
                    webhook_payload["b2_bucket"] = job.get("archived_video_bucket")
                    webhook_payload["b2_file_path"] = job.get("archived_video_path")
                if job.get("thumbnail_b2_path"):
                    webhook_payload["thumbnail_b2_path"] = job.get("thumbnail_b2_path")
            if transcript_b2_path:
                webhook_payload["transcript_b2_path"] = transcript_b2_path
            if drive_file:
                webhook_payload["drive_transcript_file_id"] = drive_file.get("id")
                webhook_payload["drive_transcript_url"] = drive_file.get("webViewLink")

            if job.get("callback_url"):
                webhook_success = await self.webhook_client.send_callback(
                    job["callback_url"], webhook_payload
                )
                if webhook_success:
                    logger.info(f"Job {job_id}: Completed successfully")
                else:
                    logger.warning(f"Job {job_id}: Completed but webhook delivery failed")
            else:
                logger.info(f"Job {job_id}: Completed successfully (no callback URL)")

        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}", exc_info=True)
            error_msg = str(e)

            self.job_manager.update_job(job_id, status="failed", error=error_msg)

            if job.get("callback_url"):
                try:
                    failure_payload = {"job_id": job_id, "status": "failed", "error": error_msg}
                    if job.get("source_type") != "local_file":
                        failure_payload["b2_bucket"] = job["b2_bucket"]
                        failure_payload["b2_file_path"] = job["b2_file_path"]
                    else:
                        failure_payload["original_filename"] = job.get("original_filename")
                    webhook_success = await self.webhook_client.send_callback(
                        job["callback_url"], failure_payload
                    )
                    if not webhook_success:
                        logger.warning(f"Job {job_id}: Failed and webhook delivery also failed.")
                except Exception as webhook_error:
                    logger.error(f"Job {job_id}: Webhook delivery raised exception: {webhook_error}")

        finally:
            self._cleanup_files(temp_files)
            self._cleanup_job_files(job_id)

    async def _archive_local_media(self, job_id: str, job: dict) -> None:
        """Permanently archive a browser-uploaded source and optional thumbnail.

        Archival credentials are server-only Render environment variables. When
        they are configured, failure is fatal: a completed local-upload job must
        never imply that its source is safely retained when it is not.
        """
        configured = all(
            [B2_KEY_ID, B2_APPLICATION_KEY, B2_ARCHIVE_BUCKET]
        )
        if not configured:
            logger.warning(
                "Job %s: B2 archive is not configured; local source remains temporary",
                job_id,
            )
            return

        local_path = job.get("local_file_path")
        if not local_path or not os.path.exists(local_path):
            raise Exception(f"local_file_not_found: {local_path}")

        original = Path(job.get("original_filename") or local_path).name
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", original).strip("._")
        safe_name = safe_name or f"upload{Path(local_path).suffix.lower()}"
        video_path = f"{B2_VIDEO_PREFIX}/{job_id}/{safe_name}"
        client = B2Client(B2_KEY_ID, B2_APPLICATION_KEY)

        try:
            logger.info("Job %s: Archiving source to B2 at %s", job_id, video_path)
            await asyncio.wait_for(
                client.upload_file(B2_ARCHIVE_BUCKET, local_path, video_path),
                timeout=B2_STREAM_TIMEOUT,
            )
            self.job_manager.update_job(
                job_id,
                archived_video_bucket=B2_ARCHIVE_BUCKET,
                archived_video_path=video_path,
                b2_bucket=B2_ARCHIVE_BUCKET,
                b2_file_path=video_path,
            )
            job.update(
                {
                    "archived_video_bucket": B2_ARCHIVE_BUCKET,
                    "archived_video_path": video_path,
                    "b2_bucket": B2_ARCHIVE_BUCKET,
                    "b2_file_path": video_path,
                }
            )
        except Exception as exc:
            self.job_manager.update_job(job_id, archive_error=str(exc))
            raise Exception(f"b2_archive_failed: {exc}") from exc

        thumbnail_local = os.path.join(self.temp_dir, f"{job_id}_thumbnail.webp")
        try:
            generated = await self.media_processor.create_video_thumbnail(
                local_path,
                thumbnail_local,
                at_seconds=THUMBNAIL_AT_SECONDS,
            )
            if not generated:
                return
            thumbnail_path = f"{B2_THUMBNAIL_PREFIX}/{job_id}/thumbnail.webp"
            await asyncio.wait_for(
                client.upload_file(B2_ARCHIVE_BUCKET, generated, thumbnail_path),
                timeout=B2_UPLOAD_TIMEOUT,
            )
            self.job_manager.update_job(job_id, thumbnail_b2_path=thumbnail_path)
            job["thumbnail_b2_path"] = thumbnail_path
            logger.info("Job %s: Thumbnail archived at %s", job_id, thumbnail_path)
        except Exception as exc:
            # The original video is already safe. A missing thumbnail should not
            # throw away an otherwise successful transcription.
            logger.warning("Job %s: Thumbnail generation/upload failed: %s", job_id, exc)
        finally:
            self._cleanup_files([thumbnail_local])

    async def _process_local_file(self, job_id: str, job: dict) -> list[str]:
        """Process a file already on disk (uploaded via /transcribe-file).
        Skips B2 download entirely; runs audio extraction → chunking → transcription.
        """
        local_path = job.get("local_file_path")
        if not local_path or not os.path.exists(local_path):
            raise Exception(f"local_file_not_found: {local_path}")

        temp_files = []
        try:
            logger.info(f"Job {job_id}: Extracting audio from local file: {local_path}")
            audio_path = await self._extract_audio(local_path)
            if audio_path != local_path:
                temp_files.append(audio_path)

            logger.info(f"Job {job_id}: Getting chunk info")
            duration_seconds, num_chunks = await self.media_processor.get_chunk_info(
                audio_path, chunk_duration=CHUNK_DURATION_SECONDS
            )
            self.job_manager.update_job(job_id, chunks_total=num_chunks)

            logger.info(f"Job {job_id}: Transcribing {num_chunks} chunks")
            transcripts = await self._transcribe_chunks_incremental(
                job_id, audio_path, num_chunks, duration_seconds
            )

            return transcripts
        finally:
            self._cleanup_files(temp_files)

    async def _upload_transcript_to_drive(self, job: dict, transcript: str) -> dict:
        """Upload transcript text to Google Drive via the n8n proxy webhook.

        The n8n workflow handles the actual Drive upload using its stored OAuth
        credential, so no service account key is needed on this server.
        Returns a dict with keys: ok, drive_file_id, drive_file_url.
        """
        import httpx

        if not self.n8n_drive_url:
            raise Exception("N8N_DRIVE_WEBHOOK_URL is not set — cannot upload to Drive")

        original_filename = job.get("original_filename") or job.get("b2_file_path") or "transcript"
        base_name = Path(original_filename).stem
        drive_filename = f"{base_name}_transcript.txt"

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                self.n8n_drive_url,
                json={
                    "filename": drive_filename,
                    "content": transcript,
                    "folder_id": job["google_drive_folder_id"],
                },
            )
            resp.raise_for_status()
            data = resp.json()

        # Normalise to the shape the rest of the worker expects
        return {
            "id": data.get("drive_file_id"),
            "webViewLink": data.get("drive_file_url"),
        }
    
    async def _assert_disk_space_for_job(self, job_id: str, job: dict, b2_client: B2Client) -> None:
        """Raise if the temp disk doesn't have headroom to process this file.

        Fetches the file size from B2 metadata (cheap), then compares
        size * DISK_OVERHEAD_FACTOR + DISK_SAFETY_MARGIN against the free space on
        self.temp_dir's filesystem. Failing here marks the job 'failed' with a clear
        error instead of letting it hang on a full disk.
        """
        try:
            file_size = await b2_client.get_file_size(job["b2_bucket"], job["b2_file_path"])
        except Exception as e:
            # If metadata lookup fails, surface it cleanly instead of falling through to
            # the download (which would also fail, but with less context).
            raise Exception(f"file_size_lookup_failed: {e}")

        try:
            free_bytes = shutil.disk_usage(self.temp_dir).free
        except Exception as e:
            logger.warning(f"Job {job_id}: disk_usage check failed ({e}); skipping pre-flight check")
            return

        needed = int(file_size * DISK_OVERHEAD_FACTOR) + DISK_SAFETY_MARGIN_BYTES
        if needed > free_bytes:
            mb = lambda b: b // (1024 * 1024)
            raise Exception(
                f"insufficient_disk_space: file is {mb(file_size)}MB, "
                f"need ~{mb(needed)}MB free, only {mb(free_bytes)}MB available on {self.temp_dir}. "
                f"Increase TEMP_STORAGE_DIR capacity or wait for in-flight jobs to clear."
            )
        logger.info(
            f"Job {job_id}: disk pre-flight OK "
            f"(file={file_size // (1024 * 1024)}MB, need={needed // (1024 * 1024)}MB, "
            f"free={free_bytes // (1024 * 1024)}MB)"
        )

    async def _stream_b2_to_transcription(self, job_id: str, job: dict, b2_client: B2Client) -> list[str]:
        """Stream B2 file through ffmpeg to segment files; transcribe each segment and delete.
        Never writes the full media file to disk, so stays under 2GB temp limit.
        """
        from services.b2_client import B2DownloadError
        
        segment_dir = os.path.join(self.temp_dir, f"{job_id}_segments")
        os.makedirs(segment_dir, exist_ok=True)
        logger.info(f"Job {job_id}: Created segment dir: {segment_dir}")
        
        try:
            r, w = os.pipe()
            stream = None
            proc = None
            b2_task = None
            try:
                # Open write end for B2; B2 will close it when done
                stream = os.fdopen(w, "wb")
                w = -1  # so we don't close it again
                logger.info(f"Job {job_id}: Opened pipe for streaming")
                
                # Start B2 download writing to pipe (runs in executor, closes stream when done)
                logger.info(
                    f"Job {job_id}: Starting B2 stream download "
                    f"(bucket={job['b2_bucket']}, path={job['b2_file_path']})"
                )
                b2_task = asyncio.create_task(
                    asyncio.wait_for(
                        b2_client.download_to_stream(
                            job["b2_bucket"], job["b2_file_path"], stream
                        ),
                        timeout=B2_STREAM_TIMEOUT
                    )
                )
                
                # ffmpeg reads from pipe, writes chunk_000.mp3, chunk_001.mp3, ... to segment_dir
                logger.info(f"Job {job_id}: Starting ffmpeg stream-to-segments...")
                proc = await self.media_processor.run_ffmpeg_stream_to_segments(
                    r, segment_dir, CHUNK_DURATION_SECONDS
                )
                r = -1  # ffmpeg process holds it
                logger.info(f"Job {job_id}: ffmpeg started (PID: {proc.pid})")
                
                # Process segments as they complete: transcribe and delete
                results = {}
                processed = set()
                poll_interval = 2.0
                no_segments_timeout = 60.0  # If no segments after 60s, something is wrong
                last_segment_time = asyncio.get_event_loop().time()
                initial_wait = True
                
                logger.info(f"Job {job_id}: Starting segment processing loop...")
                while True:
                    # Check if B2 download failed
                    if b2_task.done():
                        try:
                            await b2_task
                            logger.info(f"Job {job_id}: B2 download completed successfully")
                        except asyncio.TimeoutError:
                            logger.error(f"Job {job_id}: B2 download timed out after {B2_STREAM_TIMEOUT}s")
                            if proc and proc.returncode is None:
                                try:
                                    proc.terminate()
                                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                                except Exception:
                                    proc.kill()
                            raise Exception("B2 download timed out")
                        except Exception as e:
                            logger.error(f"Job {job_id}: B2 download failed: {e}", exc_info=True)
                            # Kill ffmpeg if B2 failed
                            if proc and proc.returncode is None:
                                try:
                                    proc.terminate()
                                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                                except Exception:
                                    proc.kill()
                            raise Exception(f"B2 download failed: {e}")
                    
                    # List current segment files
                    indices = set()
                    try:
                        for name in os.listdir(segment_dir):
                            if name.startswith("chunk_") and name.endswith(".mp3"):
                                try:
                                    idx = int(name[6:9])  # chunk_XXX.mp3
                                    indices.add(idx)
                                except ValueError:
                                    pass
                    except Exception as e:
                        logger.warning(f"Job {job_id}: Error listing segment dir: {e}")
                    
                    if indices:
                        last_segment_time = asyncio.get_event_loop().time()
                        initial_wait = False
                        logger.debug(f"Job {job_id}: Found {len(indices)} segment files")
                    
                    # Check timeout: if no segments after initial wait, something is wrong
                    if initial_wait and (asyncio.get_event_loop().time() - last_segment_time) > no_segments_timeout:
                        logger.error(f"Job {job_id}: Timeout waiting for first segment after {no_segments_timeout}s")
                        if proc and proc.returncode is None:
                            try:
                                stderr = ""
                                if proc.stderr:
                                    try:
                                        stderr_data = await asyncio.wait_for(proc.stderr.read(), timeout=1.0)
                                        decoded = stderr_data.decode(errors="replace")
                                        stderr = decoded[-1000:]
                                    except Exception:
                                        pass
                                logger.error(f"Job {job_id}: ffmpeg stderr: {stderr}")
                                proc.terminate()
                                await asyncio.wait_for(proc.wait(), timeout=5.0)
                            except Exception:
                                proc.kill()
                        raise Exception(f"No segments produced after {no_segments_timeout}s. ffmpeg may have failed.")
                    
                    for i in sorted(indices):
                        if i in processed:
                            continue
                        # Chunk i is complete when chunk i+1 exists or ffmpeg has exited
                        if (i + 1) in indices or proc.returncode is not None:
                            path = os.path.join(segment_dir, f"chunk_{i:03d}.mp3")
                            if os.path.exists(path):
                                logger.info(f"Job {job_id}: Processing chunk {i}")
                                try:
                                    transcript = await self._transcribe_chunk_with_retry(
                                        job_id, path, i, max(indices) + 1 if indices else 1
                                    )
                                    results[i] = transcript
                                    self.job_manager.update_progress(
                                        job_id, len(results), max(indices) + 1 if indices else 1
                                    )
                                    logger.info(f"Job {job_id}: Chunk {i} transcribed successfully")
                                finally:
                                    self._cleanup_files([path])
                                processed.add(i)
                    
                    if proc.returncode is not None:
                        logger.info(f"Job {job_id}: ffmpeg exited with code {proc.returncode}")
                        if not indices or len(processed) >= len(indices):
                            break
                    await asyncio.sleep(poll_interval)
                
                # If ffmpeg failed, do NOT wait for B2 (B2 may block forever writing to a pipe no one reads)
                if proc.returncode != 0 and proc.returncode is not None:
                    stderr = ""
                    if proc.stderr:
                        try:
                            stderr_data = await asyncio.wait_for(proc.stderr.read(), timeout=2.0)
                            decoded = stderr_data.decode(errors="replace")
                            # Take last 2000 chars; large ID3/XMP blobs would bury the actual error
                            stderr = decoded[-2000:]
                        except Exception:
                            pass
                    logger.error(f"Job {job_id}: ffmpeg failed: exit {proc.returncode}, stderr: {stderr}")
                    # Cancel B2 task so we don't hang (B2 writing to pipe with no reader blocks)
                    b2_task.cancel()
                    try:
                        await b2_task
                    except asyncio.CancelledError:
                        pass
                    raise Exception(f"ffmpeg_stream_failed: exit {proc.returncode} {stderr[:500]}")
                
                # ffmpeg succeeded; wait for B2 to finish (it may still be flushing)
                logger.info(f"Job {job_id}: Waiting for B2 download to complete...")
                try:
                    await asyncio.wait_for(b2_task, timeout=30.0)
                except asyncio.TimeoutError:
                    b2_task.cancel()
                    try:
                        await b2_task
                    except asyncio.CancelledError:
                        pass
                    logger.warning(f"Job {job_id}: B2 task did not finish in time (ignored, ffmpeg already done)")
                await proc.wait()
                
                # Return transcripts in order
                num_chunks = max(results.keys()) + 1 if results else 0
                self.job_manager.update_job(job_id, chunks_total=num_chunks)
                return [results.get(i, "") for i in range(num_chunks)]
                
            finally:
                # Cleanup: close pipe ends if not already closed
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
                # Close stream if still open
                if stream:
                    try:
                        stream.close()
                    except Exception:
                        pass
                # Kill ffmpeg if still running
                if proc and proc.returncode is None:
                    try:
                        logger.warning(f"Job {job_id}: Killing ffmpeg process")
                        proc.terminate()
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                    except Exception:
                        proc.kill()
        except B2DownloadError as e:
            logger.error(f"Job {job_id}: B2DownloadError: {e}")
            raise Exception(str(e))
        except Exception as e:
            logger.error(f"Job {job_id}: Stream failed: {e}", exc_info=True)
            raise Exception(f"stream_failed: {e}")
        finally:
            # Remove segment dir (any remaining files)
            try:
                for name in os.listdir(segment_dir):
                    self._cleanup_files([os.path.join(segment_dir, name)])
                os.rmdir(segment_dir)
            except Exception as ex:
                logger.warning(f"Cleanup segment dir: {ex}")
    
    async def _incremental_chunk_transcription(self, job_id: str, job: dict, b2_client: B2Client) -> list[str]:
        """Fallback: Download file, extract audio, then create chunks incrementally.
        Still better than creating all chunks at once, but writes full file to disk first.
        """
        logger.info(f"Job {job_id}: Using incremental chunking fallback")
        temp_files = []

        try:
            # Download media
            logger.info(f"Job {job_id}: Downloading media from B2")
            # Outer timeout: same rationale as transcript upload — b2sdk's synchronous urllib3
            # call has no aggressive read timeout, so a silently-dropped TCP socket can hang
            # for hours. Bail early so the master per-job timeout doesn't have to.
            media_path = await asyncio.wait_for(
                self._download_media(job, b2_client),
                timeout=B2_STREAM_TIMEOUT,
            )
            temp_files.append(media_path)
            
            # Extract audio
            logger.info(f"Job {job_id}: Extracting audio")
            audio_path = await self._extract_audio(media_path)
            if audio_path != media_path:
                temp_files.append(audio_path)
                self._cleanup_files([media_path])
                temp_files.remove(media_path)
            
            # Get chunk info
            logger.info(f"Job {job_id}: Getting chunk info")
            duration_seconds, num_chunks = await self.media_processor.get_chunk_info(
                audio_path, chunk_duration=CHUNK_DURATION_SECONDS
            )
            self.job_manager.update_job(job_id, chunks_total=num_chunks)
            
            # Transcribe chunks incrementally
            logger.info(f"Job {job_id}: Transcribing {num_chunks} chunks incrementally")
            transcripts = await self._transcribe_chunks_incremental(
                job_id, audio_path, num_chunks, duration_seconds
            )
            
            # Cleanup audio file
            if audio_path in temp_files:
                temp_files.remove(audio_path)
            self._cleanup_files([audio_path])
            
            return transcripts
        finally:
            self._cleanup_files(temp_files)
    
    async def _download_media(self, job: dict, b2_client: B2Client) -> str:
        """Download media file from B2"""
        from services.b2_client import B2DownloadError

        try:
            # Extract file extension from B2 file path
            file_extension = Path(job["b2_file_path"]).suffix
            if not file_extension:
                file_extension = ".bin"  # Fallback for files without extension

            # Preserve extension in local filename
            local_path = os.path.join(self.temp_dir, f"{job['job_id']}_media{file_extension}")

            await b2_client.download_file(
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
        except NoAudioTrackError:
            raise
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
        # Put per-job chunks in their own subdirectory so _cleanup_job_files (which
        # matches anything starting with job_id) catches any leftovers if the job
        # is killed mid-flight. Otherwise generic chunk_N.mp3 files would survive.
        chunks_dir = os.path.join(self.temp_dir, f"{job_id}_chunks")
        try:
            os.makedirs(chunks_dir, exist_ok=True)
            # Create chunk on-demand
            chunk_path = await self.media_processor.create_single_chunk(
                audio_path,
                chunk_index,
                CHUNK_DURATION_SECONDS,
                chunks_dir
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
        time_offset = chunk_index * CHUNK_DURATION_SECONDS

        while retry_count < max_retries:
            try:
                transcript = await self.openai_client.transcribe(chunk_path, time_offset=time_offset)
                
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
        return "\n".join(t for t in transcripts if t)
    
    async def _upload_transcript(self, job: dict, transcript: str, b2_client: B2Client) -> str:
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
            await b2_client.upload_file(
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

    def _cleanup_job_files(self, job_id: str):
        """Remove any leftover files or directories in temp_dir tagged with this job_id.
        Catches anything missed by per-step cleanup so the persistent disk doesn't fill up.
        """
        try:
            if not os.path.isdir(self.temp_dir):
                return
            for entry in os.listdir(self.temp_dir):
                if not entry.startswith(job_id):
                    continue
                full = os.path.join(self.temp_dir, entry)
                try:
                    if os.path.isdir(full) and not os.path.islink(full):
                        shutil.rmtree(full, ignore_errors=True)
                    else:
                        os.remove(full)
                    logger.debug(f"Cleaned up leftover for job {job_id}: {full}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup {full} for job {job_id}: {e}")
        except Exception as e:
            logger.warning(f"Job cleanup scan failed for {job_id}: {e}")
