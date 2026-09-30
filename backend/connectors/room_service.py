from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
import logging
import uuid
from backend.app.config import settings

logger = logging.getLogger("room_service")

class RoomService(ABC):
    @abstractmethod
    def request_booking(self, room_name: str, start_time: str, end_time: str, capacity: int, equipment: str, meeting_id: str) -> Dict[str, Any]:
        """Request a physical room or auditorium booking."""
        pass

    @abstractmethod
    def check_room_status(self, booking_id: str) -> Dict[str, Any]:
        """Check status of a submitted room booking."""
        pass

class MockRoomService(RoomService):
    def __init__(self):
        # In-memory store for simulation
        self.bookings: Dict[str, Dict[str, Any]] = {}

    def request_booking(self, room_name: str, start_time: str, end_time: str, capacity: int, equipment: str, meeting_id: str) -> Dict[str, Any]:
        booking_id = f"room-req-{uuid.uuid4().hex[:8]}"
        
        # Scenario logic:
        # If Auditorium Alpha -> requires manual facility manager approval (WAITING_FOR_ROOM)
        # If capacity > 100 -> Auditorium
        # If Conference Room B -> auto-confirmed
        is_auditorium = "auditorium" in room_name.lower() or capacity > 30
        initial_status = "PENDING" if is_auditorium else "CONFIRMED"
        
        record = {
            "booking_id": booking_id,
            "meeting_id": meeting_id,
            "room_name": room_name,
            "start_time": start_time,
            "end_time": end_time,
            "capacity": capacity,
            "equipment": equipment,
            "status": initial_status,
            "approver": settings.AUDITORIUM_BOOKING_EMAIL if is_auditorium else "auto-system",
            "provider": "MockRoomService"
        }
        self.bookings[booking_id] = record
        logger.info(f"[MockRoom] Booking request {booking_id} for {room_name} status: {initial_status}")
        return record

    def check_room_status(self, booking_id: str) -> Dict[str, Any]:
        if booking_id in self.bookings:
            return self.bookings[booking_id]
        return {
            "booking_id": booking_id,
            "status": "NOT_FOUND",
            "message": "Booking record not found in system."
        }

    def simulate_manager_response(self, booking_id: str, approved: bool) -> Dict[str, Any]:
        """Simulation hook for asynchronous lab demonstrations."""
        if booking_id in self.bookings:
            self.bookings[booking_id]["status"] = "CONFIRMED" if approved else "REJECTED"
            return self.bookings[booking_id]
        return {"status": "NOT_FOUND"}

class RealRoomService(RoomService):
    def __init__(self, endpoint_url: Optional[str] = None):
        self.endpoint_url = endpoint_url

    def request_booking(self, room_name: str, start_time: str, end_time: str, capacity: int, equipment: str, meeting_id: str) -> Dict[str, Any]:
        from backend.connectors.email_service import get_email_service
        from worker.state.state_manager import state_manager

        booking_id = f"room-req-{uuid.uuid4().hex[:8]}"
        body = "\n".join([
            f"Room/auditorium: {room_name}",
            f"Date and time: {start_time} - {end_time}",
            f"Capacity requested: {capacity}",
            f"Required equipment: {equipment or 'Not specified'}",
            f"Meeting ID: {meeting_id}",
        ])
        email_result = get_email_service().send_invitation(
            [settings.AUDITORIUM_BOOKING_EMAIL],
            "Auditorium Booking Request",
            body,
            meeting_id,
        )
        if not email_result.get("success"):
            return {
                "booking_id": booking_id,
                "meeting_id": meeting_id,
                "room_name": room_name,
                "status": "FAILED",
                "booking_status": "FAILED",
                "message": "Auditorium request was not sent.",
                "error": email_result.get("error"),
                "http_status": email_result.get("http_status"),
                "error_reason": email_result.get("error_reason"),
                "error_message": email_result.get("error_message"),
                "provider": "EmailAuditoriumWorkflow",
            }
        state_manager.update_room_response(meeting_id, room_name, "PENDING", "Request sent. Waiting for auditorium response.")
        return {
            "booking_id": booking_id,
            "meeting_id": meeting_id,
            "room_name": room_name,
            "start_time": start_time,
            "end_time": end_time,
            "capacity": capacity,
            "equipment": equipment,
            "status": "PENDING",
            "booking_status": "REQUEST_SENT",
            "message": f"Request sent to {settings.AUDITORIUM_BOOKING_EMAIL}. Waiting for auditorium response.",
            "approver": settings.AUDITORIUM_BOOKING_EMAIL,
            "provider": "EmailAuditoriumWorkflow",
        }

    def check_room_status(self, booking_id: str) -> Dict[str, Any]:
        from backend.app.db import SessionLocal, RoomBooking
        db = SessionLocal()
        try:
            booking = db.query(RoomBooking).filter(RoomBooking.notes.contains(booking_id)).first()
            if booking:
                return booking.to_dict()
            return {
                "booking_id": booking_id,
                "status": "NOT_FOUND",
                "message": "No confirmed auditorium booking exists for this request.",
            }
        finally:
            db.close()

_mock_room_instance = MockRoomService()

def get_room_service() -> RoomService:
    if settings.DEMO_MODE:
        return _mock_room_instance
    return RealRoomService()
