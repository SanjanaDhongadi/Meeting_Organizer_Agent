from typing import Dict, Any, Optional
from backend.runtime.agent_interface import BaseAgent, AgentOutput
from backend.skills.agenda_skill import AgendaPreparationSkill
import logging

logger = logging.getLogger("agenda_agent")

class AgendaAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Agenda Agent",
            role="Draft, structure, and refine meeting agendas",
            description="Generates concise timed meeting agendas when purpose is supplied, and prompts for clarification or skip if purpose is omitted."
        )
        self.skill = AgendaPreparationSkill()

    def execute(self, state: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> AgentOutput:
        purpose = state.get("purpose", "")
        duration = state.get("duration_minutes", 30)
        participants = [p.get("name", "") for p in state.get("resolved_participants", [])]
        allow_skip = state.get("skip_agenda", False)

        skill_res = self.skill.run({
            "purpose": purpose,
            "duration_minutes": duration,
            "participants": participants,
            "allow_skip": allow_skip
        })

        has_purpose = skill_res.get("has_purpose", False)
        action_prompt = skill_res.get("action_prompt")
        agenda_text = skill_res.get("agenda_text", "")

        if not has_purpose and not allow_skip:
            return AgentOutput(
                agent_name=self.name,
                status="NEEDS_INFO",
                data={
                    "agenda": "",
                    "has_purpose": False,
                    "action_prompt": action_prompt,
                    "allow_skip_option": True
                },
                message=action_prompt,
                errors=["Meeting purpose is required to prepare an agenda, or user may choose to skip."]
            )

        return AgentOutput(
            agent_name=self.name,
            status="SUCCESS",
            data={
                "agenda": agenda_text,
                "has_purpose": has_purpose,
                "is_editable": True
            }
        )
