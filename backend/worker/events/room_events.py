from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field

class RoomBookingResponseEvent(BaseModel):
    event_id: str
    meeting_id: str
    room_name: str
    status: str  # CONFIRMED, REJECTED
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    approver: Optional[str] = "facilities@example.com"
    notes: Optional[str] = ""
    # SIMULATED (demo buttons) vs a real external response (e.g. GOOGLE_CALENDAR, GMAIL_REPLY)
    source: str = "EXTERNAL"
