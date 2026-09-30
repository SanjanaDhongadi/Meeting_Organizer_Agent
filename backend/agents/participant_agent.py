from typing import Dict, Any, List, Optional
from backend.runtime.agent_interface import BaseAgent, AgentOutput
from backend.tools.participant_tools import FindParticipantTool, RetrieveParticipantProfileTool
import logging

logger = logging.getLogger("participant_agent")

class ParticipantAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Participant Agent",
            role="Identify, verify, and retrieve participant profiles from directory or vector store",
            description="Resolves participant names, flags unknown individuals, handles ambiguous name collisions, and tags external attendees."
        )
        self.find_tool = FindParticipantTool()
        self.profile_tool = RetrieveParticipantProfileTool()

    def execute(self, state: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> AgentOutput:
        parsed_participants = state.get("parsed_participants", [])
        tool_calls = []
        resolved = []
        unknowns = []
        ambiguities = []

        for p_name in parsed_participants:
            p_clean = p_name.strip()
            if not p_clean:
                continue

            tool_calls.append({"tool": "find_participant", "query": p_clean})
            res = self.find_tool.execute(query=p_clean)
            
            data = res.data or {}
            match_type = data.get("match_type")
            matches = data.get("participants", [])

            if match_type in ["EXACT_MATCH", "EXACT_EMAIL"] and len(matches) == 1:
                emp = matches[0]
                resolved.append({
                    "name": emp["name"],
                    "email": emp["email"],
                    "employee_id": emp["employee_id"],
                    "designation": emp["designation"],
                    "department": emp["department"],
                    "timezone": emp["timezone"],
                    "working_days": emp["working_days"],
                    "working_hours": f"{emp['working_hours_start']} - {emp['working_hours_end']}",
                    "preferences": emp["meeting_preferences"],
                    "is_external": False,
                    "calendar_status": "AVAILABLE" if emp.get("google_calendar_connected") else "UNVERIFIED"
                })
            elif match_type == "AMBIGUOUS" or len(matches) > 1:
                ambiguities.append({
                    "queried_name": p_clean,
                    "candidates": matches,
                    "message": f"Multiple employees found matching '{p_clean}'. Please select the intended participant."
                })
            elif "@" in p_clean:
                # Direct external email provided
                resolved.append({
                    "name": p_clean.split("@")[0].capitalize(),
                    "email": p_clean,
                    "employee_id": None,
                    "designation": "External Guest",
                    "department": "External",
                    "timezone": "UTC",
                    "working_days": "Unknown",
                    "working_hours": "Unknown",
                    "preferences": "None",
                    "is_external": True,
                    "calendar_status": "UNVERIFIED"
                })
            else:
                # Unknown participant in database -> Rule: DO NOT invent
                unknowns.append({
                    "queried_name": p_clean,
                    "message": "Participant not found. Please register this person or provide their email address."
                })

        status = "SUCCESS"
        errors = []
        if unknowns:
            status = "NEEDS_INFO"
            errors.extend([u["message"] for u in unknowns])
        if ambiguities:
            status = "NEEDS_INFO"
            errors.extend([a["message"] for a in ambiguities])

        return AgentOutput(
            agent_name=self.name,
            status=status,
            data={
                "resolved_participants": resolved,
                "unknown_participants": unknowns,
                "ambiguous_participants": ambiguities,
                "participant_emails": [r["email"] for r in resolved]
            },
            tool_calls=tool_calls,
            errors=errors
        )
