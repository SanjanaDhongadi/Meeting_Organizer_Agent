import logging
from typing import Dict, Any, Optional
from worker.state.state_manager import state_manager
from backend.tools.registry import tool_registry
from backend.app.config import settings
from backend.connectors.email_service import get_email_service

logger = logging.getLogger("meeting_job")

class MeetingJob:
    """
    Executes background meeting progression and state checks.
    """
    def execute(self, meeting_id: str) -> Dict[str, Any]:
        meeting = state_manager.load_meeting(meeting_id)
        if not meeting:
            return {"success": False, "error": f"Meeting {meeting_id} not found"}

        current_status = meeting.get("status")
        logger.info(f"[MeetingJob] Evaluating meeting {meeting_id} in status: {current_status}")

        if current_status == "APPROVED":
            mode = meeting.get("mode", "ONLINE")
            participants = meeting.get("participants", [])
            attendees = [p["email"] for p in participants]

            if mode == "OFFLINE":
                room_name = meeting.get("room_name") or "Auditorium"
                equipment = meeting.get("parsed_details", {}).get("equipment") or "Not specified"
                participant_count = len(participants)
                body = "\n".join([
                    f"Date and time: {meeting.get('scheduled_start', 'TBD')}",
                    f"Duration: {meeting.get('duration_minutes', 30)} minutes",
                    f"Number of participants: {participant_count}",
                    f"Required equipment: {equipment}",
                    f"Meeting purpose: {meeting.get('purpose', '')}",
                    f"Room requested: {room_name}",
                    f"Meeting ID: {meeting_id}",
                ])
                email_result = get_email_service().send_invitation(
                    [settings.AUDITORIUM_BOOKING_EMAIL],
                    "Auditorium Booking Request",
                    body,
                    meeting_id,
                )
                if not email_result.get("success"):
                    state_manager.update_meeting_status(
                        meeting_id, "ACTION_FAILED", "ROOM_REQUEST_FAILED",
                        email_result.get("error", "Room booking email could not be sent."),
                    )
                    return {"success": False, "error": email_result.get("error", "Room booking email could not be sent.")}
                state_manager.update_room_response(
                    meeting_id, room_name, "PENDING", "Awaiting auditorium response."
                )
                state_manager.update_meeting_status(
                    meeting_id,
                    "WAITING_FOR_AUDITORIUM_RESPONSE",
                    "ROOM_BOOKING_REQUEST_SENT",
                    f"Room booking request sent to {settings.AUDITORIUM_BOOKING_EMAIL}.",
                )
                return {"success": True, "new_status": "WAITING_FOR_AUDITORIUM_RESPONSE"}

            cal_tool = tool_registry.get("create_calendar_event")
            cal_res = cal_tool.execute(
                title=meeting.get("title", "Project Meeting"),
                start_time=meeting.get("scheduled_start", ""),
                end_time=meeting.get("scheduled_end", ""),
                attendees=attendees,
                description=meeting.get("agenda", ""),
                is_online=True
            )
            if not cal_res.success:
                # Surface the complete Google Calendar error — never hide it
                error_detail = cal_res.error or "Google Calendar event could not be created."
                if cal_res.data:
                    http_status = cal_res.data.get("http_status")
                    err_reason = cal_res.data.get("error_reason")
                    err_msg = cal_res.data.get("error_message")
                    err_code = cal_res.data.get("error_code")
                    if http_status:
                        error_detail = f"[{err_code}] HTTP {http_status}: {err_msg or error_detail}" + (f" (reason: {err_reason})" if err_reason else "")
                    elif err_code:
                        error_detail = f"[{err_code}] {error_detail}"
                logger.error("[MeetingJob] Calendar event creation failed for %s: %s", meeting_id, error_detail)
                state_manager.update_meeting_status(meeting_id, "ACTION_FAILED", "CALENDAR_EVENT_FAILED", error_detail)
                return {"success": False, "error": error_detail, "error_data": cal_res.data}


            # Persist event details
            save_tool = tool_registry.get("save_meeting_state")
            save_tool.execute(
                meeting_id=meeting_id,
                calendar_event_id=cal_res.data.get("event_id"),
                meet_url=cal_res.data.get("meet_url")
            )

            state_manager.update_meeting_status(
                meeting_id,
                "WAITING_FOR_PARTICIPANTS",
                "CALENDAR_INVITATIONS_SENT",
                "Calendar invitation sent. Awaiting participant responses.",
            )
            return {"success": True, "new_status": "WAITING_FOR_PARTICIPANTS"}

        return {"success": True, "status": current_status}
