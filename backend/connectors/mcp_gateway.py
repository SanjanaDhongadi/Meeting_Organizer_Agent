from typing import Dict, Any, Optional
import logging
from pydantic import BaseModel, Field
from backend.connectors.calendar_service import get_calendar_service
from backend.connectors.email_service import get_email_service
from backend.connectors.room_service import get_room_service

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
        }
    }

    # Capabilities with external side effects: only allowed for an approved meeting when called from outside.
    CONSEQUENTIAL = {"calendar.create_event", "calendar.create_meet", "gmail.send", "room.request"}

    def __init__(self):
        pass

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

    @staticmethod
    def _meeting_is_approved(meeting_id: Optional[str]) -> bool:
        if not meeting_id:
            return False
        from backend.app.db import SessionLocal, Meeting
        db = SessionLocal()
        try:
            meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
            return bool(meeting and meeting.approval_status == "APPROVED")
        finally:
            db.close()

    def list_capabilities(self) -> Dict[str, Any]:
        return {
            "protocol_version": "2024-11-05",
            "server_info": {"name": "AI Meeting Organizer MCP Gateway", "version": "1.0.0"},
            "capabilities": self.CAPABILITIES
        }

    def execute(self, method: str, params: Dict[str, Any], req_id: Optional[str] = None, auth_context: Optional[Dict[str, Any]] = None) -> MCPResponse:
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
        # Human approval gate for callers outside the workflow (e.g. the public /api/mcp endpoint).
        if auth_context and auth_context.get("external") and method in self.CONSEQUENTIAL \
                and not self._meeting_is_approved(params.get("meeting_id")):
            return MCPResponse(
                id=req_id,
                error={
                    "code": -32003,
                    "message": f"Permission denied: '{method}' requires the meeting_id of a human-APPROVED meeting.",
                    "data": {"capability": method},
                }
            )

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
                    sender_email=params.get("sender_email"),
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
