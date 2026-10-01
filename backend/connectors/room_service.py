from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
import logging
import uuid
from backend.app.config import settings

logger = logging.getLogger("room_service")

class RoomService(ABC):
    @abstractmethod
    def request_booking(self, room_name: str, start_time: str, end_time: str, capacity: int, equipment: str, meeting_id: str,
                        **details: Any) -> Dict[str, Any]:
        """Request a physical room or auditorium booking."""
        pass

    @abstractmethod
    def check_room_status(self, booking_id: str) -> Dict[str, Any]:
        """Check status of a submitted room booking."""
        pass

class MockRoomService(RoomService):
    """
    Deterministic in-memory adapter for unit tests only. It is NOT used by the live application:
    there is no facility booking API, so the live flow uses the email-based request workflow below.
    """
    def __init__(self):
        # In-memory store for simulation
        self.bookings: Dict[str, Dict[str, Any]] = {}

    def request_booking(self, room_name: str, start_time: str, end_time: str, capacity: int, equipment: str, meeting_id: str,
                        **details: Any) -> Dict[str, Any]:
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

def auditorium_request_subject(meeting_id: str) -> str:
    return f"Auditorium Booking Request [{meeting_id}]"

class RealRoomService(RoomService):
    """
    Email-based auditorium/room request workflow (there is no facility booking API).
    Sends the request to AUDITORIUM_BOOKING_EMAIL through the active email adapter (Gmail in live mode),
    records the booking as PENDING ("request sent / waiting for response") and never reports it as booked.
    Confirmation or rejection arrives later as a separate response event.
    """
    def __init__(self, endpoint_url: Optional[str] = None):
        self.endpoint_url = endpoint_url

    def request_booking(self, room_name: str, start_time: str, end_time: str, capacity: int, equipment: str, meeting_id: str,
                        **details: Any) -> Dict[str, Any]:
        from backend.connectors.email_service import get_email_service
        from worker.state.state_manager import state_manager

        booking_id = f"room-req-{uuid.uuid4().hex[:8]}"
        body = "\n".join([
            "Please confirm or reject the following room/auditorium booking request.",
            "Reply to this email with CONFIRMED or REJECTED (keep the meeting ID in the subject).",
            "",
            f"Room/auditorium requirement: {room_name}",
            f"Date and time: {start_time} - {end_time}",
            f"Duration: {details.get('duration_minutes', 'Not specified')} minutes",
            f"Number of participants: {details.get('participant_count', 'Not specified')}",
            f"Capacity requested: {capacity}",
            f"Required equipment: {equipment or 'Not specified'}",
            f"Meeting purpose: {details.get('purpose') or 'Not specified'}",
            f"Meeting ID: {meeting_id}",
            f"Request reference: {booking_id}",
        ])
        email_result = get_email_service().send_invitation(
            [settings.AUDITORIUM_BOOKING_EMAIL],
            auditorium_request_subject(meeting_id),
            body,
            meeting_id,
            sender_email=details.get("sender_email"),
        )
        if not email_result.get("success"):
            state_manager.update_room_response(
                meeting_id, room_name, "FAILED", f"Request NOT sent: {email_result.get('error')}",
                capacity=capacity, equipment=equipment,
            )
            return {
                "success": False,
                "booking_id": booking_id,
                "meeting_id": meeting_id,
                "room_name": room_name,
                "status": "FAILED",
                "booking_status": "FAILED",
                "message": "Auditorium request was not sent.",
                "error": email_result.get("error"),
                "error_code": email_result.get("error_code"),
                "http_status": email_result.get("http_status"),
                "error_reason": email_result.get("error_reason"),
                "error_message": email_result.get("error_message"),
                "provider": "EmailAuditoriumWorkflow",
            }
        state_manager.update_room_response(
            meeting_id, room_name, "PENDING",
            f"Request sent to {settings.AUDITORIUM_BOOKING_EMAIL} (ref {booking_id}). Waiting for auditorium response.",
            capacity=capacity, equipment=equipment,
        )
        return {
            "success": True,
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
            "email_provider": email_result.get("provider"),
            "email_message_id": email_result.get("message_id"),
            "provider": "EmailAuditoriumWorkflow",
        }

    def check_room_status(self, booking_id: str) -> Dict[str, Any]:
        from backend.app.db import SessionLocal, RoomBooking
        db = SessionLocal()
        try:
            booking = db.query(RoomBooking).filter(RoomBooking.notes.contains(booking_id)).first()
            if booking:
                return {**booking.to_dict(), "booking_id": booking_id}
            return {
                "booking_id": booking_id,
                "status": "NOT_FOUND",
                "message": "No auditorium request exists with this reference.",
            }
        finally:
            db.close()

# Explicit name for the live adapter; RealRoomService is kept for existing imports.
EmailAuditoriumRoomService = RealRoomService

_mock_room_instance = MockRoomService()

def get_room_service() -> RoomService:
    # The email-based request workflow is used in every mode (in DEMO_MODE it sends through the mock
    # email adapter). MockRoomService auto-confirms rooms, so it is reserved for isolated unit tests.
    return RealRoomService()
