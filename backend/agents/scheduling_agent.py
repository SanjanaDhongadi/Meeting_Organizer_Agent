from typing import Dict, Any, Optional
from backend.runtime.agent_interface import BaseAgent, AgentOutput
from backend.skills.scheduling_skill import MeetingSchedulingSkill
import logging

logger = logging.getLogger("scheduling_agent")

class SchedulingAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Scheduling & Availability Agent",
            role="Query calendar services and verify schedule alignment",
            description="Checks Google Calendar / Mock Calendar free-busy status, checks working hours, and identifies optimal slots without using RAG for availability."
        )
        self.skill = MeetingSchedulingSkill()

    def execute(self, state: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> AgentOutput:
        participant_emails = state.get("participant_emails", [])
        target_date = state.get("target_date")
        target_start = state.get("target_start_time")
        target_end = state.get("target_end_time")
        duration = state.get("duration_minutes", 30)

        tool_calls = [{"tool": "check_calendar_availability", "emails": participant_emails, "start": target_start, "end": target_end}]

        skill_result = self.skill.run({
            "participant_emails": participant_emails,
            "target_date": target_date,
            "target_start_time": target_start,
            "target_end_time": target_end,
            "duration_minutes": duration,
            "profiles": state.get("resolved_participants", [])
        })

        is_avail = skill_result.get("is_slot_available", False)
        conflicts = skill_result.get("conflicts", [])

        status = "SUCCESS" if is_avail else ("CONFLICT" if conflicts else "SUCCESS")

        return AgentOutput(
            agent_name=self.name,
            status=status,
            data={
                "is_slot_available": is_avail,
                "selected_slot": skill_result.get("selected_slot"),
                "alternative_slots": skill_result.get("alternative_slots", []),
                "participant_statuses": skill_result.get("participant_statuses", {}),
                "conflicts": conflicts
            },
            tool_calls=tool_calls,
            errors=[c["conflict"] for c in conflicts if c.get("severity") == "HIGH"]
        )
