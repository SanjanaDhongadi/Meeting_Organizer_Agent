import logging
from typing import Dict, Any
from backend.connectors.calendar_service import get_calendar_service

logger = logging.getLogger("calendar_job")

class CalendarJob:
    def sync_calendar(self, meeting_id: str, title: str, start: str, end: str, attendees: list) -> Dict[str, Any]:
        logger.info(f"[CalendarJob] Syncing calendar for {meeting_id}")
        svc = get_calendar_service()
        res = svc.create_event(title, start, end, attendees, "Synchronized by worker", is_online=True)
        return res
