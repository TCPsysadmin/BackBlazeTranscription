"""OpenAI transcription client"""
import logging
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


class OpenAITranscriber:
    """Client for OpenAI transcription API"""
    
    def __init__(self, api_key: str):
        # Increase timeout for large audio files (default is 10 minutes)
        self.client = AsyncOpenAI(
            api_key=api_key,
            timeout=600.0  # 10 minutes timeout
        )
    
    async def transcribe(self, audio_file_path: str) -> str:
        """Transcribe an audio file using OpenAI Whisper"""
        try:
            with open(audio_file_path, "rb") as audio_file:
                transcript = await self.client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    response_format="text"
                )
            
            logger.info(f"Transcribed {audio_file_path}")
            return transcript
            
        except Exception as e:
            logger.error(f"Transcription failed for {audio_file_path}: {e}")
            raise
