from typing import Dict, Any, Optional
import json
import logging
from backend.app.db import SessionLocal, Meeting

logger = logging.getLogger("session_memory")

class SessionMemory:
    """
    Lab 4: Session Memory
    Maintains active workflow execution state, current request, draft plan,
    and approval state across agent cycles and pause/resume events.
    """
    def __init__(self):
        self._in_memory_sessions: Dict[str, Dict[str, Any]] = {}

    def init_session(self, meeting_id: str, raw_request: str) -> Dict[str, Any]:
        state = {
            "meeting_id": meeting_id,
            "raw_request": raw_request,
            "status": "DRAFT",
            "approval_state": "PENDING",
            "draft_plan": {},
            "active_node": "INTAKE",
            "context_variables": {},
            "trace": []
        }
        self._in_memory_sessions[meeting_id] = state
        return state

    def update_session(self, meeting_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        if meeting_id not in self._in_memory_sessions:
            self._in_memory_sessions[meeting_id] = {"meeting_id": meeting_id}
        self._in_memory_sessions[meeting_id].update(updates)
        return self._in_memory_sessions[meeting_id]

    def get_session(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        # Check in-memory first
        if meeting_id in self._in_memory_sessions:
            return self._in_memory_sessions[meeting_id]
        
        # Fall back to database
        db = SessionLocal()
        try:
            m = db.query(Meeting).filter(Meeting.id == meeting_id).first()
            if m:
                state = {
                    "meeting_id": m.id,
                    "raw_request": m.raw_request,
                    "status": m.status,
                    "approval_state": m.approval_status,
                    "draft_plan": m.to_dict(),
                    "active_node": m.status,
                    "context_variables": {},
                    "trace": []
                }
                self._in_memory_sessions[meeting_id] = state
                return state
        finally:
            db.close()
        return None

    def append_trace(self, meeting_id: str, entry: Dict[str, Any]):
        session = self.get_session(meeting_id)
        if session:
            session.setdefault("trace", []).append(entry)

session_memory = SessionMemory()
