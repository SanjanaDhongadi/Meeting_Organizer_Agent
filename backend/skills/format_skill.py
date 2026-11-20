"""
Meeting Format Skill (Lab 3): renders meeting documents (subject + body) from a template and a typed result.

Templates: "invite" (calendar invitation e-mail) and "minutes" (approved minutes e-mail). The text is exactly what
the workflow and the minutes job produced before this skill existed.
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from backend.skills.base import BaseSkill, SkillMetadata


class InviteResult(BaseModel):
    title: str
    purpose: Optional[str] = None
    when_date: str
    when_time: str
    is_online: bool = True
    meet_url: Optional[str] = None
    venue: str = ""
    agenda: Optional[str] = None


class DiscussionPoint(BaseModel):
    topic: Optional[str] = None
    details: Optional[str] = None


class Decision(BaseModel):
    text: str


class ActionLine(BaseModel):
    description: str
    owner_name: Optional[str] = None
    due_date: Optional[str] = None


class MinutesResult(BaseModel):
    title: Optional[str] = None
    date: Any = None
    duration_minutes: Any = None
    participants: List[str] = Field(default_factory=list)
    summary: Optional[str] = None
    discussion_points: List[DiscussionPoint] = Field(default_factory=list)
    decisions: List[Decision] = Field(default_factory=list)
    action_items: List[ActionLine] = Field(default_factory=list)


class FormatInput(BaseModel):
    template: str
    result: Dict[str, Any]


class FormattedDocument(BaseModel):
    subject: str
    body: str


class FormatMeetingSkill(BaseSkill):
    input_model = FormatInput
    output_model = FormattedDocument

    @property
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="Meeting Format Skill",
            purpose="Render meeting documents (invitation, minutes) from a template and a typed result.",
            instructions="Validate the result against the template's model, then fill the template. Unknown templates are rejected.",
            inputs={"template": "str - 'invite' or 'minutes'", "result": "dict - InviteResult or MinutesResult fields"},
            outputs={"subject": "str", "body": "str"},
            constraints=["Pure formatting: no I/O.", "Unknown template raises ValueError."],
            examples=[],
        )

    def run(self, result: Dict[str, Any], template: str) -> Dict[str, str]:
        if template == "invite":
            doc = self._invite(InviteResult(**result))
        elif template == "minutes":
            doc = self._minutes(MinutesResult(**result))
        else:
            raise ValueError(f"Unknown meeting template '{template}'. Available: invite, minutes.")
        return doc.model_dump()

    @staticmethod
    def _invite(r: InviteResult) -> FormattedDocument:
        where = ((f"Google Meet: {r.meet_url}" if r.meet_url else "Google Meet: not available (see calendar invitation)")
                 if r.is_online else f"Venue: {r.venue}")
        body = "\n".join([
            f"You are invited to: {r.title}",
            f"Purpose: {r.purpose or '(not provided)'}",
            f"When: {r.when_date}, {r.when_time}",
            where,
            "",
            "Agenda:",
            r.agenda or "(no agenda provided)",
            "",
            "Please accept or decline the Google Calendar invitation.",
        ])
        return FormattedDocument(subject=f"Invitation: {r.title}", body=body)

    @staticmethod
    def _minutes(r: MinutesResult) -> FormattedDocument:
        lines = [f"Minutes of Meeting: {r.title}", "",
                 f"Date: {r.date or 'Not specified'}",
                 f"Duration: {r.duration_minutes or 'Not specified'} minutes",
                 "Participants: " + (", ".join(r.participants) or "Not specified"),
                 "", "SUMMARY", r.summary or "Not specified", "", "KEY DISCUSSION POINTS"]
        lines += [f"- {p.topic}: {p.details}" if p.topic and p.details else f"- {p.topic or p.details}"
                  for p in r.discussion_points] or ["None recorded"]
        lines += ["", "DECISIONS"] + ([f"- {d.text}" for d in r.decisions] or ["None recorded"])
        lines += ["", "ACTION ITEMS"]
        for i, item in enumerate(r.action_items, start=1):
            lines.append(f"{i}. {item.description} | Owner: {item.owner_name or 'Not specified'} | "
                         f"Due: {item.due_date or 'Not specified'}")
        if not r.action_items:
            lines.append("None recorded")
        lines += ["", "You can track your action items in the Meeting Organizer app."]
        return FormattedDocument(subject=f"Minutes of Meeting: {r.title} ({r.date or 'n/a'})", body="\n".join(lines))
