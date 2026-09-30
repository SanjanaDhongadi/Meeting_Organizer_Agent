from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from pydantic import BaseModel

class AgentOutput(BaseModel):
    agent_name: str
    status: str  # SUCCESS, NEEDS_INFO, CONFLICT, ERROR
    data: Dict[str, Any] = {}
    message: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = []
    errors: List[str] = []

class BaseAgent(ABC):
    def __init__(self, name: str, role: str, description: str):
        self.name = name
        self.role = role
        self.description = description

    @abstractmethod
    def execute(self, state: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> AgentOutput:
        """Core execution method invoked by the AgentRuntime."""
        pass
