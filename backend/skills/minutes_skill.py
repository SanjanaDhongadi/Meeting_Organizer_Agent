"""
Minutes of Meeting Skill (Lab 3): the procedure that turns a transcript into grounded minutes.

The LLM only extracts. The model call is injected as `complete(system, user) -> dict`, so this skill has no network
access of its own (MoMAgent._complete is the single LLM boundary). Everything returned by `complete` is checked
against the transcript: a decision or action item must carry a quote that really occurs in the transcript, an owner
must be one of the meeting's participants, and a due date must be backed by words in the transcript. Whatever fails
the check is dropped (or marked "not specified") and reported as a warning.
"""
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from skills.base import BaseSkill, SkillMetadata

CHUNK_CHARS = 60000

SYSTEM_PROMPT = (
    "You write Minutes of Meeting strictly from a meeting transcript. Use ONLY what is in the transcript. "
    "Never invent discussions, decisions, owners or deadlines. If an owner or due date was not clearly stated, "
    "use null. Reply with a JSON object with keys: "
    "summary (string, 2-5 sentences), "
    "topic (short project/topic name the meeting was about, or null), "
    "discussion_points (list of {topic, details}), "
    "decisions (list of {decision, evidence}), "
    "action_items (list of {description, owner, due_date, due_date_text, topic, evidence}). "
    "'evidence' must be one short VERBATIM quote copied from the transcript that supports the item. "
    "'owner' is the person's name exactly as spoken in the transcript, or null. "
    "'due_date' is YYYY-MM-DD only when a deadline was explicitly stated (resolve relative words like 'Friday' from "
    "the meeting date given), otherwise null; 'due_date_text' is the deadline wording copied from the transcript. "
    "Only include action items someone actually committed to or was asked to do. Empty lists are fine."
)


