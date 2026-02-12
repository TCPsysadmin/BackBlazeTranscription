"""Media processing utilities"""
import asyncio
import logging
import os
from pathlib import Path
from pydub import AudioSegment

logger = logging.getLogger(__name__)


class MediaProcessor:
    """Handles audio extraction and chunking"""
    
    SUPPORTED_AUDIO_FORMATS = [".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"]
    SUPPORTED_VIDEO_FORMATS = [".mp4", ".mov", ".avi", ".mkv", ".webm"]
    MAX_CHUNK_SIZE_MB = 20
    
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
        """Extract audio track from video file"""
        def _extract():
            audio = AudioSegment.from_file(video_path)
            audio_path = f"{video_path}_audio.mp3"
            audio.export(audio_path, format="mp3")
            return audio_path
        
        return await asyncio.get_event_loop().run_in_executor(None, _extract)
    
    async def chunk_audio(self, audio_path: str, chunk_duration: int = 600) -> list[str]:
        """Split audio into fixed-duration chunks"""
        def _chunk():
            audio = AudioSegment.from_file(audio_path)
            duration_seconds = len(audio) / 1000
            
            # If audio is short enough, return as single chunk
            if duration_seconds <= chunk_duration:
                return [audio_path]
            
            # Split into chunks
            chunk_duration_ms = chunk_duration * 1000
            chunks = []
            
            for i, start_ms in enumerate(range(0, len(audio), chunk_duration_ms)):
                chunk = audio[start_ms:start_ms + chunk_duration_ms]
                chunk_path = f"{audio_path}_chunk_{i}.mp3"
                chunk.export(chunk_path, format="mp3")
                
                # Verify chunk size
                chunk_size_mb = os.path.getsize(chunk_path) / (1024 * 1024)
                if chunk_size_mb > self.MAX_CHUNK_SIZE_MB:
                    logger.warning(f"Chunk {i} exceeds {self.MAX_CHUNK_SIZE_MB}MB: {chunk_size_mb:.2f}MB")
                
                chunks.append(chunk_path)
            
            logger.info(f"Split audio into {len(chunks)} chunks")
            return chunks
        
        return await asyncio.get_event_loop().run_in_executor(None, _chunk)
