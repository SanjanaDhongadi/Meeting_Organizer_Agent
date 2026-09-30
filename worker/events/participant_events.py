from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field

class ParticipantResponseEvent(BaseModel):
    event_id: str
    meeting_id: str
    email: str
    response: str  # ACCEPTED, REJECTED, TENTATIVE
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    notes: Optional[str] = ""
