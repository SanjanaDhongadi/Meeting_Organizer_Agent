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
