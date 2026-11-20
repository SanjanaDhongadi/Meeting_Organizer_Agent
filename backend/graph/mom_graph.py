"""
LangGraph workflow that turns a stored Meet transcript into a DRAFT Minutes of Meeting.

load (DB) -> generate (MoM agent) -> persist draft + draft action items -> END, ending in
AWAITING_ORGANIZER_APPROVAL. Any failure ends in MOM_FAILED with the real error. Nothing is distributed here.
"""
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.agents.mom_agent import mom_agent
from backend.app.db import ActionItem, Meeting, MeetingMinutes, MeetingParticipant, SessionLocal
from backend.memory.audit_memory import audit_memory
from backend.runtime.governance import governed_run

logger = logging.getLogger("mom_graph")


class MoMState(TypedDict, total=False):
    meeting_id: str
    transcript: str
    meeting: Dict[str, Any]
    participants: List[Dict[str, str]]
    mom: Dict[str, Any]
    error: Optional[str]


def meeting_details(m: Meeting, participants: List[Dict[str, str]], transcript_meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "title": m.title,
        "date": (m.scheduled_start or "")[:10],
        "scheduled_start": m.scheduled_start,
        "scheduled_end": m.scheduled_end,
        "duration_minutes": m.duration_minutes,
        "actual_start": transcript_meta.get("actual_start"),
        "actual_end": transcript_meta.get("actual_end"),
        "purpose": m.purpose or "",
        "participants": participants,
    }


def node_load(state: MoMState) -> MoMState:
    db = SessionLocal()
    try:
        row = db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == state["meeting_id"]).first()
        m = db.query(Meeting).filter(Meeting.id == state["meeting_id"]).first()
        if not row or not m or not (row.transcript_text or "").strip():
            return {**state, "error": "There is no stored transcript for this meeting."}
        people = [{"name": p.name, "email": (p.email or "").lower()}
                  for p in db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == m.id).all()]
        meta = json.loads(row.transcript_meta or "{}")
        return {**state, "transcript": row.transcript_text, "participants": people,
                "meeting": meeting_details(m, people, meta), "error": None}
    finally:
        db.close()


def node_generate(state: MoMState) -> MoMState:
    output = mom_agent.execute({"transcript": state["transcript"], "participants": state["participants"],
                                "meeting": state["meeting"]})
    audit_memory.record_event("MoM Agent", "GENERATE_MOM", meeting_id=state["meeting_id"],
                              status="SUCCESS" if output.status == "SUCCESS" else "ERROR",
                              error="; ".join(output.errors),
                              details={"action_items": len(output.data.get("action_items", [])),
                                       "warnings": output.data.get("warnings", [])})
    if output.status != "SUCCESS":
        return {**state, "error": "; ".join(output.errors) or "MoM generation failed."}
    return {**state, "mom": output.data, "error": None}


def node_persist(state: MoMState) -> MoMState:
    db = SessionLocal()
    try:
        row = db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == state["meeting_id"]).first()
        m = db.query(Meeting).filter(Meeting.id == state["meeting_id"]).first()
        mom = state["mom"]
        row.mom_draft = json.dumps({
            "meeting": state["meeting"],
            "summary": mom.get("summary", ""),
            "topic": mom.get("topic", ""),
            "discussion_points": mom.get("discussion_points", []),
            "decisions": mom.get("decisions", []),
            "warnings": mom.get("warnings", []),
            "generated_by": mom.get("generated_by", ""),
        })
        db.query(ActionItem).filter(ActionItem.meeting_id == m.id, ActionItem.finalized.is_(False)).delete()
        for item in mom.get("action_items", []):
            db.add(ActionItem(
                meeting_id=m.id, organizer_id=m.organizer_id, description=item["description"],
                owner_name=item.get("owner_name") or "", owner_email=item.get("owner_email"),
                due_date=item.get("due_date"), project_topic=item.get("project_topic") or "",
                source_quote=item.get("source_quote") or "", status="NOT_STARTED", finalized=False,
            ))
        row.status = "AWAITING_ORGANIZER_APPROVAL"
        row.last_error = ""
        row.generated_at = datetime.utcnow()
        db.commit()
        return state
    finally:
        db.close()


def node_fail(state: MoMState) -> MoMState:
    db = SessionLocal()
    try:
        row = db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == state["meeting_id"]).first()
        if row:
            row.status = "MOM_FAILED"
            row.last_error = state.get("error") or "MoM generation failed."
            db.commit()
    finally:
        db.close()
    return state


def _after(state: MoMState) -> str:
    return "node_fail" if state.get("error") else "next"


def build_mom_graph():
    graph = StateGraph(MoMState)
    graph.add_node("node_load", node_load)
    graph.add_node("node_generate", node_generate)
    graph.add_node("node_persist", node_persist)
    graph.add_node("node_fail", node_fail)
    graph.add_edge(START, "node_load")
    graph.add_conditional_edges("node_load", _after, {"next": "node_generate", "node_fail": "node_fail"})
    graph.add_conditional_edges("node_generate", _after, {"next": "node_persist", "node_fail": "node_fail"})
    graph.add_edge("node_persist", END)
    graph.add_edge("node_fail", END)
    return graph.compile()


mom_graph = build_mom_graph()


def run_mom_generation(meeting_id: str) -> Dict[str, Any]:
    """Caller must already hold the GENERATING_MOM claim."""
    try:
        with governed_run("minutes", meeting_id):
            final = mom_graph.invoke({"meeting_id": meeting_id})
        return {"success": not final.get("error"), "error": final.get("error")}
    except Exception as error:
        logger.exception("MoM workflow crashed for %s", meeting_id)
        node_fail({"meeting_id": meeting_id, "error": f"MoM workflow failed: {error}"})
        return {"success": False, "error": str(error)}
