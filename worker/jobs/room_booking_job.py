import logging
from typing import Dict, Any
from backend.connectors.room_service import get_room_service
from worker.state.state_manager import state_manager

logger = logging.getLogger("room_booking_job")

class RoomBookingJob:
    def check_and_update_room(self, meeting_id: str, booking_id: str) -> Dict[str, Any]:
        room_svc = get_room_service()
        res = room_svc.check_room_status(booking_id)
        status = res.get("status")

        if status == "CONFIRMED":
            state_manager.update_room_response(meeting_id, res.get("room_name", "Auditorium Alpha"), "CONFIRMED")
            state_manager.update_meeting_status(meeting_id, "BOOKED", "ROOM_CONFIRMED", f"Room booking {booking_id} confirmed by facilities.")
        elif status == "REJECTED":
            state_manager.update_room_response(meeting_id, res.get("room_name", "Auditorium Alpha"), "REJECTED")
            state_manager.update_meeting_status(meeting_id, "RESCHEDULING_REQUIRED", "ROOM_REJECTED", f"Room booking {booking_id} rejected by facilities.")

        return res
