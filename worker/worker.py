import time
import logging
import uuid
from typing import Dict, Any, Optional
from worker.state.state_manager import state_manager
from worker.jobs.meeting_job import MeetingJob
from worker.jobs.calendar_job import CalendarJob
from worker.jobs.email_job import EmailJob
from worker.jobs.room_booking_job import RoomBookingJob
from worker.jobs.response_processing_job import ResponseProcessingJob
from worker.events.participant_events import ParticipantResponseEvent
from worker.events.room_events import RoomBookingResponseEvent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [Worker] %(message)s")
logger = logging.getLogger("worker")

class BackgroundWorker:
    def __init__(self):
        self.state_manager = state_manager
        self.meeting_job = MeetingJob()
        self.calendar_job = CalendarJob()
        self.email_job = EmailJob()
        self.room_booking_job = RoomBookingJob()
        self.response_job = ResponseProcessingJob()
        self.running = False

    def process_cycle(self):
        """
        Single worker cycle:
        Scans for meetings in transient or background states:
        - APPROVED -> Trigger background calendar creation & invitations
        """
        from backend.app.db import SessionLocal, Meeting
        db = SessionLocal()
        try:
            approved_meetings = db.query(Meeting).filter(Meeting.status == "APPROVED").all()
            for m in approved_meetings:
                logger.info(f"Worker picked up approved meeting: {m.id}")
                self.meeting_job.execute(m.id)
        except Exception as e:
            logger.error(f"Worker cycle error: {e}")
        finally:
            db.close()

    def simulate_participant_response(self, meeting_id: str, email: str, response: str, notes: str = "") -> Dict[str, Any]:
        """
        Simulation hook for demonstration & testing:
        Simulates an asynchronous participant acceptance or rejection.
        """
        meeting = self.state_manager.load_meeting(meeting_id)
        if not meeting or meeting.get("status") != "WAITING_FOR_PARTICIPANTS":
            return {"status": "ERROR", "message": "Meeting is not waiting for participant responses"}
        if not any(p.get("email", "").lower() == email.lower() for p in meeting.get("participants", [])):
            return {"status": "ERROR", "message": "Participant email is not part of this meeting"}
        event = ParticipantResponseEvent(
            event_id=f"evt-{uuid.uuid4().hex[:8]}",
            meeting_id=meeting_id,
            email=email,
            response=response,
            notes=notes
        )
        return self.response_job.process_participant_response(event)

    def simulate_room_approval(self, meeting_id: str, room_name: str, confirmed: bool, notes: str = "") -> Dict[str, Any]:
        """
        Simulation hook for demonstration & testing:
        Simulates facility manager approving or rejecting auditorium/room reservation.
        """
        meeting = self.state_manager.load_meeting(meeting_id)
        if not meeting or meeting.get("status") not in {"WAITING_FOR_AUDITORIUM_RESPONSE", "WAITING_FOR_ROOM"}:
            return {"status": "ERROR", "message": "Meeting is not waiting for an auditorium response"}
        from backend.app.config import settings as _settings
        event = RoomBookingResponseEvent(
            event_id=f"evt-{uuid.uuid4().hex[:8]}",
            meeting_id=meeting_id,
            room_name=room_name,
            status="CONFIRMED" if confirmed else "REJECTED",
            approver=_settings.AUDITORIUM_BOOKING_EMAIL,
            notes=notes
        )
        return self.response_job.process_room_response(event)

    def start(self, interval_seconds: int = 5):
        self.running = True
        logger.info(f"AI Meeting Organizer Worker started (polling interval: {interval_seconds}s)")
        try:
            while self.running:
                self.process_cycle()
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            logger.info("Worker stopped by user.")
            self.running = False

background_worker = BackgroundWorker()

if __name__ == "__main__":
    background_worker.start()
