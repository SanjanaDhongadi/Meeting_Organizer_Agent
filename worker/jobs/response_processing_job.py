import logging
from typing import Dict, Any
from worker.state.state_manager import state_manager
from worker.events.participant_events import ParticipantResponseEvent
from worker.events.room_events import RoomBookingResponseEvent

logger = logging.getLogger("response_processing_job")


def _source_label(source: str) -> str:
    return "[SIMULATED]" if (source or "").upper() == "SIMULATED" else f"[{(source or 'EXTERNAL').upper()}]"


class ResponseProcessingJob:
    """
    Processes incoming asynchronous events (participant accepts/rejects, room decisions), records them on the
    existing meeting, then resumes the SAME LangGraph workflow (process responses -> revalidate -> finalize).
    Every recorded response keeps its source, so simulated responses are never shown as real ones.
    """

    def _resume(self, meeting_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        from backend.graph.workflow_graph import WorkflowError, resume_meeting_workflow
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
        if meeting.get("status") not in {"WAITING_FOR_AUDITORIUM_RESPONSE", "WAITING_FOR_ROOM"}:
            return {"status": "ERROR", "message": "Meeting is not waiting for an auditorium response"}
        if meeting.get("room_name", "").casefold() != event.room_name.casefold():
            return {"status": "ERROR", "message": "Room does not match the pending auditorium request"}
        if event.status not in {"CONFIRMED", "REJECTED"}:
            return {"status": "ERROR", "message": f"Unsupported auditorium response '{event.status}'"}

        label = _source_label(event.source)
        state_manager.update_room_response(
            meeting_id, meeting.get("room_name") or event.room_name, event.status,
            f"{label} {event.status} by {event.approver}. {event.notes or ''}".strip(),
        )
        state_manager.update_meeting_status(
            meeting_id, meeting.get("status"), "ROOM_RESPONSE_RECEIVED", f"{label} Room {event.room_name} {event.status}",
        )
        return self._resume(meeting_id, {"type": "room", **event.model_dump()})
