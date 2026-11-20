"""
Action-item rules shared by the API, the workflow and the worker.

PostgreSQL is the source of truth. Which tasks may be suggested for a new meeting is decided by database filters
(organizer, finalized, not completed, not deleted); relevance scoring only ranks tasks that already passed them.
"""
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.db import ActionItem, Meeting, MeetingParticipant

STATUSES = ("NOT_STARTED", "IN_PROGRESS", "COMPLETED")
FOLLOWUP_HEADER = "Follow-up on open action items:"

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "will", "have", "has", "are", "was", "were", "meeting",
    "discuss", "review", "team", "about", "into", "onto", "our", "their", "your", "its", "all", "any", "new",
    "update", "updates", "sync", "call", "check", "plan", "work", "task", "tasks", "status", "item", "items",
}


def valid_due_date(value: Any) -> Optional[str]:
    """YYYY-MM-DD or None. Raises ValueError for anything else that is not empty."""
    if value in (None, ""):
        return None
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid due date '{value}'. Use YYYY-MM-DD.")
    return str(value)


def _terms(*texts: Optional[str]) -> set:
    words = re.findall(r"[a-z0-9]{3,}", " ".join(t or "" for t in texts).lower())
    return {w for w in words if w not in _STOPWORDS}


def unfinished_items_query(db: Session, organizer_id: int, exclude_meeting_id: Optional[str] = None):
    """The database-level eligibility filter: the organizer's own, approved (finalized), unfinished, live meetings."""
    query = (
        db.query(ActionItem, Meeting)
        .join(Meeting, Meeting.id == ActionItem.meeting_id)
        .filter(
            ActionItem.organizer_id == organizer_id,
            Meeting.organizer_id == organizer_id,
            ActionItem.finalized.is_(True),
            ActionItem.status != "COMPLETED",
            Meeting.status != "DELETED",
        )
    )
    if exclude_meeting_id:
        query = query.filter(ActionItem.meeting_id != exclude_meeting_id)
    return query


def suggest_followups(db: Session, meeting: Meeting, show_all: bool = False) -> Dict[str, Any]:
    """Rank the organizer's unfinished tasks for a new meeting by topic and participant overlap."""
    import json
    parsed = json.loads(meeting.parsed_details or "{}")
    selected = {int(i) for i in parsed.get("followup_action_item_ids") or []}
    new_terms = _terms(meeting.title, meeting.purpose, parsed.get("project"), parsed.get("topic"))
    new_emails = {
        (p.email or "").lower()
        for p in db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting.id).all() if p.email
    }
    rows = unfinished_items_query(db, meeting.organizer_id, exclude_meeting_id=meeting.id).all() if meeting.organizer_id else []
    source_ids = {m.id for _, m in rows}
    source_people: Dict[str, set] = {}
    if source_ids:
        for p in db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id.in_(source_ids)).all():
            source_people.setdefault(p.meeting_id, set()).add((p.email or "").lower())

    relevant, others = [], []
    for item, source in rows:
        shared = new_terms & _terms(item.description, item.project_topic, source.title, source.purpose)
        owner_in_new = bool(item.owner_email and item.owner_email.lower() in new_emails)
        overlap = len(source_people.get(source.id, set()) & new_emails)
        reasons = []
        if shared:
            reasons.append("Same topic: " + ", ".join(sorted(shared)[:4]))
        if owner_in_new:
            reasons.append(f"Owner {item.owner_name or item.owner_email} is invited to this meeting")
        if overlap:
            reasons.append(f"{overlap} participant(s) also attended '{source.title}'")
        entry = {
            **item.to_dict(),
            "meeting_title": source.title,
            "meeting_date": source.scheduled_start,
            "reasons": reasons,
            "score": (3 if shared else 0) + (2 if owner_in_new else 0) + (1 if overlap else 0),
            "selected": item.id in selected,
        }
        (relevant if (shared or owner_in_new) else others).append(entry)
    order = lambda e: (-e["score"], e["due_date"] or "9999-12-31", e["id"])
    relevant.sort(key=order)
    others.sort(key=order)
    return {
        "relevant": relevant,
        "others": others if show_all else [],
        "other_count": len(others),
        "total_unfinished": len(relevant) + len(others),
        "selected_ids": sorted(selected),
    }


def strip_followups(agenda: Optional[str]) -> str:
    """The organizer's own agenda, without the generated follow-up section."""
    text = agenda or ""
    return text.split(FOLLOWUP_HEADER)[0].rstrip()


def followup_section(db: Session, organizer_id: Optional[int], item_ids: Iterable[int], exclude_meeting_id: Optional[str] = None) -> str:
    """Agenda text for the tasks the organizer chose (only those still eligible and unfinished)."""
    ids = [int(i) for i in item_ids or []]
    if not ids or not organizer_id:
        return ""
    rows = [(i, m) for i, m in unfinished_items_query(db, organizer_id, exclude_meeting_id).all() if i.id in ids]
    if not rows:
        return ""
    lines = [FOLLOWUP_HEADER]
    for item, source in sorted(rows, key=lambda r: ids.index(r[0].id)):
        owner = item.owner_name or item.owner_email or "owner not specified"
        due = f", due {item.due_date}" if item.due_date else ""
        lines.append(f"- {item.description} ({owner}{due}; {item.status.replace('_', ' ').lower()}; from '{source.title}')")
    return "\n".join(lines)


def compose_agenda(db: Session, meeting: Meeting, base_agenda: str) -> str:
    """base_agenda (the organizer's) first, selected follow-up tasks appended after it, never replacing it."""
    import json
    parsed = json.loads(meeting.parsed_details or "{}")
    section = followup_section(db, meeting.organizer_id, parsed.get("followup_action_item_ids") or [], meeting.id)
    base = strip_followups(base_agenda)
    return f"{base}\n\n{section}".strip() if section else base
