from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import logging
import uuid
from backend.app.config import settings

logger = logging.getLogger("calendar_service")

class CalendarService(ABC):
    @abstractmethod
    def check_availability(self, emails: List[str], start_time: str, end_time: str) -> Dict[str, Any]:
        """Check free/busy status for a list of emails."""
        pass

    @abstractmethod
    def find_common_slots(self, emails: List[str], date_str: str, duration_minutes: int) -> List[Dict[str, str]]:
        """Find common available time slots on a given date."""
        pass

    @abstractmethod
    def create_event(self, title: str, start_time: str, end_time: str, attendees: List[str], description: str, is_online: bool = True) -> Dict[str, Any]:
        """Create a calendar event and return details including meet URL if online."""
        pass

class MockCalendarService(CalendarService):
    """
    DEMO/SIMULATION calendar service used when DEMO_MODE=true.
    Does NOT create real Calendar events. Does NOT generate real Meet links.
    All returned IDs and URLs are simulated placeholders.
    """
    def __init__(self):
        pass

    def check_availability(self, emails: List[str], start_time: str, end_time: str) -> Dict[str, Any]:
        logger.info("[DEMO] Simulating availability check for %s — real calendar not accessed.", emails)
        availability = {
            email: {
                "status": "UNVERIFIED",
                "reason": "DEMO_MODE is enabled. Real calendar access requires DEMO_MODE=false and Google OAuth.",
            }
            for email in emails
        }
        return {
            "all_available": False,
            "details": availability,
            "checked_range": {"start": start_time, "end": end_time}
        }

    def find_common_slots(self, emails: List[str], date_str: str, duration_minutes: int = 30) -> List[Dict[str, str]]:
        slots = []
        base_date = date_str.split("T")[0] if "T" in date_str else date_str
        candidate_hours = [10, 11, 14, 15, 16]
        for ch in candidate_hours:
            s_time = f"{base_date}T{ch:02d}:00:00Z"
            e_dt = datetime.fromisoformat(f"{base_date}T{ch:02d}:00:00+00:00") + timedelta(minutes=duration_minutes)
            e_time = e_dt.isoformat().replace("+00:00", "Z")
            slots.append({"start": s_time, "end": e_time, "duration": f"{duration_minutes}m"})
            if len(slots) >= 3:
                break
        return slots

    def create_event(self, title: str, start_time: str, end_time: str, attendees: List[str], description: str, is_online: bool = True) -> Dict[str, Any]:
        """
        DEMO SIMULATION ONLY — no real Calendar event is created.
        The returned event_id and meet_url are simulation placeholders.
        Set DEMO_MODE=false and configure Google OAuth to create real events.
        """
        sim_event_id = f"DEMO-{uuid.uuid4().hex[:12]}"
        sim_meet_url = f"https://meet.google.com/DEMO-{uuid.uuid4().hex[:3]}-{uuid.uuid4().hex[:4]}" if is_online else ""
        logger.warning(
            "[DEMO] Simulated calendar event '%s' — NOT a real Google Calendar event. "
            "Set DEMO_MODE=false and connect Google OAuth to create real events.",
            sim_event_id
        )
        return {
            "success": True,
            "event_id": sim_event_id,
            "title": title,
            "start": start_time,
            "end": end_time,
            "attendees": attendees,
            "meet_url": sim_meet_url,
            "mode": "ONLINE" if is_online else "OFFLINE",
            "provider": "DEMO_SIMULATION",
            "warning": "This is a simulated event. Set DEMO_MODE=false to use real Google Calendar."
        }

