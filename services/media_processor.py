"""Media processing utilities.

ffmpeg/ffprobe are used for: (1) extracting audio from video, (2) getting audio duration
without loading the file, (3) chunking audio without loading the full file into memory.
The pydub fallback loads entire files into RAM and is not suitable for large files or
low-memory environments (e.g. 512MB); ensure ffmpeg is installed on the system.
"""
import asyncio
import logging
import os
import subprocess
from pathlib import Path
from pydub import AudioSegment

logger = logging.getLogger(__name__)


class MediaProcessor:
    """Handles audio extraction and chunking"""
    
    SUPPORTED_AUDIO_FORMATS = [".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"]
    SUPPORTED_VIDEO_FORMATS = [".mp4", ".mov", ".avi", ".mkv", ".webm"]
    MAX_CHUNK_SIZE_MB = 20
    CHUNK_DURATION_MS = 600000  # 10 minutes in milliseconds

    async def create_video_thumbnail(
        self,
        video_path: str,
        output_path: str,
        *,
        at_seconds: float = 3.0,
        width: int = 640,
        height: int = 360,
    ) -> str | None:
        """Extract a consistently-sized WebP thumbnail from a local video.

        Audio files return None. FFmpeg failures are surfaced so callers can log
        them while preserving the already-archived source video.
        """
        if Path(video_path).suffix.lower() not in self.SUPPORTED_VIDEO_FORMATS:
            return None

        def _extract() -> str:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            filter_value = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
            )
            last_error = ""
            # Very short videos may have no frame at the configured timestamp.
            for timestamp in dict.fromkeys([max(0.0, at_seconds), 0.0]):
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    str(timestamp),
                    "-i",
                    video_path,
                    "-frames:v",
                    "1",
                    "-vf",
                    filter_value,
                    "-c:v",
                    "libwebp",
                    "-quality",
                    "82",
                    output_path,
                ]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0 and os.path.exists(output_path):
                    return output_path
                last_error = result.stderr[-500:]
            raise RuntimeError(f"thumbnail_extraction_failed: {last_error}")

        return await asyncio.get_event_loop().run_in_executor(None, _extract)
    
    async def extract_audio(self, media_path: str) -> str:
        """Extract audio from media file"""
        file_ext = Path(media_path).suffix.lower()
        
        # If already audio, return as-is
        if file_ext in self.SUPPORTED_AUDIO_FORMATS:
            return media_path
        
        # If video, extract audio
        if file_ext in self.SUPPORTED_VIDEO_FORMATS:
            return await self._extract_audio_from_video(media_path)
        
        raise Exception(f"unsupported_format: {file_ext}")
    
    async def _extract_audio_from_video(self, video_path: str) -> str:
        """Extract audio track from video file using ffmpeg directly for better performance"""
        def _extract():
            audio_path = f"{video_path}_audio.mp3"
            
            # Try ffmpeg first for better performance with large files
            try:
                cmd = [
                    'ffmpeg',
                    '-i', video_path,
                    '-vn',  # No video
                    '-acodec', 'libmp3lame',  # MP3 codec
                    '-q:a', '2',  # High quality
                    '-y',  # Overwrite output
                    audio_path
                ]
                
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=True
                )
                logger.info(f"Audio extracted successfully to {audio_path} using ffmpeg")
                return audio_path
                
            except FileNotFoundError:
                # ffmpeg not found, fallback to pydub
                logger.warning("ffmpeg not found, falling back to pydub (slower for large files)")
                audio = AudioSegment.from_file(video_path)
                audio.export(audio_path, format="mp3")
                logger.info(f"Audio extracted successfully to {audio_path} using pydub")
                return audio_path
                
            except subprocess.CalledProcessError as e:
                logger.error(f"ffmpeg error: {e.stderr}")
                # Try pydub as fallback
                logger.warning("ffmpeg failed, trying pydub fallback")
                try:
                    audio = AudioSegment.from_file(video_path)
                    audio.export(audio_path, format="mp3")
                    logger.info(f"Audio extracted successfully to {audio_path} using pydub")
                    return audio_path
                except Exception as pydub_error:
                    logger.error(f"pydub also failed: {pydub_error}")
                    raise Exception(f"Audio extraction failed with both ffmpeg and pydub. Please install ffmpeg.")
        
        return await asyncio.get_event_loop().run_in_executor(None, _extract)
    
    async def chunk_audio(self, audio_path: str, chunk_duration: int = 600) -> list[str]:
        """Split audio into fixed-duration chunks efficiently for large files"""
        def _chunk():
            # First, get audio duration
            try:
                duration_seconds = self._get_audio_duration(audio_path)
            except Exception as e:
                logger.error(f"Failed to get audio duration: {e}")
                raise Exception(f"Cannot chunk audio: {e}")
            
            # If audio is short enough, return as single chunk
            if duration_seconds <= chunk_duration:
                logger.info(f"Audio duration {duration_seconds}s <= {chunk_duration}s, no chunking needed")
                return [audio_path]
            
            # Try ffmpeg first for efficient chunking
            try:
                return self._chunk_with_ffmpeg(audio_path, duration_seconds, chunk_duration)
            except (FileNotFoundError, subprocess.CalledProcessError) as e:
                logger.warning(f"ffmpeg chunking failed ({e}), falling back to pydub (slower, uses more memory)")
                return self._chunk_with_pydub(audio_path, chunk_duration)
        
        return await asyncio.get_event_loop().run_in_executor(None, _chunk)
    
    def _chunk_with_ffmpeg(self, audio_path: str, duration_seconds: float, chunk_duration: int) -> list[str]:
        """Chunk audio using ffmpeg (fast, memory-efficient)"""
        chunks = []
        chunk_duration_seconds = chunk_duration
        num_chunks = int(duration_seconds / chunk_duration_seconds) + 1
        
        logger.info(f"Splitting {duration_seconds}s audio into ~{num_chunks} chunks using ffmpeg")
        
        for i in range(num_chunks):
            start_time = i * chunk_duration_seconds
            chunk_path = f"{audio_path}_chunk_{i}.mp3"
            
            # Use ffmpeg to extract chunk without loading full file
            cmd = [
                'ffmpeg',
                '-i', audio_path,
                '-ss', str(start_time),  # Start time
                '-t', str(chunk_duration_seconds),  # Duration
                '-acodec', 'copy',  # Copy codec (fast, no re-encoding)
                '-y',  # Overwrite
                chunk_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            
            # Verify chunk was created and check size
            if os.path.exists(chunk_path):
                chunk_size_mb = os.path.getsize(chunk_path) / (1024 * 1024)
                logger.info(f"Created chunk {i}: {chunk_size_mb:.2f}MB")
                
                if chunk_size_mb > self.MAX_CHUNK_SIZE_MB:
                    logger.warning(f"Chunk {i} exceeds {self.MAX_CHUNK_SIZE_MB}MB: {chunk_size_mb:.2f}MB")
                
                chunks.append(chunk_path)
            else:
                logger.warning(f"Chunk {i} was not created, may be past end of audio")
                break
        
        logger.info(f"Split audio into {len(chunks)} chunks using ffmpeg")
        return chunks
    
    def _chunk_with_pydub(self, audio_path: str, chunk_duration: int) -> list[str]:
        """Chunk audio using pydub (slower, loads into memory)"""
        logger.warning("Using pydub for chunking - this may be slow and memory-intensive for large files")
        
        audio = AudioSegment.from_file(audio_path)
        duration_seconds = len(audio) / 1000
        
        # Split into chunks
        chunk_duration_ms = chunk_duration * 1000
        chunks = []
        
        for i, start_ms in enumerate(range(0, len(audio), chunk_duration_ms)):
            chunk = audio[start_ms:start_ms + chunk_duration_ms]
            chunk_path = f"{audio_path}_chunk_{i}.mp3"
            chunk.export(chunk_path, format="mp3")
            
            # Verify chunk size
            chunk_size_mb = os.path.getsize(chunk_path) / (1024 * 1024)
            logger.info(f"Created chunk {i}: {chunk_size_mb:.2f}MB")
            
            if chunk_size_mb > self.MAX_CHUNK_SIZE_MB:
                logger.warning(f"Chunk {i} exceeds {self.MAX_CHUNK_SIZE_MB}MB: {chunk_size_mb:.2f}MB")
            
            chunks.append(chunk_path)
        
        logger.info(f"Split audio into {len(chunks)} chunks using pydub")
        return chunks
    
    def _get_audio_duration(self, audio_path: str) -> float:
        """Get audio duration in seconds without loading entire file"""
        try:
            # Use ffprobe to get duration efficiently
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                audio_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            duration = float(result.stdout.strip())
            logger.info(f"Audio duration: {duration}s (via ffprobe)")
            return duration
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as e:
            # Fallback to pydub if ffprobe not available
            logger.warning(f"ffprobe not available ({e}), using pydub to get duration (slower)")
            try:
                audio = AudioSegment.from_file(audio_path)
                duration = len(audio) / 1000
                logger.info(f"Audio duration: {duration}s (via pydub)")
                return duration
            except Exception as pydub_error:
                logger.error(f"Failed to get duration with pydub: {pydub_error}")
                raise Exception(f"Could not determine audio duration. Please install ffmpeg/ffprobe.")
    
    async def get_chunk_info(self, audio_path: str, chunk_duration: int = 600) -> tuple[float, int]:
        """Get audio duration and number of chunks without creating any files.
        Returns: (duration_seconds, num_chunks)
        """
        def _get_info():
            try:
                duration_seconds = self._get_audio_duration(audio_path)
                if duration_seconds <= chunk_duration:
                    return (duration_seconds, 1)
                num_chunks = int(duration_seconds / chunk_duration) + 1
                return (duration_seconds, num_chunks)
            except Exception as e:
                logger.error(f"Failed to get chunk info: {e}")
                raise Exception(f"Cannot get chunk info: {e}")
        
        return await asyncio.get_event_loop().run_in_executor(None, _get_info)
    
    async def create_single_chunk(
        self, audio_path: str, chunk_index: int, chunk_duration: int, output_dir: str
    ) -> str | None:
        """Create a single chunk file on-demand. Returns path to chunk file, or None if past end of audio.
        This allows incremental processing: create chunk, transcribe, delete, repeat.
        """
        def _create_chunk():
            try:
                # Calculate start time for this chunk
                start_time = chunk_index * chunk_duration
                chunk_path = os.path.join(output_dir, f"chunk_{chunk_index}.mp3")
                
                # Use ffmpeg to extract single chunk without loading full file
                cmd = [
                    'ffmpeg',
                    '-i', audio_path,
                    '-ss', str(start_time),  # Start time
                    '-t', str(chunk_duration),  # Duration
                    '-acodec', 'copy',  # Copy codec (fast, no re-encoding)
                    '-y',  # Overwrite
                    chunk_path
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                
                # Verify chunk was created
                if os.path.exists(chunk_path):
                    chunk_size_mb = os.path.getsize(chunk_path) / (1024 * 1024)
                    logger.info(f"Created chunk {chunk_index}: {chunk_size_mb:.2f}MB")
                    
                    if chunk_size_mb > self.MAX_CHUNK_SIZE_MB:
                        logger.warning(f"Chunk {chunk_index} exceeds {self.MAX_CHUNK_SIZE_MB}MB: {chunk_size_mb:.2f}MB")
                    
                    return chunk_path
                else:
                    logger.warning(f"Chunk {chunk_index} was not created, may be past end of audio")
                    return None
                    
            except subprocess.CalledProcessError as e:
                # If ffmpeg fails, chunk may be past end of audio
                if "Invalid data found" in e.stderr or "End of file" in e.stderr:
                    logger.debug(f"Chunk {chunk_index} past end of audio")
                    return None
                logger.error(f"ffmpeg error creating chunk {chunk_index}: {e.stderr}")
                raise Exception(f"Failed to create chunk {chunk_index}: {e.stderr}")
            except Exception as e:
                logger.error(f"Unexpected error creating chunk {chunk_index}: {e}")
                raise Exception(f"Failed to create chunk {chunk_index}: {e}")
        
        return await asyncio.get_event_loop().run_in_executor(None, _create_chunk)
    
    @staticmethod
    async def run_ffmpeg_stream_to_segments(
        pipe_read_fd: int, output_dir: str, segment_time_seconds: int
    ) -> "asyncio.subprocess.Process":
        """Run ffmpeg reading from pipe (stdin), extract audio and write segment files.
        Does not load the full input into disk; peak disk = ~2 segment files.
        Caller must close pipe_read_fd after the process is created (ffmpeg holds it).
        Returns the Process; segment files appear in output_dir as chunk_000.mp3, chunk_001.mp3, ...
        """
        os.makedirs(output_dir, exist_ok=True)
        segment_pattern = os.path.join(output_dir, "chunk_%03d.mp3")
        cmd = [
            "ffmpeg", "-y",
            "-i", "pipe:0",
            "-vn",
            "-acodec", "libmp3lame", "-q:a", "2",
            "-f", "segment",
            "-segment_time", str(segment_time_seconds),
            "-segment_format", "mp3",
            "-reset_timestamps", "1",
            segment_pattern,
        ]

        # Pass the raw read-end fd as stdin. Python's subprocess will dup2 it to fd 0
        # in the child, which is exactly what ffmpeg's pipe:0 reads from.
        # We must make the fd inheritable first (Python 3.4+ sets O_CLOEXEC by default).
        os.set_inheritable(pipe_read_fd, True)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=pipe_read_fd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=output_dir,
        )
        return proc
