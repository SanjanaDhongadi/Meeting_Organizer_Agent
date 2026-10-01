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
    def create_event(self, title: str, start_time: str, end_time: str, attendees: List[str], description: str, is_online: bool = True,
                     organizer_email: Optional[str] = None, location: str = "", event_id: Optional[str] = None) -> Dict[str, Any]:
        """Create (or update, when event_id is given) a calendar event; returns details including meet URL if online."""
        pass

    def get_event(self, event_id: str, organizer_email: Optional[str] = None) -> Dict[str, Any]:
        """Read an event (used to pick up attendee responses)."""
        return {"success": False, "error": "get_event is not supported by this calendar adapter.", "error_code": "UNSUPPORTED"}

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

    def create_event(self, title: str, start_time: str, end_time: str, attendees: List[str], description: str, is_online: bool = True,
                     organizer_email: Optional[str] = None, location: str = "", event_id: Optional[str] = None) -> Dict[str, Any]:
        """
        DEMO SIMULATION ONLY — no real Calendar event is created.
        The returned event_id and meet_url are simulation placeholders.
        Set DEMO_MODE=false and configure Google OAuth to create real events.
        """
        sim_event_id = event_id or f"DEMO-{uuid.uuid4().hex[:12]}"
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
    """Real Google Calendar adapter. Every call uses stored OAuth tokens of a registered employee."""

    def __init__(self, credentials: Optional[Any] = None):
        self.credentials = credentials

    def _credentials_for(self, email: Optional[str]):
        from backend.connectors.google_auth import load_google_credentials_with_error
        if self.credentials:
            return self.credentials, None
        return load_google_credentials_with_error(email)

    @staticmethod
    def _build(credentials):
        from googleapiclient.discovery import build
        return build("calendar", "v3", credentials=credentials, cache_discovery=False)

    @staticmethod
    def _failure(prefix: str, error: Exception) -> Dict[str, Any]:
        from backend.connectors.google_auth import google_error_detail
        details = google_error_detail(error)
        logger.error(
            "[CalendarService] %s — HTTP %s | reason: %s | message: %s | raw: %s",
            prefix, details["http_status"], details["error_reason"], details["error_message"], error,
        )
        return {
            "success": False,
            "error": f"{prefix}: {details['error_detail']}",
            "error_code": "GOOGLE_API_FAILURE",
            "http_status": details["http_status"],
            "error_reason": details["error_reason"],
            "error_message": details["error_message"],
        }

    def _freebusy(self, email: str, time_min: str, time_max: str) -> Dict[str, Any]:
        credentials, cred_error = self._credentials_for(email)
        if not credentials:
            return {"status": "UNVERIFIED", "reason": cred_error["error"], "error_code": cred_error["error_code"]}
        try:
            result = self._build(credentials).freebusy().query(body={
                "timeMin": time_min,
                "timeMax": time_max,
                "items": [{"id": email}],
            }).execute()
        except Exception as error:
            failure = self._failure(f"Google Calendar free/busy failed for {email}", error)
            return {"status": "UNVERIFIED", "reason": failure["error"], **{k: failure[k] for k in ("http_status", "error_reason", "error_message")}}
        calendar = result.get("calendars", {}).get(email, {})
        if calendar.get("errors"):
            return {"status": "UNVERIFIED", "reason": f"Google Calendar API error: {calendar['errors']}"}
        busy = calendar.get("busy") or []
        return {"status": "BUSY" if busy else "AVAILABLE", "busy": busy,
                "reason": "Calendar shows conflicts during this time." if busy else "Calendar is free for this time slot."}

    def check_availability(self, emails: List[str], start_time: str, end_time: str) -> Dict[str, Any]:
        availability = {}
        for email in emails:
            result = self._freebusy(email, start_time, end_time)
            result.pop("busy", None)
            availability[email] = result
        return {
            "all_available": bool(emails) and all(v["status"] == "AVAILABLE" for v in availability.values()),
            "details": availability,
            "checked_range": {"start": start_time, "end": end_time},
            "provider": "GoogleCalendarService",
        }

    def find_common_slots(self, emails: List[str], date_str: str, duration_minutes: int) -> List[Dict[str, str]]:
        from datetime import timezone as _tz
        from zoneinfo import ZoneInfo
        base_date = date_str.split("T")[0] if "T" in date_str else date_str
        try:
            local_tz = ZoneInfo(settings.MEETING_TIMEZONE)
        except Exception:
            local_tz = _tz.utc
        # Search 09:00-17:00 in the organization's meeting time zone.
        day_start = datetime.fromisoformat(f"{base_date}T09:00:00").replace(tzinfo=local_tz).astimezone(_tz.utc)
        day_end = datetime.fromisoformat(f"{base_date}T17:00:00").replace(tzinfo=local_tz).astimezone(_tz.utc)
        window_start = day_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        window_end = day_end.strftime("%Y-%m-%dT%H:%M:%SZ")
        busy_by_email: Dict[str, List[Dict[str, str]]] = {}
        unverified: List[str] = []
        for email in emails:
            result = self._freebusy(email, window_start, window_end)
            if result["status"] == "UNVERIFIED":
                # Never assume an unverifiable calendar is free — record it on every slot instead.
                unverified.append(email)
                continue
            busy_by_email[email] = result.get("busy", [])

        if emails and not busy_by_email:
            return []  # nothing could be verified, so no slot can be proposed as "common"

        slots = []
        for offset in range(0, 8):
            start = day_start + timedelta(hours=offset)
            end = start + timedelta(minutes=duration_minutes)
            if end > day_end:
                continue
            conflict = any(
                start < datetime.fromisoformat(block["end"].replace("Z", "+00:00"))
                and end > datetime.fromisoformat(block["start"].replace("Z", "+00:00"))
                for blocks in busy_by_email.values() for block in blocks
            )
            if not conflict:
                slots.append({
                    "start": start.isoformat().replace("+00:00", "Z"),
                    "end": end.isoformat().replace("+00:00", "Z"),
                    "duration": f"{duration_minutes}m",
                    "verified_for": sorted(busy_by_email.keys()),
                    "unverified_participants": unverified,
                })
            if len(slots) >= 3:
                break
        return slots

    def create_event(self, title: str, start_time: str, end_time: str, attendees: List[str], description: str, is_online: bool = True,
                     organizer_email: Optional[str] = None, location: str = "", event_id: Optional[str] = None) -> Dict[str, Any]:
        from backend.connectors.google_auth import resolve_organizer_email

        if not start_time or not end_time:
            return {"success": False, "error": "Meeting start or end time is missing. Cannot create calendar event.",
                    "error_code": "MISSING_DATETIME"}
        organizer = organizer_email or resolve_organizer_email(attendees)
        if not organizer and not self.credentials:
            error_msg = (
                "No Google account is connected to organize this event. Connect the organizer's Google account "
                "from the Employees view (or set GOOGLE_CALENDAR_OWNER_EMAIL to a connected employee)."
            )
            logger.error("[CalendarService] %s", error_msg)
            return {"success": False, "error": error_msg, "error_code": "NO_CREDENTIALS"}
        credentials, cred_error = self._credentials_for(organizer)
        if not credentials:
            return {"success": False, "error": cred_error["error"], "error_code": cred_error["error_code"], "organizer_email": organizer}

        event_body = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start_time, "timeZone": "UTC"},
            "end": {"dateTime": end_time, "timeZone": "UTC"},
            "attendees": [{"email": email} for email in attendees if email],
        }
        if location:
            event_body["location"] = location
        conference_request = {
            "createRequest": {
                "requestId": f"req-{uuid.uuid4().hex[:10]}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        if is_online and not event_id:
            event_body["conferenceData"] = conference_request

        logger.info("[CalendarService] %s event: title=%r start=%r end=%r attendees=%r online=%s organizer=%s",
                    "Updating" if event_id else "Creating", title, start_time, end_time, attendees, is_online, organizer)
        try:
            events = self._build(credentials).events()
            if event_id:
                # Updating an existing event keeps its Meet link; a conference is only requested if it has none.
                created_event = events.patch(
                    calendarId="primary", eventId=event_id, body=event_body,
                    conferenceDataVersion=1 if is_online else 0, sendUpdates="all",
                ).execute()
                if is_online and not created_event.get("hangoutLink"):
                    created_event = events.patch(
                        calendarId="primary", eventId=event_id, body={"conferenceData": conference_request},
                        conferenceDataVersion=1, sendUpdates="all",
                    ).execute()
            else:
                created_event = events.insert(
                    calendarId="primary", body=event_body,
                    conferenceDataVersion=1 if is_online else 0, sendUpdates="all",
                ).execute()
            # Meet links can be provisioned asynchronously; re-read once while Google reports "pending".
            status_code = (((created_event.get("conferenceData") or {}).get("createRequest") or {}).get("status") or {}).get("statusCode")
            if is_online and not created_event.get("hangoutLink") and status_code == "pending":
                created_event = events.get(calendarId="primary", eventId=created_event["id"]).execute()
        except Exception as error:
            failure = self._failure("Google Calendar API error", error)
            failure["organizer_email"] = organizer
            return failure

        meet_url = created_event.get("hangoutLink", "") or ""
        result = {
            "success": True,
            "event_id": created_event.get("id", ""),
            "html_link": created_event.get("htmlLink", ""),
            "title": title,
            "start": start_time,
            "end": end_time,
            "attendees": attendees,
            "meet_url": meet_url,
            "organizer_email": organizer,
            "provider": "GoogleCalendarService",
        }
        if is_online and not meet_url:
            conference_status = status_code or "missing"
            result["meet_error"] = (
                f"Google created the Calendar event but did not return a Google Meet link (conference status: {conference_status}). "
                "Check that Google Meet is enabled for this Google account/Workspace."
            )
            logger.error("[CalendarService] %s", result["meet_error"])
        logger.info("[CalendarService] Event saved: id=%r meet=%r", result["event_id"], meet_url)
        return result

    def get_event(self, event_id: str, organizer_email: Optional[str] = None) -> Dict[str, Any]:
        credentials, cred_error = self._credentials_for(organizer_email)
        if not credentials:
            return {"success": False, "error": cred_error["error"], "error_code": cred_error["error_code"]}
        try:
            event = self._build(credentials).events().get(calendarId="primary", eventId=event_id).execute()
        except Exception as error:
            return self._failure("Google Calendar event read failed", error)
        return {
            "success": True,
            "event_id": event.get("id"),
            "status": event.get("status"),
            "meet_url": event.get("hangoutLink", ""),
            "attendees": [
                {"email": (a.get("email") or "").lower(), "response_status": a.get("responseStatus", "needsAction")}
                for a in event.get("attendees", [])
            ],
        }

def get_calendar_service() -> CalendarService:
    if settings.DEMO_MODE:
        return MockCalendarService()
    return GoogleCalendarService()
