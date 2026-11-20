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
from worker.jobs.minutes_job import MinutesJob
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
        self.minutes_job = MinutesJob()
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
            self.meeting_job.recover_stuck()   # crashed approved executions: resume from their checkpoints
        except Exception as e:
            logger.error(f"Stuck-execution recovery failed: {e}")
        try:
            self.sync_external_responses()
        except Exception as e:
            logger.error(f"External response sync failed: {e}")
        try:
            self.minutes_job.scan()   # ended online meetings: transcript retrieval, MoM drafts
        except Exception as e:
            logger.error(f"Minutes scan failed: {e}")

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
        and the venue coordinator's reply to a venue request from Gmail (gmail.read). Each becomes an event with a
        non-simulated source. What Google reported is stored so the UI can show it.
        """
        from app.config import settings
        from backend.app.db import SessionLocal, Meeting
        from backend.connectors.mcp_gateway import mcp_call
        from backend.agents.venue_coordinator import active_request, valid_coordinator_email
        from worker.jobs.response_processing_job import VENUE_RESPONSE_STATUSES

        if settings.DEMO_MODE:
            return [{"status": "SKIPPED", "message": "DEMO_MODE uses simulated responses only."}]
        db = SessionLocal()
        try:
            query = db.query(Meeting).filter(Meeting.status.in_(sorted(VENUE_RESPONSE_STATUSES)))
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
            organizer = (((meeting.get("parsed_details") or {}).get("workflow") or {}).get("organizer_email") or "").lower()
            if meeting["status"] == "WAITING_FOR_PARTICIPANTS" and meeting.get("calendar_event_id"):
                res = mcp_call("calendar.get_event", {"event_id": meeting["calendar_event_id"], "organizer_email": organizer or None})
                if not res.get("success"):
                    results.append({"meeting_id": mid, "status": "ERROR", "capability": "calendar.get_event", "error": res.get("error")})
                    continue
                self.state_manager.record_google_sync(mid, res.get("attendees", []), res.get("status"))
                pending = {p["email"].lower() for p in meeting.get("participants", []) if p.get("response_status") == "PENDING"}
                for attendee in res.get("attendees", []):
                    if attendee["email"] == organizer:
                        # Google never asks the organizer to RSVP: the organizer attends their own event.
                        mapped, note = "ACCEPTED", "Organizer of the Google Calendar event (organizers are not asked to respond)."
                    else:
                        mapped = {"accepted": "ACCEPTED", "declined": "REJECTED"}.get(attendee.get("response_status"))
                        note = "Response read from the Google Calendar invitation."
                    if not mapped or attendee["email"] not in pending:
                        continue
                    event = ParticipantResponseEvent(
                        event_id=self._event_id(mid, meeting["calendar_event_id"], attendee["email"], mapped),
                        meeting_id=mid, email=attendee["email"], response=mapped, notes=note, source="GOOGLE_CALENDAR",
                    )
                    results.append(self.response_job.process_participant_response(event))
                    if (self.state_manager.load_meeting(mid) or {}).get("status") != "WAITING_FOR_PARTICIPANTS":
                        break
                meeting = self.state_manager.load_meeting(mid) or meeting

            # In person: the venue coordinator's reply to a venue request that is still waiting for an answer.
            request = active_request(meeting.get("room_bookings") or []) if (meeting.get("mode") or "").upper() == "OFFLINE" else None
            if request and request.get("status") == "PENDING" and meeting.get("status") in VENUE_RESPONSE_STATUSES:
                coordinator_email = ((meeting.get("parsed_details") or {}).get("venue_coordinator_email") or "").strip()
                if not valid_coordinator_email(coordinator_email):
                    results.append({"meeting_id": mid, "status": "ERROR", "capability": "gmail.read",
                                    "error": "This meeting has no valid venue coordinator email, so venue replies cannot be matched."})
                    continue
                res = mcp_call("gmail.read", {
                    "query": f'from:{coordinator_email} subject:"{mid}" newer_than:30d',
                    "account_email": organizer or self._venue_mailbox(meeting),
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
                        event_id=self._event_id(mid, "gmail", str(request.get("id")), message.get("id", "")),
                        meeting_id=mid, room_name=request.get("room_name") or meeting.get("room_name", ""), status=decision,
                        approver=message.get("from") or coordinator_email,
                        notes=f"Reply read from Gmail: {message.get('snippet', '')[:200]}", source="GMAIL_REPLY",
                    )
                    results.append(self.response_job.process_room_response(event))
                    break
        return results

    @staticmethod
    def _venue_mailbox(meeting: Dict[str, Any]) -> Optional[str]:
        """The organizer account that sent the venue request (same selection rule as when it was sent)."""
        from backend.connectors.google_auth import resolve_organizer_email
        return resolve_organizer_email([p["email"] for p in meeting.get("participants", [])])

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
        Simulates the venue coordinator confirming or rejecting a venue request (DEMO_MODE only).
        """
        # The job validates that a venue request is actually waiting for an answer.
        meeting = self.state_manager.load_meeting(meeting_id) or {}
        approver = ((meeting.get("parsed_details") or {}).get("venue_coordinator_email") or "").strip()
        event = RoomBookingResponseEvent(
            event_id=f"evt-{uuid.uuid4().hex[:8]}",
            meeting_id=meeting_id,
            room_name=room_name,
            status="CONFIRMED" if confirmed else "REJECTED",
            approver=approver,
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
