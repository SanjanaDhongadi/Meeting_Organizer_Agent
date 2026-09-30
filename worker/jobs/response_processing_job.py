import logging
from typing import Dict, Any
from worker.state.state_manager import state_manager
from worker.events.participant_events import ParticipantResponseEvent
from worker.events.room_events import RoomBookingResponseEvent

logger = logging.getLogger("response_processing_job")

class ResponseProcessingJob:
    """
    Processes incoming asynchronous events (participant accepts/rejects, room decisions)
    and resumes the SAME workflow on the existing meeting without duplication.
    """

    def process_participant_response(self, event: ParticipantResponseEvent) -> Dict[str, Any]:
        if state_manager.is_event_processed(event.event_id):
            logger.info(f"Duplicate event {event.event_id} ignored.")
            return {"status": "SKIPPED_DUPLICATE"}

        state_manager.mark_event_processed(event.event_id)
        meeting_id = event.meeting_id

        meeting = state_manager.load_meeting(meeting_id)
        if not meeting:
            return {"status": "ERROR", "message": f"Meeting {meeting_id} not found"}
        if meeting.get("status") != "WAITING_FOR_PARTICIPANTS":
            return {"status": "ERROR", "message": "Meeting is not waiting for participant responses"}
        if not any(p.get("email", "").lower() == event.email.lower() for p in meeting.get("participants", [])):
            return {"status": "ERROR", "message": "Participant email is not part of this meeting"}
        if not state_manager.update_participant_response(meeting_id, event.email, event.response, event.notes or ""):
            return {"status": "ERROR", "message": "Participant response could not be recorded"}

        meeting = state_manager.load_meeting(meeting_id)
        participants = meeting.get("participants", [])

        # Check if anyone rejected
        has_rejection = any(p.get("response_status") == "REJECTED" for p in participants)
        if has_rejection:
            state_manager.update_meeting_status(
                meeting_id,
                "RESCHEDULING_REQUIRED",
                "PARTICIPANT_REJECTED",
                f"Participant {event.email} rejected meeting invitation."
            )
            return {"status": "RESCHEDULING_REQUIRED", "meeting_id": meeting_id}

        # Check if all participants accepted
        all_accepted = all(p.get("response_status") == "ACCEPTED" for p in participants)
        
        # Check if room is waiting
        room_bookings = meeting.get("room_bookings", [])
        is_room_pending = any(rb.get("status") == "PENDING" for rb in room_bookings)

        if all_accepted:
            if is_room_pending:
                state_manager.update_meeting_status(
                    meeting_id,
                    "WAITING_FOR_AUDITORIUM_RESPONSE",
                    "PARTICIPANTS_CONFIRMED",
                    "All participants accepted. Still awaiting facility room confirmation."
                )
                return {"status": "WAITING_FOR_AUDITORIUM_RESPONSE", "meeting_id": meeting_id}
            else:
                state_manager.update_meeting_status(
                    meeting_id,
                    "CONFIRMED",
                    "ALL_PARTICIPANTS_ACCEPTED",
                    "All participants confirmed. Meeting is confirmed."
                )
                return {"status": "CONFIRMED", "meeting_id": meeting_id}

        return {"status": "WAITING_FOR_PARTICIPANTS", "meeting_id": meeting_id}

    def process_room_response(self, event: RoomBookingResponseEvent) -> Dict[str, Any]:
        if state_manager.is_event_processed(event.event_id):
            return {"status": "SKIPPED_DUPLICATE"}

        state_manager.mark_event_processed(event.event_id)
        meeting_id = event.meeting_id

        meeting = state_manager.load_meeting(meeting_id)
        if not meeting:
            return {"status": "ERROR", "message": f"Meeting {meeting_id} not found"}
        if meeting.get("status") not in {"WAITING_FOR_AUDITORIUM_RESPONSE", "WAITING_FOR_ROOM"}:
            return {"status": "ERROR", "message": "Meeting is not waiting for an auditorium response"}
        if meeting.get("room_name", "").casefold() != event.room_name.casefold():
            return {"status": "ERROR", "message": "Room does not match the pending auditorium request"}

        state_manager.update_room_response(meeting_id, event.room_name, event.status, event.notes or "")

        if event.status == "CONFIRMED":
            if meeting and meeting.get("mode") == "OFFLINE":
                state_manager.update_meeting_status(
                    meeting_id,
                    "CONFIRMED",
                    "ROOM_CONFIRMED",
                    f"Room {event.room_name} confirmed by {event.approver}.",
                )
                return {"status": "CONFIRMED", "meeting_id": meeting_id}

            participants = meeting.get("participants", []) if meeting else []
            has_pending_participants = any(p.get("response_status") == "PENDING" for p in participants)

            if has_pending_participants:
                state_manager.update_meeting_status(
                    meeting_id,
                    "WAITING_FOR_PARTICIPANTS",
                    "ROOM_CONFIRMED",
                    f"Room {event.room_name} confirmed by {event.approver}. Awaiting participant acceptances."
                )
                return {"status": "WAITING_FOR_PARTICIPANTS", "meeting_id": meeting_id}
            else:
                state_manager.update_meeting_status(
                    meeting_id,
                    "CONFIRMED",
                    "ROOM_CONFIRMED",
                    f"Room {event.room_name} confirmed by {event.approver}. Meeting is confirmed."
                )
                return {"status": "CONFIRMED", "meeting_id": meeting_id}

        elif event.status == "REJECTED":
            state_manager.update_meeting_status(
                meeting_id,
                "RESCHEDULING_REQUIRED",
                "ROOM_REJECTED",
                f"Room booking for {event.room_name} was rejected by {event.approver}. Reason: {event.notes}"
            )
            return {"status": "RESCHEDULING_REQUIRED", "meeting_id": meeting_id}

        return {"status": "UPDATED", "meeting_id": meeting_id}
