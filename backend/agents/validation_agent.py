from typing import Dict, Any, Optional
from backend.runtime.agent_interface import BaseAgent, AgentOutput
from backend.skills.validation_skill import ValidationSkill
import logging

logger = logging.getLogger("validation_agent")

class ValidationAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Validation Agent",
            role="Perform holistic verification across participants, scheduling, and resources",
            description="Evaluates constraint satisfaction, flags missing information, and issues approval eligibility decisions."
        )
        self.skill = ValidationSkill()

    def execute(self, state: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> AgentOutput:
        plan = state.get("draft_plan", {})
        participants = state.get("resolved_participants", [])
        mode = state.get("mode") or plan.get("mode", "ONLINE")
        room_details = state.get("resource_data", {})

        skill_res = self.skill.run({
            "meeting_plan": plan,
            "participants": participants,
            "mode": mode,
            "room_details": room_details
        })

        status = skill_res.get("status", "PASS")
        errors = skill_res.get("errors", [])
        warnings = skill_res.get("warnings", [])
        can_proceed = skill_res.get("can_proceed_to_approval", False)

        agent_status = "SUCCESS" if can_proceed else "ERROR"

        return AgentOutput(
            agent_name=self.name,
            status=agent_status,
            data={
                "validation_status": status,
                "can_proceed_to_approval": can_proceed,
                "errors": errors,
                "warnings": warnings
            },
            errors=errors
        )
