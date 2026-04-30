"""OpenAI transcription client"""
import logging
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


def _fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class OpenAITranscriber:
    """Client for OpenAI transcription API"""

    def __init__(self, api_key: str):
        # Increase timeout for large audio files (default is 15 minutes)
        self.client = AsyncOpenAI(
            api_key=api_key,
            timeout=900.0  # 15 minutes timeout
        )

    async def transcribe(self, audio_file_path: str, time_offset: float = 0.0) -> str:
        """Transcribe an audio file using OpenAI Whisper, returning timestamped segments.
        time_offset shifts all segment timestamps to account for this chunk's position in the full file.
        """
        try:
            with open(audio_file_path, "rb") as audio_file:
                response = await self.client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    response_format="verbose_json"
                )

            segments = getattr(response, "segments", None) or []
            if segments:
                lines = []
                for seg in segments:
                    if isinstance(seg, dict):
                        start = seg.get("start", 0.0)
                        text = (seg.get("text") or "").strip()
                    else:
                        start = getattr(seg, "start", 0.0)
                        text = (getattr(seg, "text", "") or "").strip()
                    if text:
                        lines.append(f"[{_fmt_ts(start + time_offset)}] {text}")
                result = "\n".join(lines)
            else:
                # Fallback: no segments returned, prefix with chunk start time
                text = (getattr(response, "text", None) or "").strip()
                result = f"[{_fmt_ts(time_offset)}] {text}" if text else ""

            logger.info(f"Transcribed {audio_file_path}")
            return result

        except Exception as e:
            logger.error(f"Transcription failed for {audio_file_path}: {e}")
            raise
