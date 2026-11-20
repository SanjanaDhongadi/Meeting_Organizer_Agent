import logging
from typing import Dict, Any
from worker.state.state_manager import state_manager
from worker.events.participant_events import ParticipantResponseEvent
from worker.events.room_events import RoomBookingResponseEvent

logger = logging.getLogger("response_processing_job")

# A venue request can be answered before approval (draft) or while attendees are still responding.
PRE_APPROVAL_STATUSES = {"WAITING_FOR_HUMAN_APPROVAL", "DRAFT", "NEEDS_INFORMATION", "ACTION_FAILED"}
VENUE_RESPONSE_STATUSES = PRE_APPROVAL_STATUSES | {"WAITING_FOR_PARTICIPANTS", "WAITING_FOR_AUDITORIUM_RESPONSE", "WAITING_FOR_ROOM"}


def _source_label(source: str) -> str:
    return "[SIMULATED]" if (source or "").upper() == "SIMULATED" else f"[{(source or 'EXTERNAL').upper()}]"


class ResponseProcessingJob:
    """
    Processes incoming asynchronous events (participant accepts/rejects, room decisions), records them on the
    existing meeting, then resumes the SAME LangGraph workflow (process responses -> revalidate -> finalize).
    Every recorded response keeps its source, so simulated responses are never shown as real ones.
    """

    def _resume(self, meeting_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        from graph.workflow_graph import WorkflowError, resume_meeting_workflow
        try:
            final_state = resume_meeting_workflow(meeting_id, "RESPONSE", response_event=event)
        except WorkflowError as error:
            return {"status": "ERROR", "meeting_id": meeting_id, "message": str(error), "node": error.node}
        latest = state_manager.load_meeting(meeting_id) or {}
        result = {"status": latest.get("status") or final_state.get("status"), "meeting_id": meeting_id, "source": event.get("source")}
        if final_state.get("execution_error"):
            result["error"] = final_state["execution_error"].get("error")
            result["error_data"] = final_state["execution_error"]
        if final_state.get("warnings"):
            result["warnings"] = final_state["warnings"]
        return result

    def process_participant_response(self, event: ParticipantResponseEvent) -> Dict[str, Any]:
        if state_manager.is_event_processed(event.event_id):
            logger.info(f"Duplicate event {event.event_id} ignored.")
            return {"status": "SKIPPED_DUPLICATE"}

        state_manager.mark_event_processed(event.event_id, event.meeting_id, event.source)
        meeting_id = event.meeting_id

        meeting = state_manager.load_meeting(meeting_id)
        if not meeting:
            return {"status": "ERROR", "message": f"Meeting {meeting_id} not found"}
        if meeting.get("status") != "WAITING_FOR_PARTICIPANTS":
            return {"status": "ERROR", "message": "Meeting is not waiting for participant responses"}
        if not any(p.get("email", "").lower() == event.email.lower() for p in meeting.get("participants", [])):
            return {"status": "ERROR", "message": "Participant email is not part of this meeting"}
        notes = f"{_source_label(event.source)} {event.notes or ''}".strip()
        if not state_manager.update_participant_response(meeting_id, event.email, event.response, notes):
            return {"status": "ERROR", "message": "Participant response could not be recorded"}
        state_manager.update_meeting_status(
            meeting_id, "WAITING_FOR_PARTICIPANTS", "PARTICIPANT_RESPONSE_RECEIVED",
            f"{_source_label(event.source)} {event.email} {event.response}",
        )
        return self._resume(meeting_id, {"type": "participant", **event.model_dump()})

    def process_room_response(self, event: RoomBookingResponseEvent) -> Dict[str, Any]:
        if state_manager.is_event_processed(event.event_id):
            return {"status": "SKIPPED_DUPLICATE"}

        state_manager.mark_event_processed(event.event_id, event.meeting_id, event.source)
        meeting_id = event.meeting_id

        meeting = state_manager.load_meeting(meeting_id)
        if not meeting:
            return {"status": "ERROR", "message": f"Meeting {meeting_id} not found"}
        if meeting.get("status") not in VENUE_RESPONSE_STATUSES:
            return {"status": "ERROR", "message": f"Meeting is not waiting for a venue response (status: {meeting.get('status')})"}
        from agents.venue_coordinator import active_request
        request = active_request(meeting.get("room_bookings") or [])
        if not request or request.get("status") != "PENDING":
            return {"status": "ERROR", "message": "No venue request is waiting for a response for this meeting"}
        if meeting.get("room_name", "").casefold() != event.room_name.casefold():
            return {"status": "ERROR", "message": "Venue does not match the pending venue request"}
        if event.status not in {"CONFIRMED", "REJECTED"}:
            return {"status": "ERROR", "message": f"Unsupported venue response '{event.status}'"}

        label = _source_label(event.source)
        state_manager.update_room_response(
            meeting_id, request.get("room_name") or event.room_name, event.status,
            f"{label} {event.status} by {event.approver}. {event.notes or ''}".strip(),
        )
        status = meeting.get("status")
        if status in PRE_APPROVAL_STATUSES:
            # Not sent to attendees yet: a declined venue blocks approval until the time or venue is changed.
            if event.status == "REJECTED":
                state_manager.update_meeting_status(
                    meeting_id, "RESCHEDULING_REQUIRED", "VENUE_DECLINED",
                    f"{label} Venue declined by the venue coordinator. Edit the time or venue, then send a new venue request.")
            else:
                state_manager.update_meeting_status(meeting_id, status, "VENUE_CONFIRMED", f"{label} Venue {event.room_name} confirmed")
            return {"status": (state_manager.load_meeting(meeting_id) or {}).get("status"), "meeting_id": meeting_id,
                    "source": event.source}
        state_manager.update_meeting_status(
            meeting_id, status, "ROOM_RESPONSE_RECEIVED", f"{label} Venue {event.room_name} {event.status}",
        )
        return self._resume(meeting_id, {"type": "room", **event.model_dump()})