class GoogleCalendarService(CalendarService):
    def __init__(self, credentials: Optional[Any] = None):
        self.credentials = credentials
        self._service = None

    def _get_service(self):
        if not self._service:
            from googleapiclient.discovery import build
            self._service = build("calendar", "v3", credentials=self.credentials)
        return self._service

    def check_availability(self, emails: List[str], start_time: str, end_time: str) -> Dict[str, Any]:
        from googleapiclient.discovery import build
        from backend.connectors.google_auth import load_google_credentials

        availability = {}
        for email in emails:
            credentials = self.credentials or load_google_credentials(email)
            if not credentials:
                availability[email] = {
                    "status": "UNVERIFIED",
                    "reason": "Calendar access not authorized. Employee must connect Google Calendar via the Employee Dashboard.",
                }
                continue
            try:
                service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
                result = service.freebusy().query(body={
                    "timeMin": start_time,
                    "timeMax": end_time,
                    "items": [{"id": email}],
                }).execute()
                calendar = result.get("calendars", {}).get(email, {})
                if calendar.get("errors"):
                    availability[email] = {
                        "status": "UNVERIFIED",
                        "reason": f"Google Calendar API error: {calendar['errors']}",
                    }
                elif calendar.get("busy"):
                    availability[email] = {"status": "BUSY", "reason": "Calendar shows conflicts during this time."}
                else:
                    availability[email] = {"status": "AVAILABLE", "reason": "Calendar is free for this time slot."}
            except Exception as error:
                logger.warning("Google Calendar availability check failed for %s: %s", email, error)
                availability[email] = {
                    "status": "UNVERIFIED",
                    "reason": f"Calendar check failed: {error}",
                }
        return {
            "all_available": bool(emails) and all(v["status"] == "AVAILABLE" for v in availability.values()),
            "details": availability,
            "checked_range": {"start": start_time, "end": end_time},
        }

    def find_common_slots(self, emails: List[str], date_str: str, duration_minutes: int) -> List[Dict[str, str]]:
        from googleapiclient.discovery import build
        from backend.connectors.google_auth import load_google_credentials

        base_date = date_str.split("T")[0] if "T" in date_str else date_str
        window_start = f"{base_date}T09:00:00Z"
        window_end = f"{base_date}T17:00:00Z"
        busy_by_email = {}
        for email in emails:
            credentials = self.credentials or load_google_credentials(email)
            if not credentials:
                continue
            try:
                service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
                result = service.freebusy().query(body={
                    "timeMin": window_start,
                    "timeMax": window_end,
                    "items": [{"id": email}],
                }).execute()
                calendar = result.get("calendars", {}).get(email, {})
                busy_by_email[email] = calendar.get("busy") or []
            except Exception as error:
                logger.warning("Google Calendar slot search failed for %s: %s", email, error)
                busy_by_email[email] = []

        slots = []
        for hour in range(9, 17):
            start = datetime.fromisoformat(f"{base_date}T{hour:02d}:00:00+00:00")
            end = start + timedelta(minutes=duration_minutes)
            if end.hour > 17 or (end.hour == 17 and end.minute > 0):
                continue
            start_iso = start.isoformat().replace("+00:00", "Z")
            end_iso = end.isoformat().replace("+00:00", "Z")
            conflict = False
            for busy_blocks in busy_by_email.values():
                for block in busy_blocks:
                    b_start = datetime.fromisoformat(block["start"].replace("Z", "+00:00"))
                    b_end = datetime.fromisoformat(block["end"].replace("Z", "+00:00"))
                    if start < b_end and end > b_start:
                        conflict = True
                        break
                if conflict:
                    break
            if not conflict:
                slots.append({"start": start_iso, "end": end_iso, "duration": f"{duration_minutes}m"})
            if len(slots) >= 3:
                break
        return slots

    def create_event(self, title: str, start_time: str, end_time: str, attendees: List[str], description: str, is_online: bool = True) -> Dict[str, Any]:
        try:
            from backend.connectors.google_auth import load_google_credentials
            credentials = self.credentials or load_google_credentials()
            if not credentials:
                for attendee in attendees or []:
                    attendee_creds = load_google_credentials(attendee)
                    if attendee_creds:
                        credentials = attendee_creds
                        break
            if not credentials:
                error_msg = (
                    "Google Calendar OAuth credentials not found. "
                    "No employee has authorized Google Calendar access via the Employee Dashboard. "
                    "Go to Employee Dashboard → Connect Calendar for the organizer's account."
                )
                logger.error("[CalendarService] %s", error_msg)
                return {"success": False, "error": error_msg, "error_code": "NO_CREDENTIALS"}

            from googleapiclient.discovery import build
            service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

            if not start_time or not end_time:
                return {
                    "success": False,
                    "error": "Meeting start or end time is missing. Cannot create calendar event.",
                    "error_code": "MISSING_DATETIME"
                }

            event_body = {
                "summary": title,
                "description": description,
                "start": {"dateTime": start_time, "timeZone": "UTC"},
                "end": {"dateTime": end_time, "timeZone": "UTC"},
                "attendees": [{"email": email} for email in attendees if email]
            }
            if is_online:
                event_body["conferenceData"] = {
                    "createRequest": {
                        "requestId": f"req-{uuid.uuid4().hex[:10]}",
                        "conferenceSolutionKey": {"type": "hangoutsMeet"}
                    }
                }

            logger.info(
                "[CalendarService] Creating event: title=%r, start=%r, end=%r, attendees=%r, is_online=%s",
                title, start_time, end_time, attendees, is_online
            )

            created_event = service.events().insert(
                calendarId="primary",
                body=event_body,
                conferenceDataVersion=1 if is_online else 0,
                sendUpdates="all"
            ).execute()

            meet_url = created_event.get("hangoutLink", "")
            event_id = created_event.get("id", "")
            logger.info("[CalendarService] Event created successfully: id=%r, meet=%r", event_id, meet_url)

            return {
                "success": True,
                "event_id": event_id,
                "title": title,
                "start": start_time,
                "end": end_time,
                "attendees": attendees,
                "meet_url": meet_url,
                "provider": "GoogleCalendarService"
            }

        except Exception as e:
            # Extract the real Google API error details — never hide them
            error_detail = str(e)
            http_status = None
            error_reason = None
            error_message = None

            try:
                # google-api-python-client raises HttpError with .resp and .content
                if hasattr(e, "resp") and hasattr(e, "content"):
                    import json as _json
                    http_status = e.resp.status
                    raw_content = e.content.decode("utf-8") if isinstance(e.content, bytes) else str(e.content)
                    content = _json.loads(raw_content)
                    api_error = content.get("error", {})
                    error_reason = api_error.get("errors", [{}])[0].get("reason", "") if api_error.get("errors") else ""
                    error_message = api_error.get("message", "")
                    error_detail = (
                        f"HTTP {http_status}: {error_message}"
                        + (f" (reason: {error_reason})" if error_reason else "")
                    )
            except Exception:
                pass

            logger.error(
                "[CalendarService] Google Calendar API FAILED — HTTP %s | reason: %s | message: %s | raw: %s",
                http_status, error_reason, error_message, e
            )
            return {
                "success": False,
                "error": f"Google Calendar API error: {error_detail}",
                "error_code": "GOOGLE_API_FAILURE",
                "http_status": http_status,
                "error_reason": error_reason,
                "error_message": error_message,
            }

def get_calendar_service() -> CalendarService:
    if settings.DEMO_MODE:
        return MockCalendarService()
    return GoogleCalendarService()
