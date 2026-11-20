from typing import Dict, Any, Optional
import logging
from pydantic import BaseModel, Field
from connectors.calendar_service import get_calendar_service
from connectors.email_service import get_email_service
from connectors.room_service import get_room_service
from connectors.meet_service import get_meet_service

logger = logging.getLogger("mcp_gateway")

class MCPResponse(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[str] = None
    result: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None

class MCPGateway:
    """
    Lab 5: MCP-Style Connector Layer
    Provides structured, permission-validated access to enterprise calendar,
    email, and physical room booking capabilities with JSON-RPC schemas.
    """

    CAPABILITIES = {
        "calendar.check_availability": {
            "description": "Check participant availability in specified time window",
            "required_params": ["emails", "start_time", "end_time"]
        },
        "calendar.create_event": {
            "description": "Create a calendar event for approved meeting",
            "required_params": ["title", "start_time", "end_time", "attendees"]
        },
        "calendar.create_meet": {
            "description": "Generate Google Meet conference link for online meeting",
            "required_params": ["title", "start_time", "end_time"]
        },
        "gmail.send": {
            "description": "Send meeting invitations or notifications via email",
            "required_params": ["to_emails", "subject", "body"]
        },
        "gmail.read": {
            "description": "Read response messages from participant inbox",
            "required_params": []
        },
        "room.request": {
            "description": "Request physical conference room or auditorium reservation",
            "required_params": ["room_name", "start_time", "end_time", "capacity"]
        },
        "room.status": {
            "description": "Query status of an existing room reservation",
            "required_params": ["booking_id"]
        },
        "calendar.find_common_slots": {
            "description": "Find alternative slots where all verifiable participant calendars are free",
            "required_params": ["emails", "date_str"]
        },
        "calendar.get_event": {
            "description": "Read a calendar event, including attendee response statuses",
            "required_params": ["event_id"]
        },
        "calendar.cancel_event": {
            "description": "Cancel a meeting's calendar event and notify attendees",
            "required_params": ["event_id"]
        },
        "meet.get_transcript": {
            "description": "Internal: read the Google Meet transcript of an ended meeting (not callable from /api/mcp)",
            "required_params": ["meeting_code"]
        }
    }

    # Capabilities with external side effects: only allowed for an approved meeting when called from outside.
    CONSEQUENTIAL = {"calendar.create_event", "calendar.create_meet", "gmail.send", "room.request", "calendar.cancel_event"}

    # Never callable by outside (HTTP) callers: transcripts are private meeting content.
    INTERNAL_ONLY = {"meet.get_transcript"}

    def __init__(self):
        pass

    @property
    def meet_service(self):
        return get_meet_service()

    # Adapters are resolved per call so stored OAuth credentials are always current.
    @property
    def calendar_service(self):
        return get_calendar_service()

    @property
    def email_service(self):
        return get_email_service()

    @property
    def room_service(self):
        return get_room_service()

    # Keys that name the Google account an operation acts as, and the key each capability reads it from.
    IDENTITY_KEYS = ("account_email", "sender_email", "organizer_email")
    IDENTITY_KEY_FOR = {
        "gmail.read": "account_email", "gmail.send": "sender_email", "room.request": "sender_email",
        "calendar.create_event": "organizer_email", "calendar.create_meet": "organizer_email",
        "calendar.cancel_event": "organizer_email", "calendar.get_event": "organizer_email",
    }

    @staticmethod
    def _denied(req_id: Optional[str], message: str) -> "MCPResponse":
        return MCPResponse(id=req_id, error={"code": -32003, "message": f"Permission denied: {message}"})

    def _authorize_external(self, method: str, params: Dict[str, Any], ctx: Dict[str, Any]):
        """Caller-scoped authorization for HTTP callers. Returns (params, denial); denial is None if allowed."""
        from app.db import SessionLocal, Meeting, MeetingParticipant
        params = dict(params)
        me = (ctx.get("user_email") or "").strip().lower()

        # a. Identity: act only as the caller's own Google account; never let the connector pick another one.
        for key in self.IDENTITY_KEYS:
            if params.get(key) and str(params[key]).strip().lower() != me:
                return params, "you may only act as your own Google account."
        key = self.IDENTITY_KEY_FOR.get(method)
        if key:
            params[key] = me

        meeting_id = params.get("meeting_id")
        db = SessionLocal()
        try:
            meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first() if meeting_id else None
            owns = bool(meeting and meeting.status != "DELETED" and meeting.organizer_id == ctx.get("user_id"))
            # b. Consequential capabilities: an existing, APPROVED meeting organized by the caller.
            if method in self.CONSEQUENTIAL:
                if not (owns and meeting.approval_status == "APPROVED"):
                    return params, f"'{method}' requires the meeting_id of a human-APPROVED meeting that you organize."
                if method == "calendar.cancel_event" and (
                        not meeting.calendar_event_id or params.get("event_id") != meeting.calendar_event_id):
                    return params, "that event does not belong to this meeting."
            # d. Availability: the caller's own calendar, or participants of a meeting the caller organizes.
            if method in ("calendar.check_availability", "calendar.find_common_slots"):
                allowed = {me}
                if owns:
                    allowed |= {(p.email or "").lower() for p in
                                db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting.id).all()}
                if any(str(e).strip().lower() not in allowed for e in params.get("emails") or []):
                    return params, "you may only check your own calendar or the participants of a meeting you organize."
        finally:
            db.close()
        return params, None

    def list_capabilities(self) -> Dict[str, Any]:
        return {
            "protocol_version": "2024-11-05",
            "server_info": {"name": "AI Meeting Organizer MCP Gateway", "version": "1.0.0"},
            "capabilities": self.CAPABILITIES
        }

    def execute(self, method: str, params: Dict[str, Any], req_id: Optional[str] = None, auth_context: Optional[Dict[str, Any]] = None) -> MCPResponse:
        response = self._execute(method, params, req_id, auth_context)
        if auth_context and auth_context.get("external"):
            self._audit_external(method, params, auth_context, response)
        return response

    @staticmethod
    def _audit_external(method: str, params: Dict[str, Any], ctx: Dict[str, Any], response: "MCPResponse") -> None:
        """Record who called what. Only the method, meeting id and user id are stored - never bodies, tokens or params."""
        result = response.result if isinstance(response.result, dict) else {}
        failed = bool(response.error) or result.get("success") is False
        try:
            from memory.audit_memory import audit_memory
            audit_memory.record_event("MCP Gateway", "EXTERNAL_MCP_CALL", meeting_id=(params or {}).get("meeting_id"),
                                      status="ERROR" if failed else "SUCCESS",
                                      details={"method": method, "user_id": ctx.get("user_id")})
        except Exception:
            logger.exception("Could not record the external MCP call in the audit log")

    def _execute(self, method: str, params: Dict[str, Any], req_id: Optional[str] = None, auth_context: Optional[Dict[str, Any]] = None) -> MCPResponse:
        logger.info(f"[MCP] Executing method: {method} with params: {params}")

        # Check if method exists
        if method not in self.CAPABILITIES:
            return MCPResponse(
                id=req_id,
                error={
                    "code": -32601,
                    "message": f"Method '{method}' not found in MCP capability registry",
                    "data": {"available_methods": list(self.CAPABILITIES.keys())}
                }
            )

        # Validate required parameters
        schema = self.CAPABILITIES[method]
        missing = [p for p in schema["required_params"] if p not in params or params[p] is None]
        if missing:
            return MCPResponse(
                id=req_id,
                error={
                    "code": -32602,
                    "message": f"Invalid params for '{method}': missing required fields: {missing}",
                    "data": {"missing_fields": missing}
                }
            )

        # Permission check (can be expanded for RBAC)
        if auth_context and auth_context.get("restricted") and method.startswith("calendar.create"):
            return MCPResponse(
                id=req_id,
                error={
                    "code": -32003,
                    "message": "Permission denied: caller lacks meeting creation authority",
                }
            )
        if auth_context and auth_context.get("external") and method in self.INTERNAL_ONLY:
            return MCPResponse(
                id=req_id,
                error={"code": -32003, "message": f"Permission denied: '{method}' is internal only."}
            )
        # Callers outside the workflow (the authenticated /api/mcp endpoint) are scoped to their own account/meetings.
        if auth_context and auth_context.get("external"):
            params, denial = self._authorize_external(method, params, auth_context)
            if denial:
                return self._denied(req_id, denial)

        try:
            if method == "calendar.check_availability":
                res = self.calendar_service.check_availability(
                    emails=params["emails"],
                    start_time=params["start_time"],
                    end_time=params["end_time"]
                )
                return MCPResponse(id=req_id, result=res)

            elif method == "calendar.find_common_slots":
                slots = self.calendar_service.find_common_slots(
                    params["emails"], params["date_str"], params.get("duration_minutes", 30)
                )
                return MCPResponse(id=req_id, result={"common_slots": slots, "count": len(slots)})

            elif method == "calendar.cancel_event":
                res = self.calendar_service.cancel_event(params["event_id"], organizer_email=params.get("organizer_email"))
                return MCPResponse(id=req_id, result=res)

            elif method == "calendar.get_event":
                res = self.calendar_service.get_event(params["event_id"], organizer_email=params.get("organizer_email"))
                return MCPResponse(id=req_id, result=res)

            elif method == "calendar.create_event":
                res = self.calendar_service.create_event(
                    title=params["title"],
                    start_time=params["start_time"],
                    end_time=params["end_time"],
                    attendees=params["attendees"],
                    description=params.get("description", ""),
                    is_online=params.get("is_online", True),
                    organizer_email=params.get("organizer_email"),
                    location=params.get("location", ""),
                    event_id=params.get("event_id") or None,
                    send_updates=params.get("send_updates", "all"),
                )
                return MCPResponse(id=req_id, result=res)

            elif method == "calendar.create_meet":
                res = self.calendar_service.create_event(
                    title=params["title"],
                    start_time=params["start_time"],
                    end_time=params["end_time"],
                    attendees=params.get("attendees", []),
                    description=params.get("description", "Google Meet Session"),
                    is_online=True,
                    organizer_email=params.get("organizer_email"),
                )
                return MCPResponse(id=req_id, result=res)

            elif method == "gmail.send":
                res = self.email_service.send_invitation(
                    to_emails=params["to_emails"],
                    subject=params["subject"],
                    body=params["body"],
                    meeting_id=params.get("meeting_id", "mcp-req"),
                    sender_email=params.get("sender_email"),
                )
                return MCPResponse(id=req_id, result=res)

            elif method == "gmail.read":
                res = self.email_service.get_messages(query=params.get("query", ""), account_email=params.get("account_email"))
                return MCPResponse(id=req_id, result={"messages": res})

            elif method == "room.request":
                res = self.room_service.request_booking(
                    room_name=params["room_name"],
                    start_time=params["start_time"],
                    end_time=params["end_time"],
                    capacity=params.get("capacity", 10),
                    equipment=params.get("equipment", ""),
                    meeting_id=params.get("meeting_id", "mcp-room-req"),
                    duration_minutes=params.get("duration_minutes"),
                    participant_count=params.get("participant_count"),
                    purpose=params.get("purpose", ""),
                    coordinator_email=params.get("coordinator_email"),
                    sender_email=params.get("sender_email"),
                )
                return MCPResponse(id=req_id, result=res)

            elif method == "meet.get_transcript":
                res = self.meet_service.get_transcript(
                    meeting_code=params["meeting_code"],
                    organizer_email=params.get("organizer_email"),
                    not_before=params.get("not_before"),
                )
                return MCPResponse(id=req_id, result=res)

            elif method == "room.status":
                res = self.room_service.check_room_status(booking_id=params["booking_id"])
                return MCPResponse(id=req_id, result=res)

            else:
                return MCPResponse(id=req_id, error={"code": -32603, "message": "Unhandled capability"})

        except Exception as e:
            logger.error(f"[MCP] Capability '{method}' failed: {e}")
            return MCPResponse(
                id=req_id,
                error={
                    "code": -32000,
                    "message": f"MCP capability '{method}' failed: {str(e)}",
                    "data": {"capability": method, "exception_type": type(e).__name__}
                }
            )

mcp_gateway = MCPGateway()


def mcp_call(method: str, params: Dict[str, Any], req_id: Optional[str] = None) -> Dict[str, Any]:
    response = mcp_gateway.execute(method=method, params=params, req_id=req_id)
    if response.error:
        return {
            "success": False,
            "error": response.error.get("message", "MCP execution failed"),
            "mcp_capability": method,
            "mcp_error": response.error,
        }
    result = response.result if isinstance(response.result, dict) else {"result": response.result}
    if "success" not in result:
        result = {**result, "success": True}
    if not result.get("success"):
        # Keep the adapter's real error and identify which MCP capability failed.
        result = {**result, "mcp_capability": method}
    return result
