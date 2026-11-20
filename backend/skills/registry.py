from typing import Dict, Any, List
from skills.base import BaseSkill
from skills.scheduling_skill import MeetingSchedulingSkill
from skills.agenda_skill import AgendaPreparationSkill
from skills.validation_skill import ValidationSkill
from skills.plan_skill import PlanSkill
from skills.format_skill import FormatMeetingSkill
from skills.minutes_skill import MinutesOfMeetingSkill

class SkillRegistry:
    def __init__(self):
        self._skills: Dict[str, BaseSkill] = {}
        self._register_default_skills()

    def register(self, skill: BaseSkill):
        self._skills[skill.metadata.name] = skill

    def get(self, name: str) -> BaseSkill:
        if name not in self._skills:
            raise KeyError(f"Skill '{name}' is not registered.")
        return self._skills[name]

    def list_skills(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": s.metadata.name,
                "purpose": s.metadata.purpose,
                "instructions": s.metadata.instructions,
                "inputs": s.metadata.inputs,
                "outputs": s.metadata.outputs,
                "constraints": s.metadata.constraints,
                "examples": s.metadata.examples,
                # Pydantic-typed skills also publish their schemas.
                **({"input_schema": s.input_model.model_json_schema()} if getattr(s, "input_model", None) else {}),
                **({"output_schema": s.output_model.model_json_schema()} if getattr(s, "output_model", None) else {}),
            }
            for s in self._skills.values()
        ]

    def _register_default_skills(self):
        self.register(MeetingSchedulingSkill())
        self.register(AgendaPreparationSkill())
        self.register(ValidationSkill())
        self.register(PlanSkill())
        self.register(FormatMeetingSkill())
        self.register(MinutesOfMeetingSkill())

skill_registry = SkillRegistry()
