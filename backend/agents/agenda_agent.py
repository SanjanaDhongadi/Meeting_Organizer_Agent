from typing import Dict, Any, Optional
from runtime.agent_interface import BaseAgent, AgentOutput
from skills.agenda_skill import AgendaPreparationSkill
import logging

logger = logging.getLogger("agenda_agent")

class AgendaAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Agenda Agent",
            role="Draft, structure, and refine meeting agendas into professional executive formats",
            description="Converts rough English topics into clear, professional, logically structured agendas while preserving user intent."
        )
        self.skill = AgendaPreparationSkill()

    def execute(self, state: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> AgentOutput:
        skill_res = self.skill.run({
            "agenda_points": state.get("agenda_points") or [],
            "purpose": state.get("purpose", ""),
            "title": state.get("title", ""),
            "raw_request": state.get("raw_request", ""),
        })
        return AgentOutput(
            agent_name=self.name,
            status="SUCCESS",
            data={
                "agenda": skill_res["agenda_text"],
                "agenda_points": skill_res["agenda_points"],
                "has_purpose": skill_res["has_purpose"],
                "is_editable": True,
            }
        )
