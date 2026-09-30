from typing import List, Optional
from pydantic import BaseModel, Field
from backend.tools.base import BaseTool, ToolResult
from backend.connectors.mcp_gateway import mcp_call
import logging

logger = logging.getLogger("calendar_tools")

# 3. check_calendar_availability
class CheckCalendarAvailabilityInput(BaseModel):
    emails: List[str] = Field(..., description="List of participant emails to check")
    start_time: str = Field(..., description="Start ISO datetime string (e.g. 2026-10-02T15:00:00Z)")
    end_time: str = Field(..., description="End ISO datetime string (e.g. 2026-10-02T16:00:00Z)")

class CheckCalendarAvailabilityTool(BaseTool):
    name: str = "check_calendar_availability"
    description: str = "Check real calendar free/busy availability for participants. Does NOT use RAG or cosine similarity for availability."
    args_schema = CheckCalendarAvailabilityInput

    def _run(self, args: CheckCalendarAvailabilityInput) -> ToolResult:
        if not args.emails:
            return ToolResult(
                success=False,
                error="Must provide at least one participant email",
                tool_name=self.name
            )
        res = mcp_call("calendar.check_availability", {
            "emails": args.emails,
            "start_time": args.start_time,
            "end_time": args.end_time,
        })
        if not res.get("success") and res.get("error"):
            return ToolResult(success=False, error=res.get("error"), data=res, tool_name=self.name)
        return ToolResult(
            success=True,
            data=res,
            tool_name=self.name
        )

# 4. find_common_slots
class FindCommonSlotsInput(BaseModel):
    emails: List[str] = Field(..., description="List of participant emails")
    date_str: str = Field(..., description="Date to check (e.g. 2026-10-02)")
    duration_minutes: int = Field(30, description="Meeting duration in minutes")

class FindCommonSlotsTool(BaseTool):
    name: str = "find_common_slots"
    description: str = "Find mutually available time slots on a target date for participants."
    args_schema = FindCommonSlotsInput

    def _run(self, args: FindCommonSlotsInput) -> ToolResult:
        cal = get_calendar_service()
        slots = cal.find_common_slots(args.emails, args.date_str, args.duration_minutes)
        return ToolResult(
            success=True,
            data={"common_slots": slots, "count": len(slots)},
            tool_name=self.name
        )

# 5. create_calendar_event
class CreateCalendarEventInput(BaseModel):
    title: str = Field(..., description="Title of the meeting")
    start_time: str = Field(..., description="Start ISO datetime string")
    end_time: str = Field(..., description="End ISO datetime string")
    attendees: List[str] = Field(..., description="List of attendee email addresses")
    description: str = Field("", description="Meeting description and agenda")
    is_online: bool = Field(True, description="Whether meeting is online (generates Meet link)")

class CreateCalendarEventTool(BaseTool):
    name: str = "create_calendar_event"
    description: str = "Create official calendar event on Google Calendar or Mock Calendar."
    args_schema = CreateCalendarEventInput

    def _run(self, args: CreateCalendarEventInput) -> ToolResult:
        res = mcp_call("calendar.create_event", {
            "title": args.title,
            "start_time": args.start_time,
            "end_time": args.end_time,
            "attendees": args.attendees,
            "description": args.description,
            "is_online": args.is_online,
        })
        return ToolResult(
            success=res.get("success", False),
            data=res,
            error=None if res.get("success") else res.get("error"),
            tool_name=self.name
        )

# 6. create_google_meet
class CreateGoogleMeetInput(BaseModel):
    title: str = Field(..., description="Meeting title for Google Meet room")
    start_time: str = Field(..., description="Start ISO datetime")
    end_time: str = Field(..., description="End ISO datetime")
    attendees: Optional[List[str]] = Field(default=[], description="Attendee emails")

class CreateGoogleMeetTool(BaseTool):
    name: str = "create_google_meet"
    description: str = "Generate a Google Meet conference link for an approved online meeting."
    args_schema = CreateGoogleMeetInput

    def _run(self, args: CreateGoogleMeetInput) -> ToolResult:
        res = mcp_call("calendar.create_meet", {
            "title": args.title,
            "start_time": args.start_time,
            "end_time": args.end_time,
            "attendees": args.attendees or [],
        })
        if not res.get("success"):
            return ToolResult(
                success=False,
                error=res.get("error") or "Could not generate Google Meet conference URL",
                data=res,
                tool_name=self.name
            )
        meet_url = res.get("meet_url")
        if not meet_url:
            return ToolResult(
                success=False,
                error="Could not generate Google Meet conference URL",
                data=res,
                tool_name=self.name
            )
        return ToolResult(
            success=True,
            data={"meet_url": meet_url, "event_id": res.get("event_id")},
            tool_name=self.name
        )