class MinutesParticipant(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: Optional[str] = ""
    email: Optional[str] = None


class MinutesMeeting(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: Any = ""
    date: Any = ""


class MinutesInput(BaseModel):
    transcript: str
    participants: List[MinutesParticipant] = Field(default_factory=list)
    meeting: MinutesMeeting = Field(default_factory=MinutesMeeting)


class MinutesDecision(BaseModel):
    text: str
    evidence: str


class MinutesDiscussionPoint(BaseModel):
    topic: str = ""
    details: str = ""


class MinutesActionItem(BaseModel):
    description: str
    owner_name: str = ""
    owner_email: Optional[str] = None
    due_date: Optional[str] = None
    project_topic: str = ""
    source_quote: str = ""


class MinutesOutput(BaseModel):
    summary: str = ""
    topic: str = ""
    discussion_points: List[MinutesDiscussionPoint] = Field(default_factory=list)
    decisions: List[MinutesDecision] = Field(default_factory=list)
    action_items: List[MinutesActionItem] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


def split_transcript(transcript: str) -> List[str]:
    chunks, current, size = [], [], 0
    for line in transcript.splitlines():
        if size + len(line) > CHUNK_CHARS and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def match_participant(spoken: Optional[str], participants: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    key = norm(spoken or "")
    if not key:
        return None
    exact = [p for p in participants if key in (norm(p.get("name", "")), norm((p.get("email") or "").split("@")[0]))
             or key == norm(p.get("email", ""))]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    # A single spoken first name that identifies exactly one participant.
    first = [p for p in participants if norm(p.get("name", "")).split(" ")[:1] == [key]]
    return first[0] if len(first) == 1 else None


def ground(raw: Dict[str, Any], transcript_norm: str, participants: List[Dict[str, Any]]) -> Dict[str, Any]:
    warnings: List[str] = []

    def supported(evidence: Any) -> bool:
        quote = norm(str(evidence or ""))
        return len(quote) >= 8 and quote in transcript_norm

    decisions, dropped = [], 0
    for d in raw.get("decisions") or []:
        text = str((d or {}).get("decision") or "").strip()
        if text and supported((d or {}).get("evidence")):
            decisions.append({"text": text, "evidence": str(d["evidence"]).strip()})
        elif text:
            dropped += 1
    items, dropped_items = [], 0
    for a in raw.get("action_items") or []:
        a = a or {}
        description = str(a.get("description") or "").strip()
        if not description:
            continue
        if not supported(a.get("evidence")):
            dropped_items += 1
            continue
        spoken = (str(a["owner"]).strip() if a.get("owner") else "")
        match = match_participant(spoken, participants)
        due = a.get("due_date")
        if due:
            try:
                datetime.strptime(str(due), "%Y-%m-%d")
            except ValueError:
                due = None
        if due and not (norm(str(a.get("due_date_text") or "")) and norm(str(a["due_date_text"])) in transcript_norm):
            due = None  # a deadline the transcript does not contain
        items.append({
            "description": description,
            "owner_name": match["name"] if match else spoken,
            "owner_email": match["email"] if match else None,
            "due_date": str(due) if due else None,
            "project_topic": str(a.get("topic") or "").strip()[:255],
            "source_quote": str(a["evidence"]).strip(),
        })
    if dropped:
        warnings.append(f"{dropped} decision(s) were left out because the transcript does not support them.")
    if dropped_items:
        warnings.append(f"{dropped_items} action item(s) were left out because the transcript does not support them.")
    if any(i["owner_name"] and not i["owner_email"] for i in items):
        warnings.append("Some action-item owners could not be matched to a meeting participant; please assign them.")
    points = [{"topic": str(p.get("topic") or "").strip(), "details": str(p.get("details") or "").strip()}
              for p in (raw.get("discussion_points") or []) if isinstance(p, dict) and (p.get("topic") or p.get("details"))]
    return {"decisions": decisions, "action_items": items, "discussion_points": points, "warnings": warnings}


class MinutesOfMeetingSkill(BaseSkill):
    input_model = MinutesInput
    output_model = MinutesOutput

    @property
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="Minutes of Meeting Skill",
            purpose="Draft minutes and action items from a transcript, keeping only what the transcript supports.",
            instructions=(
                "1. Split the transcript into chunks and ask the injected model to extract from each chunk.\n"
                "2. Keep a decision or action item only if its quoted evidence occurs verbatim in the transcript.\n"
                "3. Match owners to meeting participants; clear a due date the transcript does not contain.\n"
                "4. Merge chunks and drop duplicate action items; report everything that was left out as a warning."
            ),
            inputs={"transcript": "str", "participants": "list of {name, email}", "meeting": "{title, date}"},
            outputs={"minutes": "MinutesOutput - summary, topic, discussion_points, decisions, action_items, warnings"},
            constraints=["Never invent content, owners or deadlines.", "The model call is injected, never made here."],
            examples=[],
        )

    def run(self, inputs: Any, complete: Callable[[str, str], Dict[str, Any]]) -> MinutesOutput:  # type: ignore[override]
        data = inputs if isinstance(inputs, MinutesInput) else MinutesInput(**inputs)
        participants = [{**p.model_dump(), "name": p.name or ""} for p in data.participants]
        header = (f"Meeting: {data.meeting.title}\nMeeting date: {data.meeting.date}\n"
                  f"Participants: {', '.join(p.get('name', '') for p in participants)}\n\nTranscript:\n")
        summaries, topics, points, decisions, items, warnings = [], [], [], [], [], []
        transcript_norm = norm(data.transcript)
        for chunk in split_transcript(data.transcript):
            raw = complete(SYSTEM_PROMPT, header + chunk)
            grounded = ground(raw, transcript_norm, participants)
            if str(raw.get("summary") or "").strip():
                summaries.append(str(raw["summary"]).strip())
            if raw.get("topic"):
                topics.append(str(raw["topic"]).strip())
            points += grounded["discussion_points"]
            decisions += grounded["decisions"]
            items += grounded["action_items"]
            warnings += grounded["warnings"]

        seen, unique_items = set(), []
        for item in items:
            key = (norm(item["description"]), item["owner_email"] or norm(item["owner_name"]))
            if key not in seen:
                seen.add(key)
                unique_items.append(item)
        topic = topics[0] if topics else ""
        for item in unique_items:
            item["project_topic"] = item["project_topic"] or topic
        return MinutesOutput(summary=" ".join(summaries), topic=topic, discussion_points=points, decisions=decisions,
                             action_items=unique_items, warnings=list(dict.fromkeys(warnings)))
