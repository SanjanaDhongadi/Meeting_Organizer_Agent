from typing import Dict, Any, List, Optional
from backend.skills.base import BaseSkill, SkillMetadata
from backend.app.config import settings
import logging

logger = logging.getLogger("agenda_skill")

class AgendaPreparationSkill(BaseSkill):
    @property
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="Agenda Preparation Skill",
            purpose="Generate a structured, professional draft agenda when meeting purpose is provided, or solicit purpose/skip options.",
            instructions=(
                "1. Check if user provided an explicit purpose or topic.\n"
                "2. If no purpose provided, DO NOT invent one. Return missing purpose prompt.\n"
                "3. If purpose is provided, draft realistic timed agenda items aligned with meeting duration.\n"
                "4. Make all agenda drafts editable by humans."
            ),
            inputs={
                "purpose": "str - Explicit purpose or topic supplied by the organizer",
                "duration_minutes": "int - Planned duration of the meeting",
                "participants": "List[str] - Names/roles of attendees to align discussion points",
                "allow_skip": "bool - Flag whether user opted to skip agenda creation"
            },
            outputs={
                "has_purpose": "bool - Whether sufficient purpose was provided",
                "agenda_text": "str - Formatted draft agenda markdown",
                "action_prompt": "Optional[str] - Question to user if purpose is absent",
                "is_editable": "bool - True, indicating human approval/edit gate"
            },
            constraints=[
                "Never invent an agenda without context.",
                "If purpose is omitted, must request purpose or accept [SKIP AGENDA].",
                "Agenda must be concise, structured, and contain allocated time blocks."
            ],
            examples=[
                {
                    "input": {
                        "purpose": "Discuss AI project progress",
                        "duration_minutes": 30,
                        "participants": ["Alice Chen", "Bob Smith"]
                    },
                    "output": {
                        "has_purpose": True,
                        "agenda_text": "### Proposed Agenda (30 mins)\n1. (00-05m) Welcome & Goal Alignment\n2. (05-20m) AI Project Milestones & Technical Review\n3. (20-30m) Action Items & Deployment Next Steps"
                    }
                },
                {
                    "input": {
                        "purpose": "",
                        "duration_minutes": 30
                    },
                    "output": {
                        "has_purpose": False,
                        "action_prompt": "What is the purpose of the meeting? (Or choose [SKIP AGENDA])"
                    }
                }
            ]
        )

    def run(self, inputs: Dict[str, Any], context: Dict[str, Any] = None) -> Dict[str, Any]:
        purpose = (inputs.get("purpose") or "").strip()
        duration = inputs.get("duration_minutes", 30)
        participants = inputs.get("participants", [])
        allow_skip = inputs.get("allow_skip", False)
        # Long-term memory (RAG) context: related past meetings retrieved for this request.
        related_meetings = inputs.get("related_meetings") or []
        related_titles = [m.get("title") for m in related_meetings if m.get("title")]

        if allow_skip:
            return {
                "has_purpose": False,
                "agenda_text": "[Agenda skipped by organizer request]",
                "action_prompt": None,
                "is_editable": True
            }

        if not purpose:
            return {
                "has_purpose": False,
                "agenda_text": "",
                "action_prompt": "What is the purpose of the meeting? (You may also choose [SKIP AGENDA])",
                "is_editable": True
            }

        # If OpenAI API is available and DEMO_MODE is false or user provided key
        if settings.OPENAI_API_KEY and not settings.DEMO_MODE:
            try:
                from openai import OpenAI
                client = OpenAI(api_key=settings.OPENAI_API_KEY)
                prompt = (
                    f"Create a concise, structured markdown agenda for a {duration}-minute meeting.\n"
                    f"Purpose: '{purpose}'\n"
                    f"Participants: {', '.join(participants) if participants else 'Team members'}\n"
                    + (
                        "Related past meetings retrieved from organizational memory (use them for follow-up items, "
                        "do not invent details beyond them): "
                        + "; ".join(f"{m.get('title')} — purpose: {m.get('purpose')}" for m in related_meetings)
                        + "\n"
                        if related_meetings else ""
                    )
                    + "Include numbered sections with minute allocations."
                )
                res = client.chat.completions.create(
                    model=settings.OPENAI_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=300
                )
                agenda_md = res.choices[0].message.content
                return {
                    "has_purpose": True,
                    "agenda_text": agenda_md,
                    "action_prompt": None,
                    "is_editable": True
                }
            except Exception as e:
                logger.warning(f"OpenAI call failed for agenda ({e}), using structured template.")

        # Structured deterministic draft agenda (academic / offline / demo-ready)
        d_intro = 5
        d_wrap = max(5, duration // 6)
        d_main = duration - d_intro - d_wrap
        
        agenda_lines = [
            f"### Meeting Agenda: {purpose}",
            f"**Planned Duration**: {duration} minutes",
            "",
            f"1. **(00 - {d_intro:02d}m) Welcome & Context Setting**",
            f"   - Align on objectives for: *{purpose}*",
            f"2. **({d_intro:02d} - {(d_intro+d_main):02d}m) Deep-Dive Discussion**",
            f"   - Core review, findings, and technical roadblocks",
            f"   - Input from key participants: {', '.join(participants) if participants else 'Attendees'}",
            *( [f"   - Follow-up on related past meeting(s): {', '.join(related_titles)}"] if related_titles else [] ),
            f"3. **({(d_intro+d_main):02d} - {duration:02d}m) Action Items & Next Steps**",
            f"   - Owner assignments and target deliverables"
        ]

        return {
            "has_purpose": True,
            "agenda_text": "\n".join(agenda_lines),
            "action_prompt": None,
            "is_editable": True
        }
