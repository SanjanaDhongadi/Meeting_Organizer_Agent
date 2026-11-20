"""
Meeting Plan Skill (Lab 3): turns the parsed meeting details into a LOCKED plan object.

The plan is an immutable (frozen) Pydantic model with a content hash and a version. Later steps compare their draft
against it; an organizer edit that changes the content produces a new version. The skill never touches calendars,
the database or the LLM.
"""
import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from backend.app.config import settings
from backend.skills.base import BaseSkill, SkillMetadata


class PlanParticipant(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str = ""
    email: str = ""


class PlanInput(BaseModel):
    title: Optional[str] = None
    purpose: Optional[str] = None
    date: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration_minutes: Optional[int] = None
    mode: Optional[str] = None
    room_name: Optional[str] = None
    venue_coordinator_email: Optional[str] = None
    participants: List[PlanParticipant] = Field(default_factory=list)
    agenda_points: List[str] = Field(default_factory=list)
    previous_plan: Optional[Dict[str, Any]] = None


class MeetingPlan(BaseModel):
    model_config = ConfigDict(frozen=True)  # immutable: this is what "locked" means
    title: str = ""
    purpose: str = ""
    date: str = ""
    start_time: str = ""
    end_time: str = ""
    duration_minutes: int = 0
    mode: str = ""
    room_name: str = ""
    venue_coordinator_email: str = ""
    participants: Tuple[PlanParticipant, ...] = ()
    agenda_points: Tuple[str, ...] = ()
    timezone: str = "UTC"
    plan_hash: str
    version: int
    locked_at: str


CONTENT_FIELDS = ("title", "purpose", "date", "start_time", "end_time", "duration_minutes", "mode", "room_name",
                  "venue_coordinator_email", "participants", "agenda_points")


class PlanSkill(BaseSkill):
    input_model = PlanInput
    output_model = MeetingPlan

    @property
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="Meeting Plan Skill",
            purpose="Lock the parsed meeting details into an immutable, hashed and versioned plan.",
            instructions=(
                "1. Normalise the parsed details into the typed content fields.\n"
                "2. Hash the canonical JSON of the content fields.\n"
                "3. Keep the previous version when the hash is unchanged, otherwise increase it by one.\n"
                "4. Return the frozen MeetingPlan; later steps only compare against it."
            ),
            inputs={"details": "PlanInput - parsed meeting details and an optional previous_plan"},
            outputs={"plan": "MeetingPlan - frozen plan with plan_hash, version and locked_at"},
            constraints=["The plan is immutable.", "Same content gives the same hash and version.",
                         "No calendar, database or LLM access."],
            examples=[],
        )

    @staticmethod
    def _hash(content: Dict[str, Any]) -> str:
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def run(self, inputs: Any, context: Optional[Dict[str, Any]] = None) -> MeetingPlan:
        data = inputs if isinstance(inputs, PlanInput) else PlanInput(**(inputs or {}))
        content: Dict[str, Any] = {
            "title": (data.title or "").strip(),
            "purpose": (data.purpose or "").strip(),
            "date": data.date or "",
            "start_time": data.start_time or "",
            "end_time": data.end_time or "",
            "duration_minutes": data.duration_minutes or 0,
            "mode": (data.mode or "").upper(),
            "room_name": (data.room_name or "").strip(),
            "venue_coordinator_email": (data.venue_coordinator_email or "").strip().lower(),
            "participants": [{"name": p.name.strip(), "email": p.email.strip().lower()}
                             for p in sorted(data.participants, key=lambda p: p.email.strip().lower())],
            "agenda_points": [a.strip() for a in data.agenda_points if a and a.strip()],
        }
        plan_hash = self._hash(content)
        previous = data.previous_plan or {}
        if previous.get("plan_hash") == plan_hash:
            version, locked_at = int(previous.get("version") or 1), previous.get("locked_at")
        else:
            version = int(previous.get("version") or 0) + 1
            locked_at = None
        return MeetingPlan(
            **{**content, "participants": tuple(PlanParticipant(**p) for p in content["participants"]),
               "agenda_points": tuple(content["agenda_points"])},
            timezone=settings.MEETING_TIMEZONE, plan_hash=plan_hash, version=version,
            locked_at=locked_at or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        )


def plan_differences(plan: Dict[str, Any], slot_start: Optional[str], slot_end: Optional[str], mode: Optional[str],
                     venue: Optional[str], emails: List[str]) -> List[str]:
    """Names of the fields in which a draft differs from a locked plan (venue only when the plan names one)."""
    diffs: List[str] = []
    if (slot_start or "") != plan.get("start_time", ""):
        diffs.append("start_time")
    if (slot_end or "") != plan.get("end_time", ""):
        diffs.append("end_time")
    if (mode or "").upper() != plan.get("mode", ""):
        diffs.append("mode")
    if plan.get("room_name") and (venue or "").strip().lower() != plan["room_name"].strip().lower():
        diffs.append("venue")
    locked = sorted((p.get("email") or "").lower() for p in plan.get("participants", []))
    if sorted(e.lower() for e in emails if e) != locked:
        diffs.append("participants")
    return diffs
