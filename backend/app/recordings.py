"""Disk storage for browser-recorded meeting audio. Raw audio never goes into PostgreSQL; the database keeps
only metadata (RecordingSegment) and the transcript."""
import hashlib
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List

from backend.app.config import settings
from backend.app.db import RecordingSegment

logger = logging.getLogger("recordings")

ALLOWED_MIME = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav"}


def max_segment_bytes() -> int:
    return settings.RECORDING_MAX_SEGMENT_MB * 1024 * 1024


def root() -> Path:
    path = Path(settings.RECORDINGS_DIR)
    return path if path.is_absolute() else Path(__file__).resolve().parents[2] / path


def segment_path(meeting_id: str, index: int, mime_type: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", meeting_id)[:64] or "meeting"
    extension = ALLOWED_MIME.get((mime_type or "").split(";")[0].strip().lower(), "webm")
    return root() / safe / f"segment_{index:04d}.{extension}"


def write_segment(meeting_id: str, index: int, mime_type: str, data: bytes) -> Path:
    path = segment_path(meeting_id, index, mime_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".part")
    temp.write_bytes(data)
    os.replace(temp, path)                  # a half-written file is never visible under the final name
    return path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def delete_audio(db, segments: List[RecordingSegment]) -> int:
    """Delete the raw audio files of the given segments and mark them deleted. Transcripts are kept."""
    removed = 0
    for segment in segments:
        if segment.storage_path:
            try:
                Path(segment.storage_path).unlink(missing_ok=True)
                removed += 1
            except OSError as error:
                logger.warning("Could not delete %s: %s", segment.storage_path, error)
                continue
        segment.storage_path, segment.audio_deleted_at = "", datetime.utcnow()
    db.commit()
    if segments:
        folder = segment_path(segments[0].meeting_id, 0, "audio/webm").parent
        try:
            folder.rmdir()
        except OSError:
            pass
    return removed
