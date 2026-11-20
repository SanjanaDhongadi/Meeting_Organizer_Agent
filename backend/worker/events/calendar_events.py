from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field

class CalendarSyncEvent(BaseModel):
    event_id: str
    meeting_id: str
    calendar_event_id: str
    status: str  # SYNCED, CANCELLED, UPDATED
    attendees: List[str] = []
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
