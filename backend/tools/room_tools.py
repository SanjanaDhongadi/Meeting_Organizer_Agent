from typing import Optional
from pydantic import BaseModel, Field
from backend.tools.base import BaseTool, ToolResult
from backend.connectors.mcp_gateway import mcp_call
import logging

logger = logging.getLogger("room_tools")

# 8. request_room_booking
class RequestRoomBookingInput(BaseModel):
    room_name: str = Field(..., description="Name of the physical room or auditorium (e.g. Auditorium Alpha, Conference Room B)")
    start_time: str = Field(..., description="Start ISO datetime")
    end_time: str = Field(..., description="End ISO datetime")
    capacity: int = Field(10, description="Minimum seating capacity required")
    equipment: str = Field("", description="Special equipment required (e.g. Projector, Mic, Livestream Rig)")
    meeting_id: str = Field(..., description="ID of the meeting requiring the room")
    duration_minutes: Optional[int] = Field(None, description="Meeting duration in minutes")
    participant_count: Optional[int] = Field(None, description="Number of participants")
    purpose: str = Field("", description="Meeting purpose")
    sender_email: Optional[str] = Field(None, description="Connected Google account used to send the request")

class RequestRoomBookingTool(BaseTool):
    name: str = "request_room_booking"
    description: str = "Submit a reservation request for an offline room or auditorium."
    args_schema = RequestRoomBookingInput

    def _run(self, args: RequestRoomBookingInput) -> ToolResult:
        res = mcp_call("room.request", {
            "room_name": args.room_name,
            "start_time": args.start_time,
            "end_time": args.end_time,
            "capacity": args.capacity,
            "equipment": args.equipment,
            "meeting_id": args.meeting_id,
            "duration_minutes": args.duration_minutes,
            "participant_count": args.participant_count,
            "purpose": args.purpose,
            "sender_email": args.sender_email,
        })
        success = res.get("success", True) and res.get("status") not in {"FAILED"}
        return ToolResult(
            success=success,
            data=res,
            error=None if success else res.get("error") or res.get("message"),
            tool_name=self.name
        )

# 9. check_room_status
class CheckRoomStatusInput(BaseModel):
    booking_id: str = Field(..., description="Booking request reference ID")

class CheckRoomStatusTool(BaseTool):
    name: str = "check_room_status"
    description: str = "Check the reservation status of an offline meeting room or auditorium request."
    args_schema = CheckRoomStatusInput

    def _run(self, args: CheckRoomStatusInput) -> ToolResult:
        res = mcp_call("room.status", {"booking_id": args.booking_id})
        if not res.get("success") and res.get("error"):
            return ToolResult(success=False, error=res.get("error"), data=res, tool_name=self.name)
        if res.get("status") == "NOT_FOUND":
            return ToolResult(
                success=False,
                error=f"No room booking found with ID '{args.booking_id}'",
                tool_name=self.name
            )
        return ToolResult(
            success=True,
            data=res,
            tool_name=self.name
        )
