from abc import ABC, abstractmethod
from typing import Dict, Any, List
from pydantic import BaseModel

class SkillMetadata(BaseModel):
    name: str
    purpose: str
    instructions: str
    inputs: Dict[str, str]
    outputs: Dict[str, str]
    constraints: List[str]
    examples: List[Dict[str, Any]]

class BaseSkill(ABC):
    @property
    @abstractmethod
    def metadata(self) -> SkillMetadata:
        pass

    @abstractmethod
    def run(self, inputs: Dict[str, Any], context: Dict[str, Any] = None) -> Dict[str, Any]:
        """Execute the deterministic or AI skill procedure."""
        pass
