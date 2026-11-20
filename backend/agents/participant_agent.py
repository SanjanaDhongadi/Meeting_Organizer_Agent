from typing import Dict, Any, List, Optional
import re
from backend.runtime.agent_interface import BaseAgent, AgentOutput
import logging

logger = logging.getLogger("participant_agent")


def normalize_participant(entry: Any) -> Dict[str, str]:
    """Extract clean name and email from a string or dict without checking any directory."""
    if isinstance(entry, dict):
        name = str(entry.get("name") or "").strip()
        email = str(entry.get("email") or "").strip().lower()
        if not name and email:
            name = email.split("@")[0].capitalize()
        return {"name": name, "email": email}

    text = str(entry or "").strip()
    if not text:
        return {"name": "", "email": ""}

    email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", text)
    if email_match:
        email = email_match.group(0).lower()
        name = re.sub(r"[\(<]?\s*" + re.escape(email) + r"\s*[\)>]?", "", text).strip()
        if not name:
            name = email.split("@")[0].capitalize()
        return {"name": name, "email": email}

    return {"name": text, "email": ""}


class ParticipantAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Participant Agent",
            role="Identify participant names and structure attendee profiles",
            description="Extracts participant names and emails directly without employee directory dependency."
        )

    def execute(self, state: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> AgentOutput:
        parsed_participants = state.get("parsed_participants", [])
        resolved = []

        for item in parsed_participants:
            p = normalize_participant(item)
            name = p["name"]
            email = p["email"]

            if not name and not email:
                continue

            resolved.append({
                "name": name or (email.split("@")[0].capitalize() if email else "Attendee"),
                "email": email,
                "is_external": True,
                "calendar_status": "UNVERIFIED"
            })

        return AgentOutput(
            agent_name=self.name,
            status="SUCCESS",
            data={
                "resolved_participants": resolved,
                "unknown_participants": [],
                "ambiguous_participants": [],
                "participant_emails": [p["email"] for p in resolved if p["email"]],
            }
        )
