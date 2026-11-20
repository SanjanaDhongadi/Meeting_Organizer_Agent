"""
Minutes-of-Meeting agent.

Thin wrapper around MinutesOfMeetingSkill (backend/skills/minutes_skill.py), which holds the procedure: the LLM only
extracts from the transcript, and everything it returns is checked against the transcript. This agent owns the single
LLM boundary (`_complete`) and the error handling.
"""
import logging
from typing import Any, Dict, List, Optional

from app.config import settings
from connectors import groq_client
from runtime.agent_interface import AgentOutput, BaseAgent
from skills.minutes_skill import (  # names kept for existing importers
    CHUNK_CHARS, SYSTEM_PROMPT, MinutesOfMeetingSkill, ground, match_participant, norm as _norm,
    split_transcript as _split_transcript,
)

logger = logging.getLogger("mom_agent")


class MoMUnavailable(RuntimeError):
    """The LLM is not configured or could not be reached."""


class MoMAgent(BaseAgent):
    def __init__(self):
        super().__init__("MoM Agent", "Minutes of Meeting writer",
                         "Drafts structured minutes and action items from a Google Meet transcript.")
        self.skill = MinutesOfMeetingSkill()

    # ----- LLM boundary (the only network call; tests replace it) -----
    def _complete(self, system: str, user: str) -> Dict[str, Any]:
        """The only LLM boundary of the MoM agent (tests replace it). Groq, JSON mode, temperature 0."""
        if not groq_client.is_configured():
            raise MoMUnavailable(groq_client.not_configured_message("minutes generation"))
        try:
            return groq_client.chat_json(system, user)
        except groq_client.LLMError as error:
            raise MoMUnavailable(f"The language model request failed ({error.code}): {error}") from error

    # ----- grounding (delegates to the skill) -----
    @staticmethod
    def _match_participant(spoken: Optional[str], participants: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
        return match_participant(spoken, participants)

    def _ground(self, raw: Dict[str, Any], transcript_norm: str, participants: List[Dict[str, str]]) -> Dict[str, Any]:
        return ground(raw, transcript_norm, participants)

    # ----- main entry -----
    def execute(self, state: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> AgentOutput:
        transcript = (state.get("transcript") or "").strip()
        if not transcript:
            return AgentOutput(agent_name=self.name, status="ERROR", errors=["The transcript is empty."])
        try:
            minutes = self.skill.run({"transcript": transcript, "participants": state.get("participants") or [],
                                      "meeting": state.get("meeting") or {}}, complete=self._complete)
        except MoMUnavailable as error:
            return AgentOutput(agent_name=self.name, status="ERROR", errors=[str(error)])
        except Exception as error:
            logger.exception("MoM generation failed")
            return AgentOutput(agent_name=self.name, status="ERROR", errors=[f"MoM generation failed: {error}"])
        return AgentOutput(agent_name=self.name, status="SUCCESS", data={
            **minutes.model_dump(),
            "generated_by": f"groq:{settings.GROQ_LLM_MODEL}",
        })


mom_agent = MoMAgent()
