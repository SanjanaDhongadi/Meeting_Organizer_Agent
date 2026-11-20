"""Speech-to-text for browser-recorded meeting audio (Groq whisper-large-v3). The Groq client itself lives in groq_client.py.

The recording is one mixed track (tab audio + the organizer's microphone), so who said what cannot be told apart.
Every line therefore carries the neutral label "Speaker" and a timestamp; names are never guessed.
Nothing is fabricated: without GROQ_API_KEY the call fails with a clear, non-retryable error.
"""
import logging
import os
from typing import Any, Dict, List

from backend.app.config import settings
from backend.connectors import groq_client

logger = logging.getLogger("transcription")

NEUTRAL_SPEAKER = "Speaker"


def format_timestamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def format_lines(parts: List[Dict[str, Any]], offset_seconds: float = 0.0) -> str:
    """[{start, text}] -> '[hh:mm:ss] Speaker: text' lines (the same 'Name: text' shape as Meet transcripts)."""
    lines = []
    for part in parts:
        text = " ".join(str(part.get("text") or "").split())
        if text:
            lines.append(f"[{format_timestamp(offset_seconds + float(part.get('start') or 0))}] {NEUTRAL_SPEAKER}: {text}")
    return "\n".join(lines)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


# Formats Groq's speech-to-text accepts that the recorder/upload API can produce.
SUPPORTED_EXTENSIONS = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav"}


class TranscriptionService:
    def transcribe_file(self, path: str, mime_type: str = "audio/webm", offset_seconds: float = 0.0) -> Dict[str, Any]:
        """Returns {success, text, error, retryable, code}. `text` holds timestamped, neutrally labelled lines."""
        if not groq_client.is_configured():
            return {"success": False, "retryable": False, "code": "TRANSCRIPTION_NOT_CONFIGURED",
                    "error": groq_client.not_configured_message("transcription of recorded audio") + " Then press Retry."}
        base = (mime_type or "").split(";")[0].strip().lower()
        if base not in SUPPORTED_EXTENSIONS:
            return {"success": False, "retryable": False, "code": "TRANSCRIPTION_UNSUPPORTED_FORMAT",
                    "error": f"The audio format {base or 'unknown'} is not supported for transcription."}
        try:
            if os.path.getsize(path) > settings.RECORDING_MAX_SEGMENT_MB * 1024 * 1024:
                return {"success": False, "retryable": False, "code": "TRANSCRIPTION_TOO_LARGE",
                        "error": f"The audio segment is larger than {settings.RECORDING_MAX_SEGMENT_MB} MB, Groq's upload limit."}
            return self._call(path, base, offset_seconds)
        except Exception as error:      # classify below; the key is never part of the message
            return self._failure(error)

    def _client(self):
        return groq_client.get_client(timeout=300.0)

    def _call(self, path: str, mime_type: str, offset_seconds: float) -> Dict[str, Any]:
        with open(path, "rb") as handle:
            data = handle.read()
        response = self._client().audio.transcriptions.create(
            model=settings.GROQ_TRANSCRIPTION_MODEL, file=(f"segment.{SUPPORTED_EXTENSIONS[mime_type]}", data),
            response_format="verbose_json", temperature=0.0)
        parts = [{"start": _get(s, "start", 0), "text": _get(s, "text", "")} for s in (_get(response, "segments") or [])]
        if not parts and (_get(response, "text") or "").strip():
            parts = [{"start": 0, "text": _get(response, "text")}]
        return {"success": True, "text": format_lines(parts, offset_seconds), "retryable": False}

    @staticmethod
    def _failure(error: Exception) -> Dict[str, Any]:
        problem = groq_client.classify_error(error)
        message = {"BAD_REQUEST": f"Groq rejected the audio: {problem}"}.get(problem.code, str(problem))
        logger.warning("Transcription failed (%s): %s", problem.code, message)
        return {"success": False, "retryable": problem.retryable, "code": f"TRANSCRIPTION_{problem.code}", "error": message[:500]}


transcription_service = TranscriptionService()
