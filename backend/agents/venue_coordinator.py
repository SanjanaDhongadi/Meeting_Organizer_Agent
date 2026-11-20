"""Helpers for the manual venue booking email attached to one offline meeting."""
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
from app.config import settings


# Statuses of a venue request row (room_bookings). SUPERSEDED/CANCELLED rows are history only.
INACTIVE_REQUEST_STATUSES = {"SUPERSEDED", "CANCELLED", "FAILED"}
# Meetings that hold a venue for their time slot (same idea as attendee double-booking).
VENUE_HOLDING_STATUSES = ("EXECUTING", "WAITING_FOR_PARTICIPANTS", "WAITING_FOR_AUDITORIUM_RESPONSE", "WAITING_FOR_ROOM",
                          "CONFIRMED", "BOOKED")


def valid_coordinator_email(address: Optional[str]) -> bool:
    return bool(address and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address.strip()))


def local_when(start: str, end: str) -> Dict[str, str]:
    """Human-readable date/time in the organization's meeting time zone."""
    try:
        tz = ZoneInfo(settings.MEETING_TIMEZONE)
    except Exception:
        tz = timezone.utc
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(tz)
        e = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(tz)
    except Exception:
        return {"date": "Not provided", "time": "Not provided"}
    return {"date": s.strftime("%A, %d %B %Y"),
            "time": f"{s.strftime('%I:%M %p').lstrip('0')} – {e.strftime('%I:%M %p').lstrip('0')} ({settings.MEETING_TIMEZONE})"}


def request_subject(meeting_id: str, title: str = "") -> str:
    # The meeting ID in brackets lets the app match the coordinator's reply to this meeting.
    return f"Venue booking request: {title or 'Meeting'} [{meeting_id}]"


def ensure_reference(subject: str, meeting_id: str) -> str:
    subject = (subject or "").strip()
    return subject if meeting_id in subject else f"{subject} [{meeting_id}]".strip()


def build_request_draft(meeting: Dict[str, Any], organizer_email: Optional[str] = None) -> Dict[str, str]:
    """Draft email (editable by the organizer) built only from the meeting's real details."""
    when = local_when(meeting.get("scheduled_start") or "", meeting.get("scheduled_end") or "")
    participants = meeting.get("participants") or []
    lines = [
        "Dear Venue Coordinator,",
        "",
        "I would like to request the following venue for an in-person meeting. Please check its availability and "
        "reply to this email with CONFIRMED if the venue is booked, or REJECTED if it is not available.",
        "",
        f"Meeting: {meeting.get('title') or 'Not provided'}",
        f"Venue: {meeting.get('room_name') or 'Not provided'}",
        f"Date: {when['date']}",
        f"Time: {when['time']}",
        f"Duration: {meeting.get('duration_minutes') or 'Not provided'} minutes",
        f"Number of participants: {len(participants)}",
        f"Purpose: {meeting.get('purpose') or 'Not provided'}",
    ]
    if organizer_email:
        lines.append(f"Organizer: {organizer_email}")
    lines += [f"Meeting reference: {meeting.get('id') or meeting.get('meeting_id')}", "", "Thank you."]
    return {"to": (meeting.get("venue_coordinator_email") or "").strip(),
            "subject": request_subject(meeting.get("id") or meeting.get("meeting_id") or "", meeting.get("title") or ""),
            "body": "\n".join(lines)}


def active_request(room_bookings: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    active = [rb for rb in room_bookings or [] if (rb.get("status") or "") not in INACTIVE_REQUEST_STATUSES]
    return active[-1] if active else None


def request_status_label(request: Optional[Dict[str, Any]]) -> str:
    if not request:
        return "NOT_SENT"
    return {"SENDING": "SENDING", "PENDING": "SENT", "CONFIRMED": "CONFIRMED", "REJECTED": "DECLINED"}.get(request.get("status") or "", "SENT")


def venue_conflicts_in_app(venue: str, start: str, end: str, exclude_meeting_id: Optional[str]) -> List[Dict[str, Any]]:
    """Other meetings in this application that already hold the same venue at an overlapping time."""
    if not venue or not start or not end:
        return []
    from sqlalchemy import text, bindparam
    from app.db import engine
    query = text(
        "SELECT id, title, status, scheduled_start, scheduled_end FROM meetings "
        "WHERE lower(trim(room_name)) = :venue AND upper(mode) = 'OFFLINE' AND status IN :statuses "
        "AND scheduled_start < :end AND scheduled_end > :start AND id != :exclude"
    ).bindparams(bindparam("statuses", expanding=True))
    with engine.connect() as conn:
        rows = conn.execute(query, {"venue": venue.strip().lower(), "statuses": list(VENUE_HOLDING_STATUSES),
                                    "start": start, "end": end, "exclude": exclude_meeting_id or ""}).mappings().all()
    return [dict(r) for r in rows]


def check_venue_availability(venue: str, start: str, end: str, meeting_id: Optional[str] = None) -> Dict[str, Any]:
    """Check for overlapping offline meetings in this application only."""
    checked_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    base = {"venue": venue, "checked_at": checked_at}
    clashes = venue_conflicts_in_app(venue, start, end, meeting_id)
    if clashes:
        held = clashes[0]
        when = local_when(held["scheduled_start"], held["scheduled_end"])
        return {**base, "status": "BUSY", "source": "application",
                "reason": f"{venue} is already used by '{held['title']}' on {when['date']}, {when['time']}."}
    return {**base, "status": "AVAILABLE", "source": "application",
            "reason": "No overlapping offline booking is recorded in this application."}


def venue_view(meeting: Dict[str, Any], workflow: Dict[str, Any], organizer_email: Optional[str] = None) -> Dict[str, Any]:
    """Everything the Venue Coordinator tab / details section shows for an OFFLINE meeting."""
    request = active_request(meeting.get("room_bookings") or [])
    bookings = meeting.get("room_bookings") or []
    latest_send = next((rb for rb in reversed(bookings) if rb.get("status") in {"SENDING", "PENDING", "FAILED", "CONFIRMED", "REJECTED"}), None)
    email_status = {"PENDING": "SENT", "CONFIRMED": "SENT", "REJECTED": "SENT"}.get(
        (latest_send or {}).get("status"), (latest_send or {}).get("status", "NOT_SENT"))
    address = (meeting.get("venue_coordinator_email") or "").strip()
    return {
        "coordinator_name": "Venue coordinator",
        "coordinator_email": address,
        "configured": valid_coordinator_email(address),
        "configuration_error": None if valid_coordinator_email(address) else "Enter a valid venue coordinator email in meeting details before sending.",
        "venue": meeting.get("room_name") or "",
        "availability": (workflow.get("venue_check") if (workflow.get("venue_check") or {}).get("source") == "application"
                         and (workflow.get("venue_check") or {}).get("status") in {"AVAILABLE", "BUSY"} else None),
        "request_status": request_status_label(request),
        "email_status": email_status,
        "email_status_message": (latest_send or {}).get("notes", ""),
        "request": request,
        "history": [rb for rb in meeting.get("room_bookings") or [] if rb is not request],
        "draft": build_request_draft(meeting, organizer_email),
    }
