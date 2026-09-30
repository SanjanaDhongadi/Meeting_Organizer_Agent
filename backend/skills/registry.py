from typing import Dict, Any, List
from backend.skills.base import BaseSkill
from backend.skills.scheduling_skill import MeetingSchedulingSkill
from backend.skills.agenda_skill import AgendaPreparationSkill
from backend.skills.validation_skill import ValidationSkill

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
                "examples": s.metadata.examples
            }
            for s in self._skills.values()
        ]

    def _register_default_skills(self):
        self.register(MeetingSchedulingSkill())
        self.register(AgendaPreparationSkill())
        self.register(ValidationSkill())

skill_registry = SkillRegistry()
