"""
Google Meet transcript retrieval (Meet REST API v2).

Flow: meeting code -> conferenceRecords (filtered by space.meeting_code) -> transcripts -> transcript entries.
The result tells the caller whether the transcript is READY, or why it is not ready yet, so the worker can retry.
Needs the `meetings.space.readonly` OAuth scope on the organizer's Google account, and transcription must have
been turned on during the meeting (Google Workspace feature).
"""
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger("meet_service")

MEET_CODE_RE = re.compile(r"meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})", re.IGNORECASE)


def meeting_code_from_url(meet_url: Optional[str]) -> Optional[str]:
    match = MEET_CODE_RE.search(meet_url or "")
    return match.group(1).lower() if match else None


class MeetService(ABC):
    @abstractmethod
    def get_transcript(self, meeting_code: str, organizer_email: Optional[str] = None,
                       not_before: Optional[str] = None) -> Dict[str, Any]:
        """
        Returns {"success": False, error, error_code, ...} when Google could not be asked, otherwise
        {"success": True, "ready": bool, "reason": str, ...}; when ready also "transcript_text" and "meta".
        """


class MockMeetService(MeetService):
    """DEMO_MODE: there are no real Meet conferences, so there are never transcripts. Nothing is fabricated."""

    def get_transcript(self, meeting_code, organizer_email=None, not_before=None):
        return {"success": True, "ready": False, "reason": "DEMO_MODE",
                "message": "DEMO_MODE is on: Google Meet transcripts are not available."}


class GoogleMeetService(MeetService):
    PAGE = 100

    def __init__(self, credentials: Optional[Any] = None):
        self.credentials = credentials

    def _credentials_for(self, email: Optional[str]):
        from connectors.google_auth import load_google_credentials_with_error
        if self.credentials:
            return self.credentials, None
        return load_google_credentials_with_error(email)

    @staticmethod
    def _build(credentials):
        from googleapiclient.discovery import build
        return build("meet", "v2", credentials=credentials, cache_discovery=False)

    @staticmethod
    def _failure(prefix: str, error: Exception) -> Dict[str, Any]:
        from connectors.google_auth import google_error_detail
        details = google_error_detail(error)
        logger.error("[MeetService] %s — HTTP %s | %s", prefix, details["http_status"], details["error_detail"])
        code = "NETWORK_UNREACHABLE" if details.get("network_error") else "GOOGLE_API_FAILURE"
        message = f"{prefix}: {details['error_detail']}"
        if details["http_status"] in (401, 403):
            code = "MEET_ACCESS_DENIED"
            message += (" The organizer must reconnect Google (the Meet transcript permission "
                        "'meetings.space.readonly' is required) and the Meet API must be enabled in Google Cloud.")
        return {"success": False, "error": message, "error_code": code, "http_status": details["http_status"]}

    @staticmethod
    def _speaker(participant: Dict[str, Any]) -> str:
        for key in ("signedinUser", "anonymousUser", "phoneUser"):
            if participant.get(key, {}).get("displayName"):
                return participant[key]["displayName"]
        return "Unknown speaker"

    def _paged(self, request_factory, key: str) -> List[Dict[str, Any]]:
        items, token = [], None
        while True:
            response = request_factory(token).execute()
            items.extend(response.get(key, []))
            token = response.get("nextPageToken")
            if not token:
                return items

    def get_transcript(self, meeting_code, organizer_email=None, not_before=None):
        credentials, cred_error = self._credentials_for(organizer_email)
        if not credentials:
            return {"success": False, "error": cred_error["error"], "error_code": cred_error["error_code"]}
        if not re.fullmatch(r"[a-z]{3}-[a-z]{4}-[a-z]{3}", meeting_code or ""):
            return {"success": False, "error": f"'{meeting_code}' is not a valid Google Meet code.", "error_code": "INVALID_MEETING_CODE"}
        try:
            service = self._build(credentials)
            records = self._paged(lambda t: service.conferenceRecords().list(
                filter=f'space.meeting_code = "{meeting_code}"', pageSize=self.PAGE, pageToken=t), "conferenceRecords")
        except Exception as error:
            return self._failure("Google Meet conference lookup failed", error)

        # A recurring event reuses its code: only conferences that started around this meeting count.
        records = [r for r in records if not not_before or (r.get("startTime") or "") >= not_before]
        if not records:
            return {"success": True, "ready": False, "reason": "NO_CONFERENCE_RECORD",
                    "message": "Google has no Meet conference for this meeting yet (nobody joined, or it is not processed yet)."}
        if any(not r.get("endTime") for r in records):
            return {"success": True, "ready": False, "reason": "CONFERENCE_IN_PROGRESS",
                    "message": "The meeting is still in progress."}
        records.sort(key=lambda r: r.get("startTime") or "")

        lines: List[str] = []
        transcripts_seen: List[str] = []
        speakers: List[str] = []
        try:
            for record in records:
                transcripts = self._paged(lambda t, r=record: service.conferenceRecords().transcripts().list(
                    parent=r["name"], pageSize=self.PAGE, pageToken=t), "transcripts")
                if any(t.get("state") != "FILE_GENERATED" for t in transcripts):
                    return {"success": True, "ready": False, "reason": "TRANSCRIPT_PROCESSING",
                            "message": "Google is still generating the transcript."}
                if not transcripts:
                    continue
                names = {}
                for p in self._paged(lambda t, r=record: service.conferenceRecords().participants().list(
                        parent=r["name"], pageSize=250, pageToken=t), "participants"):
                    names[p["name"]] = self._speaker(p)
                for transcript in transcripts:
                    transcripts_seen.append(transcript["name"])
                    entries = self._paged(lambda t, tr=transcript: service.conferenceRecords().transcripts().entries().list(
                        parent=tr["name"], pageSize=self.PAGE, pageToken=t), "transcriptEntries")
                    for entry in entries:
                        text = (entry.get("text") or "").strip()
                        if text:
                            speaker = names.get(entry.get("participant"), "Unknown speaker")
                            if speaker not in speakers:
                                speakers.append(speaker)
                            lines.append(f"{speaker}: {text}")
        except Exception as error:
            return self._failure("Google Meet transcript download failed", error)

        if not transcripts_seen:
            return {"success": True, "ready": False, "reason": "NO_TRANSCRIPT",
                    "message": "The meeting ended but Google has no transcript for it. Transcription must be turned on during the meeting."}
        if not lines:
            return {"success": True, "ready": False, "reason": "EMPTY_TRANSCRIPT",
                    "message": "The transcript exists but contains no spoken text."}
        return {
            "success": True, "ready": True, "reason": "READY",
            "transcript_text": "\n".join(lines),
            "meta": {
                "conference_records": [r["name"] for r in records],
                "transcripts": transcripts_seen,
                "actual_start": records[0].get("startTime"),
                "actual_end": max((r.get("endTime") or "") for r in records),
                "speakers": speakers,
            },
        }


def get_meet_service() -> MeetService:
    if settings.DEMO_MODE:
        return MockMeetService()
    return GoogleMeetService()
