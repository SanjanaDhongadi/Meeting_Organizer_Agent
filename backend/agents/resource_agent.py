from typing import Dict, Any, Optional
from backend.runtime.agent_interface import BaseAgent, AgentOutput
from database.seed.seed_data import SEED_ROOMS
from backend.app.config import settings
import logging

logger = logging.getLogger("resource_agent")

class ResourceAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Resource/Room Agent",
            role="Manage conference rooms, auditorium allocations, and virtual meeting links",
            description="Allocates physical spaces with required capacity/equipment or provisions virtual Google Meet rooms."
        )
    def execute(self, state: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> AgentOutput:
        mode = (state.get("mode") or "ONLINE").upper()
        participants = state.get("resolved_participants", [])
        num_attendees = max(len(participants), 2)
        purpose = state.get("purpose", "")
        start_time = state.get("target_start_time") or "2026-10-02T15:00:00Z"
        end_time = state.get("target_end_time") or "2026-10-02T16:00:00Z"
        meeting_id = state.get("meeting_id", "draft-meeting")

        if mode == "ONLINE":
            return AgentOutput(
                agent_name=self.name,
                status="SUCCESS",
                data={
                    "mode": "ONLINE",
                    "virtual_provider": "Google Meet",
                    "provisional_meet_url": "",
                    "room_status": "NOT_CREATED"
                }
            )

        # OFFLINE: Physical room selection
        req_room = state.get("room_name") or ""
        equipment = state.get("equipment") or ""

        if not req_room and SEED_ROOMS:
            # Pick from the configured catalog based on capacity and purpose
            large_event = "keynote" in purpose.lower() or "all-hands" in purpose.lower() or num_attendees > 20
            if large_event:
                req_room = max(SEED_ROOMS, key=lambda r: r.get("capacity", 0))["name"]
            else:
                fitting = sorted((r for r in SEED_ROOMS if r.get("capacity", 0) >= num_attendees and not r.get("requires_approval")),
                                 key=lambda r: r.get("capacity", 0))
                req_room = (fitting[0] if fitting else max(SEED_ROOMS, key=lambda r: r.get("capacity", 0)))["name"]
        room_data = next(
            (room for room in SEED_ROOMS if room["name"].lower() == req_room.lower()),
            None,
        )
        capacity = room_data["capacity"] if room_data else 14
        equipment = equipment or (room_data["equipment"] if room_data else "Projector, Audio System")

        return AgentOutput(
            agent_name=self.name,
            status="SUCCESS",
            data={
                "mode": "OFFLINE",
                "room_name": req_room,
                "capacity": capacity,
                "equipment": equipment,
                "booking_id": None,
                "booking_status": "NOT_REQUESTED",
                "requires_facility_approval": bool(room_data and room_data["requires_approval"]),
                "approver": settings.AUDITORIUM_BOOKING_EMAIL if room_data and room_data["requires_approval"] else None
            },
            tool_calls=[]
        )
