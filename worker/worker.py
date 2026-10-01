import time
import hashlib
import logging
import re
import uuid
from typing import Dict, Any, List, Optional
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
        - APPROVED -> resume the workflow at the approval gate (calendar event / invitations / room request)
        - WAITING_* -> pick up real external responses (Google Calendar attendee status, Gmail auditorium replies)
        """
        from backend.app.db import SessionLocal, Meeting
        db = SessionLocal()
        try:
            approved_ids = [m.id for m in db.query(Meeting).filter(Meeting.status == "APPROVED").all()]
        except Exception as e:
            logger.error(f"Worker cycle error: {e}")
            approved_ids = []
        finally:
            db.close()
        for meeting_id in approved_ids:
            logger.info(f"Worker picked up approved meeting: {meeting_id}")
            self.meeting_job.execute(meeting_id)
        try:
            self.sync_external_responses()
        except Exception as e:
            logger.error(f"External response sync failed: {e}")

    # ----------------- Real external responses (not simulations) -----------------

    @staticmethod
    def _event_id(*parts: str) -> str:
        return "ext-" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:40]

    @staticmethod
    def classify_auditorium_reply(text: str) -> Optional[str]:
        lowered = (text or "").lower()
        rejected = re.search(r"\b(reject(ed)?|declin(e|ed)|unavailable|not available|cannot|can't)\b", lowered)
        confirmed = re.search(r"\b(confirm(ed)?|approved?|booked|accepted)\b", lowered)
        if rejected and not confirmed:
            return "REJECTED"
        if confirmed and not rejected:
            return "CONFIRMED"
        return None  # ambiguous replies are left for a human

    def sync_external_responses(self, meeting_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Reads REAL responses through MCP: attendee responseStatus from the Google Calendar event (calendar.get_event)
        and auditorium replies from Gmail (gmail.read). Each becomes an event with a non-simulated source.
        """
        from backend.app.config import settings
        from backend.app.db import SessionLocal, Meeting
        from backend.connectors.mcp_gateway import mcp_call

        if settings.DEMO_MODE:
            return [{"status": "SKIPPED", "message": "DEMO_MODE uses simulated responses only."}]
        db = SessionLocal()
        try:
            query = db.query(Meeting).filter(Meeting.status.in_(["WAITING_FOR_PARTICIPANTS", "WAITING_FOR_AUDITORIUM_RESPONSE"]))
            if meeting_id:
                query = query.filter(Meeting.id == meeting_id)
            meeting_ids = [m.id for m in query.all()]
        finally:
            db.close()

        results: List[Dict[str, Any]] = []
        for mid in meeting_ids:
            meeting = self.state_manager.load_meeting(mid)
            if not meeting:
                continue
            organizer = ((meeting.get("parsed_details") or {}).get("workflow") or {}).get("organizer_email")
            if meeting["status"] == "WAITING_FOR_PARTICIPANTS" and meeting.get("calendar_event_id"):
                res = mcp_call("calendar.get_event", {"event_id": meeting["calendar_event_id"], "organizer_email": organizer})
                if not res.get("success"):
                    results.append({"meeting_id": mid, "status": "ERROR", "capability": "calendar.get_event", "error": res.get("error")})
                    continue
                pending = {p["email"].lower() for p in meeting.get("participants", []) if p.get("response_status") == "PENDING"}
                for attendee in res.get("attendees", []):
                    mapped = {"accepted": "ACCEPTED", "declined": "REJECTED"}.get(attendee.get("response_status"))
                    if not mapped or attendee["email"] not in pending:
                        continue
                    event = ParticipantResponseEvent(
                        event_id=self._event_id(mid, meeting["calendar_event_id"], attendee["email"], mapped),
                        meeting_id=mid, email=attendee["email"], response=mapped,
                        notes="Response read from the Google Calendar invitation.", source="GOOGLE_CALENDAR",
                    )
                    results.append(self.response_job.process_participant_response(event))
                    if (self.state_manager.load_meeting(mid) or {}).get("status") != "WAITING_FOR_PARTICIPANTS":
                        break
            elif meeting["status"] == "WAITING_FOR_AUDITORIUM_RESPONSE":
                res = mcp_call("gmail.read", {
                    "query": f'from:{settings.AUDITORIUM_BOOKING_EMAIL} subject:"{mid}" newer_than:30d',
                    "account_email": organizer,
                })
                if not res.get("success"):
                    results.append({"meeting_id": mid, "status": "ERROR", "capability": "gmail.read", "error": res.get("error")})
                    continue
                for message in res.get("messages", []):
                    decision = self.classify_auditorium_reply(f"{message.get('subject', '')} {message.get('snippet', '')}")
                    if not decision:
                        results.append({"meeting_id": mid, "status": "NEEDS_HUMAN_REVIEW", "message_id": message.get("id"),
                                        "snippet": message.get("snippet")})
                        continue
                    event = RoomBookingResponseEvent(
                        event_id=self._event_id(mid, "gmail", message.get("id", "")),
                        meeting_id=mid, room_name=meeting.get("room_name", ""), status=decision,
                        approver=message.get("from") or settings.AUDITORIUM_BOOKING_EMAIL,
                        notes=f"Reply read from Gmail: {message.get('snippet', '')[:200]}", source="GMAIL_REPLY",
                    )
                    results.append(self.response_job.process_room_response(event))
                    break
        return results

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
            notes=notes,
            source="SIMULATED",
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
            notes=notes,
            source="SIMULATED",
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
