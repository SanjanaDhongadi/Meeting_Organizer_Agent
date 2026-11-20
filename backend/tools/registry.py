from typing import Dict, Any, List
from tools.base import BaseTool
from tools.participant_tools import FindParticipantTool, RetrieveParticipantProfileTool
from tools.calendar_tools import (
    CheckCalendarAvailabilityTool,
    FindCommonSlotsTool,
    CreateCalendarEventTool,
    CreateGoogleMeetTool
)
from tools.communication_tools import SendEmailTool
from tools.room_tools import RequestRoomBookingTool, CheckRoomStatusTool
from tools.state_tools import SaveMeetingStateTool, LoadMeetingStateTool, CreateAuditLogTool

class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}
        self._register_default_tools()

    def register(self, tool: BaseTool):
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' is not registered.")
        return self._tools[name]

    def list_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "schema": t.args_schema.schema() if hasattr(t, "args_schema") else {}
            }
            for t in self._tools.values()
        ]

    def _register_default_tools(self):
        tools = [
            FindParticipantTool(),
            RetrieveParticipantProfileTool(),
            CheckCalendarAvailabilityTool(),
            FindCommonSlotsTool(),
            CreateCalendarEventTool(),
            CreateGoogleMeetTool(),
            SendEmailTool(),
            RequestRoomBookingTool(),
            CheckRoomStatusTool(),
            SaveMeetingStateTool(),
            LoadMeetingStateTool(),
            CreateAuditLogTool()
        ]
        for t in tools:
            self.register(t)

tool_registry = ToolRegistry()
