import uuid
import logging
from typing import Dict, Any, Optional, List
from backend.runtime.agent_interface import BaseAgent, AgentOutput
from backend.skills.registry import skill_registry
from backend.tools.registry import tool_registry
from backend.memory.session_memory import session_memory
from backend.memory.long_term_memory import long_term_memory
from backend.memory.audit_memory import audit_memory
from backend.agents.coordinator_agent import MeetingCoordinator

logger = logging.getLogger("agent_runtime")

class AgentRuntime:
    """
    Lab 6: Reusable Agent Runtime
    Provides the central execution loop governing Agent/Skill selection,
    Tool invocation, Session & Long-Term Memory access, and Audit logging.
    """

    def __init__(self):
        self.coordinator = MeetingCoordinator()
        self.tool_registry = tool_registry
        self.skill_registry = skill_registry
        self.session_memory = session_memory
        self.long_term_memory = long_term_memory
        self.audit_memory = audit_memory

    def run_meeting_request(self, raw_request: str, session_id: Optional[str] = None, skip_agenda: bool = False) -> Dict[str, Any]:
        """
        Live entry point. Runs the LangGraph workflow (backend/graph/workflow_graph.py):
        intake -> parse -> identify participants -> RAG context -> availability -> (alternative slot)
        -> agenda -> resource -> validate/critique/conflicts -> draft plan -> human approval gate (pauses).
        Approval, edits and asynchronous responses later resume the SAME persisted meeting in the same graph.
        """
        from backend.graph.workflow_graph import start_meeting_workflow, workflow_view

        meeting_id = session_id or f"meet-{uuid.uuid4().hex[:10]}"
        logger.info(f"[Runtime] Running LangGraph workflow for meeting {meeting_id}: '{raw_request}'")
        final_state = start_meeting_workflow(raw_request, meeting_id=meeting_id, skip_agenda=skip_agenda)
        return workflow_view(final_state)

agent_runtime = AgentRuntime()
