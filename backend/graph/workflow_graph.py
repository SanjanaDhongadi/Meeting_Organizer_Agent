import copy
import functools
from datetime import datetime
import json
import time
import uuid
import logging
from typing import Dict, Any, List, Literal, Optional
from langgraph.graph import StateGraph, START, END

from backend.agents.venue_coordinator import active_request, check_venue_availability, local_when
from backend.graph.state import MeetingWorkflowState
from backend.agents.coordinator_agent import MeetingCoordinator
from backend.tools.registry import tool_registry
from backend.skills.scheduling_skill import availability_status
from backend.runtime.governance import count_step, governed_run
from backend.skills.plan_skill import PlanSkill, plan_differences
from backend.skills.format_skill import FormatMeetingSkill
from backend.memory.audit_memory import audit_memory
from backend.memory.long_term_memory import long_term_memory
from backend.memory.session_memory import session_memory
from backend.app.config import settings
from worker.state.state_manager import state_manager

logger = logging.getLogger("workflow_graph")

coordinator = MeetingCoordinator()

# Statuses that mean "the plan is waiting for the human gate / for more information".
PRE_APPROVAL_STATUSES = {"DRAFT", "NEEDS_INFORMATION", "WAITING_FOR_HUMAN_APPROVAL", "RESCHEDULING_REQUIRED"}


class WorkflowError(Exception):
    """Raised when a LangGraph node fails; identifies the node and meeting so the error is actionable."""

    def __init__(self, node: str, meeting_id: Optional[str], cause: Exception):
        self.node = node
        self.meeting_id = meeting_id
        self.cause = cause
        super().__init__(f"LangGraph node '{node}' failed for meeting {meeting_id or '(new)'}: {type(cause).__name__}: {cause}")


def workflow_node(func):
    """Wrap a node: record the transition in the state trace and attribute failures to the node."""
    @functools.wraps(func)
    def wrapper(state: MeetingWorkflowState) -> MeetingWorkflowState:
        started = time.monotonic()
        try:
            count_step()   # Lab 6: step/time budget of the governed run (no-op outside one); failures become WorkflowError
            result = func(state)
        except WorkflowError:
            raise
        except Exception as error:
            meeting_id = state.get("meeting_id")
            logger.exception("LangGraph node %s failed (meeting %s)", func.__name__, meeting_id)
            audit_memory.record_event("LangGraph", "NODE_FAILED", meeting_id=meeting_id, status="ERROR",
                                      error=f"{type(error).__name__}: {error}",
                                      details={"node": func.__name__, "status_before": state.get("status")})
            raise WorkflowError(func.__name__, meeting_id, error) from error
        trace = list(result.get("trace") or [])
        trace.append({"node": func.__name__, "status": result.get("status")})
        out = {**result, "trace": trace}
        _audit_node_completed(func.__name__, state, out, started)
        return out
    return wrapper


def _audit_node_completed(node: str, before: MeetingWorkflowState, after: MeetingWorkflowState, started: float) -> None:
    """Per-step audit: which node ran, status change, WHICH top-level keys changed (never their values), duration."""
    try:
        changed = sorted(k for k in set(before) | set(after) if k != "trace" and before.get(k) != after.get(k))
        audit_memory.record_event("LangGraph", "NODE_COMPLETED", meeting_id=after.get("meeting_id") or before.get("meeting_id"), details={
            "node": node, "status_before": before.get("status"), "status_after": after.get("status"),
            "changed_keys": changed, "duration_ms": int((time.monotonic() - started) * 1000)})
    except Exception:
        logger.exception("Could not record NODE_COMPLETED for %s", node)


# ----------------- PERSISTENCE HELPERS (existing save_meeting_state tool / state_manager) -----------------

def _save(**fields) -> Dict[str, Any]:
    res = tool_registry.get("save_meeting_state").execute(**fields)
    if not res.success:
        raise RuntimeError(res.error)
    return res.data


def _workflow_snapshot(state: MeetingWorkflowState) -> Dict[str, Any]:
    rag = state.get("rag_context") or {}
    return {
        "participants": state.get("participants", []),
        "unknown_participants": state.get("unknown_participants", []),
        "ambiguous_participants": state.get("ambiguous_participants", []),
        "alternative_slots": state.get("alternative_slots", []),
        "requested_slot": state.get("requested_slot"),
        "requested_slot_busy": state.get("requested_slot_busy", False),
        "participant_calendar_statuses": state.get("participant_calendar_statuses", {}),
        "organizer_availability": state.get("organizer_availability"),
        "resource_info": state.get("resource_info", {}),
        "venue_check": state.get("venue_check"),
        "organizer_email": state.get("organizer_email"),
        "execution_steps": state.get("execution_steps") or {},
        "execution": state.get("execution_result", {}),
        "execution_error": state.get("execution_error"),
        "meet_error": state.get("meet_error", ""),
        "warnings": state.get("warnings", []),
        "rag_context": {
            "relevant_employees": [
                {"name": e["profile"]["name"], "email": e["profile"]["email"], "similarity": e["similarity"]}
                for e in rag.get("relevant_employees", [])
            ],
            "historical_meetings": [
                {k: m.get(k) for k in ("meeting_id", "title", "purpose", "similarity")}
                for m in rag.get("historical_meetings", [])
            ],
            "participant_preferences": rag.get("participant_preferences", {}),
        },
        # Accumulated path across runs of the same meeting (start, edits, approval, responses).
        "trace": (((state.get("parsed_details") or {}).get("workflow") or {}).get("trace", [])
                  + [t["node"] for t in state.get("trace", [])])[-40:],
    }


def _persist_workflow(state: MeetingWorkflowState, **fields) -> None:
    parsed = dict(state.get("parsed_details") or {})
    parsed["workflow"] = _workflow_snapshot(state)
    if state.get("validation") is not None:
        parsed["validation"] = state.get("validation")
    _save(meeting_id=state["meeting_id"], parsed_details=parsed, **fields)


def _execution_steps(meeting_id: str) -> Dict[str, Any]:
    """Completed external actions of the current approval, as last persisted (crash-recovery checkpoints)."""
    workflow = ((_db_meeting(meeting_id) or {}).get("parsed_details") or {}).get("workflow") or {}
    return dict(workflow.get("execution_steps") or {})


def _mark_execution_step(meeting_id: str, step: str, **fields) -> Dict[str, Any]:
    """Persist 'step is done' right after the external action succeeded. Reads the latest parsed_details and
    changes only parsed_details["workflow"]["execution_steps"]."""
    parsed = (_db_meeting(meeting_id) or {}).get("parsed_details") or {}
    workflow = parsed.setdefault("workflow", {})
    steps = dict(workflow.get("execution_steps") or {})
    steps[step] = {"done": True, **fields, "at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}
    workflow["execution_steps"] = steps
    _save(meeting_id=meeting_id, parsed_details=parsed)
    return steps


def clear_execution_steps(meeting_id: str) -> None:
    """A fresh approval (APPROVED -> EXECUTING) starts with no completed steps."""
    parsed = (_db_meeting(meeting_id) or {}).get("parsed_details") or {}
    workflow = parsed.get("workflow") or {}
    if workflow.get("execution_steps"):
        workflow["execution_steps"] = {}
        _save(meeting_id=meeting_id, parsed_details=parsed)


def _db_meeting(meeting_id: str) -> Optional[Dict[str, Any]]:
    return state_manager.load_meeting(meeting_id)


# ----------------- GRAPH NODES -----------------

@workflow_node
def node_intake(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state.get("meeting_id") or f"meet-{uuid.uuid4().hex[:10]}"
    raw_request = state.get("raw_request", "")
    organizer_id = state.get("organizer_id")
    audit_memory.record_event("Meeting Coordinator", "INTAKE", meeting_id=meeting_id, details={"request": raw_request})
    session_memory.init_session(meeting_id, raw_request)
    # The meeting exists as a DRAFT from the first step so every later step works on the same persisted entity.
    _save(meeting_id=meeting_id, organizer_id=organizer_id, title="Draft meeting", raw_request=raw_request, status="DRAFT", approval_status="PENDING")
    return {**state, "meeting_id": meeting_id, "status": "DRAFT"}

@workflow_node
def node_parse_request(state: MeetingWorkflowState) -> MeetingWorkflowState:
    parsed = coordinator.parse_natural_language_request(state.get("raw_request", ""))
    meeting_id = state["meeting_id"]
    audit_memory.record_event("Meeting Coordinator", "PARSE_REQUEST", meeting_id=meeting_id, details=parsed)
    return {
        **state,
        "parsed_details": parsed,
        "mode": parsed.get("mode") or "",
        "status": "PARSED"
    }

@workflow_node
def node_identify_participants(state: MeetingWorkflowState) -> MeetingWorkflowState:
    parsed = state.get("parsed_details", {})
    pending = state.get("pending_participant_names")
    p_names = pending if pending is not None else parsed.get("participants", [])
    output = coordinator.participant_agent.execute({"parsed_participants": p_names})
    meeting_id = state["meeting_id"]
    audit_memory.record_event("Participant Agent", "IDENTIFY_PARTICIPANTS", meeting_id=meeting_id, details=output.data)

    resolved = output.data.get("resolved_participants", [])
    if pending is not None:
        known = {p["email"].lower() for p in state.get("participants", []) if p.get("email")}
        resolved = state.get("participants", []) + [p for p in resolved if p.get("email", "").lower() not in known]
    return {
        **state,
        "participants": resolved,
        "unknown_participants": [],
        "ambiguous_participants": [],
        "pending_participant_names": None,
        "status": "PARTICIPANTS_IDENTIFIED"
    }

@workflow_node
def node_retrieve_context(state: MeetingWorkflowState) -> MeetingWorkflowState:
    """Lab 4 RAG: retrieve employee/history context and place it in agent state for the specialist agents."""
    meeting_id = state["meeting_id"]
    participants = state.get("participants", [])
    emails = [p["email"] for p in participants if p.get("email")]
    query = " ".join([state.get("raw_request", "")] + [p.get("name", "") for p in participants])
    rag_context = long_term_memory.search_context(query, exclude_meeting_id=meeting_id, participant_emails=emails)

    # Stored preferences/working time from long-term memory become part of each participant's working profile.
    prefs = rag_context.get("participant_preferences", {})
    enriched = []
    for p in participants:
        memory = prefs.get((p.get("email") or "").lower())
        if memory:
            p = {**p, "preferences": memory["preferences"], "working_days": memory["working_days"],
                 "working_hours": memory["working_hours"], "timezone": memory["timezone"],
                 "mode_preference": memory.get("mode_preference"), "context_source": "long_term_memory"}
        enriched.append(p)

    audit_memory.record_event("Long-Term Memory", "RETRIEVE_CONTEXT", meeting_id=meeting_id, details={
        "relevant_employees": [e["profile"]["email"] for e in rag_context.get("relevant_employees", [])],
        "historical_meetings": [m["meeting_id"] for m in rag_context.get("historical_meetings", [])],
        "participant_preferences_for": list(prefs.keys()),
    })
    return {**state, "rag_context": rag_context, "participants": enriched, "status": "CONTEXT_RETRIEVED"}

@workflow_node
def node_ask_for_information(state: MeetingWorkflowState) -> MeetingWorkflowState:
    """Pause the SAME meeting and ask the organizer only for what is missing or ambiguous (nothing is assumed)."""
    meeting_id = state["meeting_id"]
    parsed = state.get("parsed_details", {})
    asks = [q["question"] for q in parsed.get("questions", [])]
    msg = "Action Required: " + " ".join(asks)
    merged = {
        "participants": state.get("participants", []),
        "unknown_participants": [],
        "ambiguous_participants": [],
        "resource_info": state.get("resource_info", {}),
        "mode": state.get("mode", "ONLINE"),
    }
    conflicts = coordinator.detect_and_resolve_conflicts(merged, {"is_consistent": True, "critique_notes": []})
    audit_memory.record_event("Meeting Coordinator", "ASK_FOR_INFORMATION", meeting_id=meeting_id, status="WAITING", details={"message": msg})
    new_state = {**state, "status": "NEEDS_INFORMATION", "error_message": msg, "conflicts": conflicts,
                 "approval_status": "PENDING", "critique_notes": []}
    # Nothing is scheduled; the draft waits (same meeting) until the organizer answers.
    _persist_workflow(
        new_state,
        title=parsed.get("title") or "",
        status="NEEDS_INFORMATION",
        mode=parsed.get("mode") or "",
        scheduled_start=parsed.get("start_time") or "",
        scheduled_end=parsed.get("end_time") or "",
        duration_minutes=parsed.get("duration_minutes") or 0,
        purpose=parsed.get("purpose") or "",
        room_name=parsed.get("room_name") or "",
        approval_status="PENDING",
        conflicts=conflicts,
        participants=state.get("participants", []),
    )
    return new_state

@workflow_node
def node_check_availability(state: MeetingWorkflowState) -> MeetingWorkflowState:
    """Lab 2: read real calendar free/busy through the Scheduling agent's typed tools (MCP -> Google Calendar).

    The organizer and every participant whose Google Calendar is connected are checked with their own authorized
    credentials. Anyone else (external, unregistered, not connected, OAuth failure) is UNVERIFIED: never assumed free,
    never blocking, and still invited by email.
    """
    meeting_id = state["meeting_id"]
    participants = state.get("participants", [])
    parsed = state.get("parsed_details", {})
    locked = state.get("scheduling_slot") if state.get("slot_locked") else None
    start = (locked or {}).get("start") or parsed.get("start_time")
    end = (locked or {}).get("end") or parsed.get("end_time")

    # Lab 3: lock the complete details into a versioned plan (an organizer edit re-locks a new version).
    plan = PlanSkill().run({
        "title": parsed.get("title"), "purpose": parsed.get("purpose"), "date": parsed.get("date"),
        "start_time": parsed.get("start_time"), "end_time": parsed.get("end_time"),
        "duration_minutes": parsed.get("duration_minutes"), "mode": state.get("mode") or parsed.get("mode"),
        "room_name": parsed.get("room_name"), "venue_coordinator_email": parsed.get("venue_coordinator_email"),
        "participants": [{"name": p.get("name") or "", "email": p.get("email") or ""} for p in participants],
        "agenda_points": parsed.get("agenda_points") or [], "previous_plan": parsed.get("plan"),
    })
    parsed = {**parsed, "plan": plan.model_dump(mode="json")}
    state = {**state, "parsed_details": parsed}
    audit_memory.record_event("Meeting Coordinator", "LOCK_PLAN", meeting_id=meeting_id,
                              details={"version": plan.version, "plan_hash": plan.plan_hash})

    emails = [p["email"] for p in participants if p.get("email")]
    organizer = (state.get("organizer_email") or "").strip().lower()
    check_emails = list(emails)
    profiles = list(participants)
    if organizer and organizer not in {e.lower() for e in emails}:
        check_emails.append(organizer)
        profiles.append({"name": "The organizer", "email": organizer, "is_external": True})

    output = coordinator.scheduling_agent.execute({
        "participant_emails": check_emails,
        "target_date": (start or "")[:10] or parsed.get("date"),
        "target_start_time": start,
        "target_end_time": end,
        "duration_minutes": parsed.get("duration_minutes", 30),
        "resolved_participants": profiles,
        "meeting_id": meeting_id,
    })
    statuses = output.data.get("participant_statuses", {})
    # Only a real calendar result may say AVAILABLE or BUSY; everything else is UNVERIFIED.
    updated_participants = [
        {**p, "calendar_status": availability_status(statuses.get(p.get("email"))),
         "availability_reason": (statuses.get(p.get("email")) or {}).get("reason", "")}
        for p in participants
    ]
    organizer_availability = None
    if organizer:
        organizer_availability = {
            "email": organizer,
            "status": availability_status(statuses.get(organizer)),
            "reason": (statuses.get(organizer) or {}).get("reason", ""),
        }

    audit_memory.record_event(
        "Scheduling Agent", "CHECK_AVAILABILITY", meeting_id=meeting_id,
        details={**output.data, "organizer_availability": organizer_availability},
    )

    requested = output.data.get("requested_slot") or {"start": start, "end": end}
    return {
        **state,
        "participants": updated_participants,
        "organizer_availability": organizer_availability,
        "requested_slot": requested,
        "requested_slot_busy": output.data.get("requested_slot_busy", False),
        "scheduling_slot": requested,
        "alternative_slots": output.data.get("alternative_slots", []),
        "participant_calendar_statuses": statuses,
        "scheduling_conflicts": output.data.get("conflicts", []),
        "status": "AVAILABILITY_CHECKED"
    }

@workflow_node
def node_find_alternative_slot(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    alts = state.get("alternative_slots", [])
    selected = alts[0] if alts else None
    conflicts = []
    for c in state.get("scheduling_conflicts", []):
        if c.get("severity") == "HIGH" and selected:
            # The busy requested slot is resolved by proposing the alternative; the human still decides at the gate.
            c = {**c, "severity": "RESOLVED",
                 "resolution": f"Requested slot is busy; alternative {selected['start']} – {selected['end']} proposed for approval."}
        conflicts.append(c)
    audit_memory.record_event("Scheduling Agent", "FIND_ALTERNATIVE_SLOT", meeting_id=meeting_id,
                              status="SUCCESS" if selected else "WARNING",
                              details={"requested": state.get("requested_slot"), "selected_alternative": selected})
    participants = state.get("participants", [])
    if selected:
        # Show each attendee's status for the slot that is actually proposed, not for the busy requested one.
        verified = {e.lower() for e in selected.get("verified_for", [])}
        participants = [{**p, "calendar_status": "AVAILABLE" if p["email"].lower() in verified else "UNVERIFIED",
                         "availability_reason": "Free at the proposed alternative time." if p["email"].lower() in verified
                         else "Could not be verified for the proposed alternative time."} for p in participants]
        organizer = state.get("organizer_availability")
        if organizer:
            ok = organizer["email"].lower() in verified
            state = {**state, "organizer_availability": {
                **organizer, "status": "AVAILABLE" if ok else "UNVERIFIED",
                "reason": "Free at the proposed alternative time." if ok else "Could not be verified for the proposed alternative time."}}
    return {**state, "scheduling_slot": selected, "scheduling_conflicts": conflicts, "participants": participants,
            "organizer_availability": state.get("organizer_availability"), "status": "ALTERNATIVE_SLOT_SELECTED"}

def _with_followups(meeting_id: str, agenda_text: str) -> str:
    from backend.app.action_items import compose_agenda
    from backend.app.db import SessionLocal, Meeting
    db = SessionLocal()
    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        return compose_agenda(db, meeting, agenda_text) if meeting else agenda_text
    finally:
        db.close()


@workflow_node
def node_prepare_agenda(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    parsed = state.get("parsed_details", {})
    if parsed.get("agenda_points") is None and state.get("agenda_locked") and state.get("agenda"):
        # Older meeting with a free-text agenda edited by the organizer: keep the human's version.
        audit_memory.record_event("Agenda Agent", "PREPARE_AGENDA", meeting_id=meeting_id, details={"kept_organizer_agenda": True})
        return {**state, "has_purpose": bool(parsed.get("purpose")), "status": "AGENDA_PREPARED"}
    # Convert rough English topics into clear, professional, logically structured agenda
    output = coordinator.agenda_agent.execute({
        "purpose": parsed.get("purpose", ""),
        "agenda_points": parsed.get("agenda_points") or [],
        "title": parsed.get("title", ""),
        "raw_request": state.get("raw_request", ""),
    })

    # The organizer's agenda stays primary; follow-up tasks they chose are appended after it.
    agenda_text = _with_followups(meeting_id, output.data.get("agenda", ""))
    _save(meeting_id=meeting_id, agenda=agenda_text)
    audit_memory.record_event("Agenda Agent", "PREPARE_AGENDA", meeting_id=meeting_id, details=output.data)
    return {
        **state,
        "agenda": agenda_text,
        "has_purpose": output.data.get("has_purpose", False),
        "status": "AGENDA_PREPARED"
    }

@workflow_node
def node_prepare_resource(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    parsed = state.get("parsed_details", {})
    slot = state.get("scheduling_slot") or {}
    output = coordinator.resource_agent.execute({
        "mode": state.get("mode", "ONLINE"),
        "resolved_participants": state.get("participants", []),
        "purpose": parsed.get("purpose", ""),
        "room_name": parsed.get("room_name", ""),
        "equipment": parsed.get("equipment", ""),
        "meeting_id": meeting_id,
        "target_start_time": slot.get("start"),
        "target_end_time": slot.get("end")
    })

    audit_memory.record_event("Resource Agent", "PREPARE_RESOURCE", meeting_id=meeting_id, details=output.data)
    venue_check = None
    if (state.get("mode") or "").upper() == "OFFLINE" and slot.get("start") and slot.get("end"):
        # Check only for conflicting offline meetings in this application.
        venue = (output.data or {}).get("room_name") or parsed.get("room_name", "")
        if venue:
            venue_check = check_venue_availability(venue, slot["start"], slot["end"], meeting_id)
            audit_memory.record_event("Meeting Coordinator", "CHECK_VENUE_CONFLICTS", meeting_id=meeting_id,
                                      status="ERROR" if venue_check["status"] == "BUSY" else "SUCCESS",
                                      details=venue_check)
    return {
        **state,
        "resource_info": output.data,
        "venue_check": venue_check,
        "status": "RESOURCE_PREPARED"
    }

@workflow_node
def node_validate(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    slot = state.get("scheduling_slot") or {}
    val_out = coordinator.validation_agent.execute({
        "draft_plan": {
            "mode": state.get("mode", "ONLINE"),
            "start_time": slot.get("start"),
            "room_name": (state.get("resource_info") or {}).get("room_name")
        },
        "resolved_participants": state.get("participants", []),
        "mode": state.get("mode", "ONLINE"),
        "resource_data": state.get("resource_info", {})
    })
    v_data = val_out.data

    # Lab 3: differences between the draft and the locked plan are warnings only (they never block approval).
    locked_plan = (state.get("parsed_details") or {}).get("plan")
    if locked_plan:
        diffs = plan_differences(locked_plan, slot.get("start"), slot.get("end"), state.get("mode"),
                                 (state.get("resource_info") or {}).get("room_name"),
                                 [p.get("email") for p in state.get("participants", [])])
        v_data = {**v_data, "warnings": list(v_data.get("warnings", [])) +
                  [f"Draft differs from locked plan v{locked_plan.get('version')}: {d}" for d in diffs]}

    # Lab 8 merge -> critique (reads RAG preferences from state) -> conflict detection & resolution.
    merged = {
        "participants": state.get("participants", []),
        "unknown_participants": state.get("unknown_participants", []),
        "ambiguous_participants": state.get("ambiguous_participants", []),
        "scheduling_slot": slot,
        "scheduling_conflicts": state.get("scheduling_conflicts", []),
        "resource_info": state.get("resource_info", {}),
        "venue_check": state.get("venue_check"),
        "mode": state.get("mode", "ONLINE"),
    }
    critique_res = coordinator.critique(merged, state)
    conflicts = coordinator.detect_and_resolve_conflicts(merged, critique_res)
    audit_memory.record_event("Meeting Coordinator", "CRITIQUE", meeting_id=meeting_id,
                              status="SUCCESS" if critique_res["is_consistent"] else "WARNING", details=critique_res)
    for c in conflicts:
        audit_memory.record_event("Meeting Coordinator", "CONFLICT_DETECTED", meeting_id=meeting_id, status="WARNING", details=c)
    audit_memory.record_event("Validation Agent", "VALIDATE", meeting_id=meeting_id, status=v_data.get("validation_status", "PASS"), details=v_data)
    return {
        **state,
        "validation": v_data,
        "validation_status": v_data.get("validation_status", "PASS"),
        "validation_errors": v_data.get("errors", []),
        "validation_warnings": v_data.get("warnings", []),
        "conflicts": conflicts,
        "critique_notes": critique_res.get("critique_notes", []),
        "status": "VALIDATED"
    }

@workflow_node
def node_create_draft_plan(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    parsed = state.get("parsed_details", {})
    slot = state.get("scheduling_slot") or {}
    conflicts = state.get("conflicts", [])
    validation = state.get("validation") or {}

    has_blockers = any(c.get("status") == "UNRESOLVED_BLOCKER" for c in conflicts) or not validation.get("can_proceed_to_approval", False)
    needs_purpose = not state.get("has_purpose") and not state.get("skip_agenda") and not state.get("agenda")
    if has_blockers:
        status = "RESCHEDULING_REQUIRED"
    elif needs_purpose:
        status = "DRAFT"  # purpose missing: organizer must give one or skip the agenda
    else:
        status = "WAITING_FOR_HUMAN_APPROVAL"

    resource = state.get("resource_info") or {}
    meet_url = ""
    calendar_event_id = ""
    meet_error = ""
    if status == "WAITING_FOR_HUMAN_APPROVAL" and (state.get("mode") or "").upper() != "OFFLINE":
        participants = state.get("participants", [])
        emails = [p.get("email") for p in participants if p.get("email")]
        organizer = _organizer_for(state, emails)
        if organizer or settings.DEMO_MODE:
            existing = _db_meeting(meeting_id) or {}
            cal_res = tool_registry.get("create_calendar_event").execute(
                title=parsed.get("title") or state.get("title") or "Project Meeting",
                start_time=slot.get("start") or "", end_time=slot.get("end") or "",
                attendees=[], description="", is_online=True, organizer_email=organizer,
                event_id=existing.get("calendar_event_id") or None,
                meeting_id=meeting_id, send_updates="none",
            )
            if cal_res.success:
                cal_data = cal_res.data or {}
                meet_url = cal_data.get("meet_url") or ""
                calendar_event_id = cal_data.get("event_id") or ""
                meet_error = cal_data.get("meet_error") or ""
                _save(meeting_id=meeting_id, calendar_event_id=calendar_event_id, meet_url=meet_url)
            else:
                meet_error = cal_res.error or "Google Calendar could not create the draft meeting or Meet link."
        else:
            meet_error = "Connect a Google account to create a Google Meet link."
        if meet_error:
            audit_memory.record_event("Meeting Coordinator", "PREPARE_MEET_LINK", meeting_id=meeting_id,
                                      status="WARNING", error=meet_error)
    else:
        stored = _db_meeting(meeting_id) or {}
        meet_url = stored.get("meet_url") or ""
        calendar_event_id = stored.get("calendar_event_id") or ""
    plan = {
        "meeting_id": meeting_id,
        "title": parsed.get("title") or state.get("title") or "",
        "mode": state.get("mode", "ONLINE"),
        "scheduled_start": slot.get("start"),
        "scheduled_end": slot.get("end"),
        "duration_minutes": parsed.get("duration_minutes", 30),
        "purpose": parsed.get("purpose", ""),
        "agenda": state.get("agenda", ""),
        "participants": state.get("participants", []),
        "room_name": resource.get("room_name", ""),
        "meet_url": meet_url,
        "meet_error": meet_error,
        "calendar_event_id": calendar_event_id,
        "status": status
    }

    new_state = {**state, "draft_plan": plan, "status": status, "approval_status": "PENDING", "meet_error": meet_error}
    _persist_workflow(
        new_state,
        title=plan["title"],
        status=status,
        mode=plan["mode"],
        scheduled_start=plan["scheduled_start"],
        scheduled_end=plan["scheduled_end"],
        duration_minutes=plan["duration_minutes"],
        purpose=plan["purpose"],
        agenda=plan["agenda"],
        room_name=plan["room_name"],
        approval_status="PENDING",
        conflicts=conflicts,
        critique_notes="; ".join(state.get("critique_notes", [])),
        participants=plan["participants"],
        meet_url=plan["meet_url"],
        calendar_event_id=plan["calendar_event_id"],
    )
    session_memory.update_session(meeting_id, {"status": status, "draft_plan": plan, "approval_state": "PENDING"})
    audit_memory.record_event("Meeting Coordinator", "CREATE_DRAFT_PLAN", meeting_id=meeting_id, approval_status="WAITING", details=plan)
    return new_state

@workflow_node
def node_human_approval(state: MeetingWorkflowState) -> MeetingWorkflowState:
    """Hard gate: execution is only allowed when the persisted meeting was APPROVED by a human and claimed for execution."""
    meeting_id = state["meeting_id"]
    meeting = _db_meeting(meeting_id) or {}
    approved = (
        state.get("resume_action") == "APPROVAL"
        and meeting.get("approval_status") == "APPROVED"
        and meeting.get("status") == "EXECUTING"
    )
    approval = "APPROVED" if approved else (meeting.get("approval_status") or "PENDING")
    if approval in {"EDITED"}:
        approval = "PENDING"
    audit_memory.record_event("Human Operator", "HUMAN_APPROVAL_GATE", meeting_id=meeting_id, approval_status=approval,
                              details={"notes": meeting.get("approval_notes", ""), "gate_passed": approved})
    status = "APPROVED" if approved else (state.get("status") or meeting.get("status"))
    return {**state, "approval_status": approval, "status": status}

def _execution_failure(state: MeetingWorkflowState, step: str, data: Optional[Dict[str, Any]], error: Optional[str]) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    data = data or {}
    detail = error or data.get("error") or f"{step} failed."
    if data.get("http_status"):
        detail = f"[{data.get('error_code')}] HTTP {data['http_status']}: {data.get('error_message') or detail}" + (
            f" (reason: {data['error_reason']})" if data.get("error_reason") else "")
    elif data.get("error_code"):
        detail = f"[{data['error_code']}] {detail}"
    if data.get("mcp_capability"):
        detail = f"MCP {data['mcp_capability']}: {detail}"
    execution_error = {
        "step": step, "error": detail,
        **{k: data.get(k) for k in ("error_code", "http_status", "error_reason", "error_message", "mcp_capability") if data.get(k)},
    }
    audit_memory.record_event("Meeting Coordinator", "EXECUTE", meeting_id=meeting_id, status="ERROR", error=detail, details=execution_error)
    new_state = {**state, "execution_error": execution_error, "status": "ACTION_FAILED"}
    _persist_workflow(new_state)
    state_manager.update_meeting_status(meeting_id, "ACTION_FAILED", f"{step.upper()}_FAILED", detail)
    return new_state

def _needs_room_booking(venue: str) -> bool:
    """Only catalog rooms that require facility approval go through the emailed booking request."""
    from database.seed.seed_data import SEED_ROOMS
    return any(r["name"].lower() == (venue or "").lower() and r.get("requires_approval") for r in SEED_ROOMS)

def _organizer_for(state: MeetingWorkflowState, attendees: List[str]) -> Optional[str]:
    if settings.DEMO_MODE:
        return None
    from backend.connectors.google_auth import resolve_organizer_email
    organizer_hint = state.get("organizer_email")
    if not organizer_hint and state.get("organizer_id"):
        from backend.app.db import SessionLocal, User
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.id == state["organizer_id"]).first()
            if u:
                organizer_hint = u.email
        finally:
            db.close()
    return resolve_organizer_email(attendees, organizer_hint=organizer_hint)

@workflow_node
def node_execute(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    meeting = _db_meeting(meeting_id) or {}
    # Defence in depth: never run external actions unless the persisted meeting is approved.
    if meeting.get("approval_status") != "APPROVED" or meeting.get("status") != "EXECUTING":
        raise PermissionError("Execution attempted without human approval — blocked by the approval gate.")

    mode = meeting.get("mode") or state.get("mode", "ONLINE")
    attendee_emails = [p["email"] for p in meeting.get("participants", [])]
    organizer = _organizer_for(state, attendee_emails)
    title = meeting.get("title") or "Project Meeting"
    start, end = meeting.get("scheduled_start") or "", meeting.get("scheduled_end") or ""
    warnings: List[str] = []

    venue = meeting.get("room_name") or ""
    # Online: Google Meet. In person: an invitation that names the venue in the description (no map link; the
    # venue itself is requested separately from the venue coordinator via the Venue Coordinator tab).
    is_online = mode != "OFFLINE"
    when = local_when(start, end)

    if not organizer and not settings.DEMO_MODE:
        warnings.append("Google account not connected. Meeting scheduled internally; connect Google account to synchronize Calendar and Gmail.")
        audit_memory.record_event("Meeting Coordinator", "SCHEDULED_INTERNALLY", meeting_id=meeting_id,
                                  details={"title": title, "start": start, "end": end})
        _save(meeting_id=meeting_id, approval_status="APPROVED", status="SCHEDULED")
        state_manager.update_meeting_status(meeting_id, "SCHEDULED", "INTERNAL_SCHEDULED", "Meeting scheduled internally without Google synchronization.")
        execution_result = {
            "calendar_event_id": None,
            "meet_url": None,
            "internal_only": True,
            "message": "Saved internally without Google synchronization."
        }
        new_state = {**state, "status": "SCHEDULED", "execution_result": execution_result, "warnings": warnings}
        _persist_workflow(new_state)
        return new_state

    steps = _execution_steps(meeting_id)
    if (steps.get("calendar") or {}).get("done"):
        # Crash recovery: the event of this approval already exists. Reuse it; do not recreate it or reset responses.
        data = {"event_id": steps["calendar"].get("event_id") or meeting.get("calendar_event_id"), "organizer_email": organizer}
        meet_url = (meeting.get("meet_url") or "") if is_online else ""
        audit_memory.record_event("Meeting Coordinator", "EXECUTE_STEP_SKIPPED", meeting_id=meeting_id, details={"step": "calendar"})
    else:
        cal_res = tool_registry.get("create_calendar_event").execute(
            title=title,
            start_time=start,
            end_time=end,
            attendees=attendee_emails,
            description="\n".join(filter(None, [
                f"Venue: {venue}" if not is_online and venue else "",
                f"Purpose: {meeting.get('purpose')}" if meeting.get("purpose") else "",
                f"Agenda:\n{meeting.get('agenda')}" if meeting.get("agenda") else "",
            ])),
            is_online=is_online,
            organizer_email=organizer,
            location="",
            event_id=meeting.get("calendar_event_id") or None,
            meeting_id=meeting_id,
            send_updates="all",
        )
        if not cal_res.success:
            return _execution_failure(state, "calendar_event", cal_res.data, cal_res.error)
        data = cal_res.data or {}
        steps = _mark_execution_step(meeting_id, "calendar", event_id=data.get("event_id"))
        state = {**state, "execution_steps": steps}
        meet_url = (data.get("meet_url") or "") if is_online else ""
        if is_online and data.get("meet_error"):
            warnings.append(data["meet_error"])
        _save(meeting_id=meeting_id, calendar_event_id=data.get("event_id"), meet_url=meet_url)
        state_manager.reset_participant_responses(meeting_id)

    invite = FormatMeetingSkill().run({
        "title": title, "purpose": meeting.get("purpose"), "when_date": when["date"], "when_time": when["time"],
        "is_online": is_online, "meet_url": meet_url, "venue": venue, "agenda": meeting.get("agenda"),
    }, "invite")
    if (steps.get("invitation") or {}).get("done"):
        # Crash recovery: the invitation of this approval was already sent; never send it twice.
        email_ok, email_data, email_error = True, {"message_id": steps["invitation"].get("message_id")}, None
        audit_memory.record_event("Meeting Coordinator", "EXECUTE_STEP_SKIPPED", meeting_id=meeting_id, details={"step": "invitation"})
    else:
        email_res = tool_registry.get("send_email").execute(
            to_emails=attendee_emails,
            subject=invite["subject"],
            body=invite["body"],
            meeting_id=meeting_id,
            sender_email=organizer or data.get("organizer_email"),
        )
        email_ok, email_data, email_error = bool(email_res.success), email_res.data or {}, email_res.error
        if email_ok:
            steps = _mark_execution_step(meeting_id, "invitation", message_id=email_data.get("message_id"))
            state = {**state, "execution_steps": steps}
    if not email_ok:
        warnings.append(f"Gmail invitation was NOT sent: {email_error}")
        audit_memory.record_event("Meeting Coordinator", "SEND_INVITATION_EMAIL", meeting_id=meeting_id, status="ERROR",
                                  error=email_error or "", details=email_data)
    execution_result = {
        "calendar_event_id": data.get("event_id"),
        "meet_url": meet_url,
        "calendar_provider": data.get("provider"),
        "organizer_email": data.get("organizer_email") or organizer,
        "attendees": attendee_emails,
        "email_sent": email_ok,
        "email_message_id": email_data.get("message_id"),
        "email_provider": email_data.get("provider"),
    }
    pending_room = False
    pending_participants = attendee_emails
    next_status = "WAITING_FOR_PARTICIPANTS"

    audit_memory.record_event("Meeting Coordinator", "EXECUTE", meeting_id=meeting_id,
                              status="WARNING" if warnings else "SUCCESS", details={**execution_result, "warnings": warnings})
    new_state = {
        **state,
        "execution_result": execution_result,
        "execution_error": None,
        "warnings": warnings,
        "organizer_email": execution_result.get("organizer_email"),
        "pending_participants": pending_participants,
        "pending_room": pending_room,
        "status": next_status
    }
    _persist_workflow(new_state)
    state_manager.update_meeting_status(meeting_id, next_status, "APPROVED_ACTIONS_SENT", "; ".join(warnings))
    return new_state

@workflow_node
def node_wait_for_responses(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    audit_memory.record_event("Meeting Coordinator", "WAIT_FOR_RESPONSES", meeting_id=meeting_id, status="WAITING", details={"pending_room": state.get("pending_room"), "pending_participants": state.get("pending_participants")})
    return state

@workflow_node
def node_process_responses(state: MeetingWorkflowState) -> MeetingWorkflowState:
    """Resume point for asynchronous responses: evaluate the persisted meeting (same meeting, no new workflow)."""
    meeting_id = state["meeting_id"]
    meeting = _db_meeting(meeting_id) or {}
    event = state.get("response_event") or {}
    participants = meeting.get("participants", [])
    rooms = meeting.get("room_bookings", [])
    mode = meeting.get("mode", "ONLINE")

    rejected_people = [p["email"] for p in participants if p.get("response_status") == "REJECTED"]
    all_accepted = bool(participants) and all(p.get("response_status") == "ACCEPTED" for p in participants)
    # In person: the venue coordinator's answer to the (separately sent) venue request.
    venue_request = active_request(rooms) if mode == "OFFLINE" else None
    venue_status = (venue_request or {}).get("status")
    room_pending = mode == "OFFLINE" and venue_status != "CONFIRMED"

    if rejected_people:
        outcome, next_status = "RESCHEDULE", "RESCHEDULING_REQUIRED"
        note = f"Participant {', '.join(rejected_people)} rejected the meeting invitation."
    elif venue_status == "REJECTED":
        outcome, next_status = "RESCHEDULE", "RESCHEDULING_REQUIRED"
        note = (f"Venue declined: the venue coordinator rejected the booking for {venue_request['room_name']}. "
                "Edit the time or venue, then send a new venue request.")
    elif all_accepted and mode == "OFFLINE" and venue_status != "CONFIRMED":
        outcome, next_status = "WAITING", "WAITING_FOR_AUDITORIUM_RESPONSE"
        note = ("All participants accepted. Waiting for venue confirmation"
                + (" (send the venue request from the Venue Coordinator tab)." if not venue_request else "."))
    elif all_accepted:
        outcome, next_status, note = "READY", meeting.get("status"), "All participants accepted."
    else:
        outcome, next_status, note = "WAITING", "WAITING_FOR_PARTICIPANTS", "Waiting for the remaining participant responses."

    audit_memory.record_event("Worker", "PROCESS_RESPONSES", meeting_id=meeting_id, status="SUCCESS",
                              details={"event": event, "outcome": outcome, "note": note})
    if outcome != "READY" and next_status != meeting.get("status"):
        state_manager.update_meeting_status(meeting_id, next_status, "RESPONSE_PROCESSED", note)
    return {**state, "response_outcome": outcome, "status": next_status if outcome != "READY" else "RESPONSES_PROCESSED",
            "pending_room": room_pending, "pending_participants": [p["email"] for p in participants if p.get("response_status") == "PENDING"]}

@workflow_node
def node_revalidate(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    meeting = _db_meeting(meeting_id) or {}
    val_out = coordinator.validation_agent.execute({
        "draft_plan": {"mode": meeting.get("mode"), "scheduled_start": meeting.get("scheduled_start"), "room_name": meeting.get("room_name")},
        "resolved_participants": state.get("participants") or meeting.get("participants", []),
        "mode": meeting.get("mode"),
        "resource_data": state.get("resource_info") or {"room_name": meeting.get("room_name")},
    })
    errors = list(val_out.data.get("errors", []))
    if meeting.get("approval_status") != "APPROVED":
        errors.append("Meeting is no longer approved.")
    ok = not errors
    audit_memory.record_event("Validation Agent", "REVALIDATE", meeting_id=meeting_id, status="SUCCESS" if ok else "ERROR",
                              details={**val_out.data, "errors": errors})
    if not ok:
        state_manager.update_meeting_status(meeting_id, "RESCHEDULING_REQUIRED", "REVALIDATION_FAILED", "; ".join(errors))
        return {**state, "status": "RESCHEDULING_REQUIRED", "validation_errors": errors, "response_outcome": "RESCHEDULE"}
    return {**state, "status": "REVALIDATED", "validation_errors": []}

@workflow_node
def node_finalize(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    meeting = _db_meeting(meeting_id) or {}
    warnings = list(state.get("warnings") or [])
    note = "All participants confirmed. Meeting is confirmed."

    # Last check before confirming: no attendee may already be in another CONFIRMED meeting at this time.
    emails = [p["email"] for p in meeting.get("participants", [])]
    clashes = state_manager.find_conflicting_meetings(emails, meeting.get("scheduled_start") or "", meeting.get("scheduled_end") or "",
                                                      exclude_meeting_id=meeting_id, include_tentative=False)
    if clashes:
        names = {p["email"].lower(): p["name"] for p in meeting.get("participants", [])}
        message = " ".join(f"{names.get(em, em)} is not available during the selected time slot (already confirmed for '{held[0]['title']}')."
                           for em, held in clashes.items())
        state_manager.update_meeting_status(meeting_id, "RESCHEDULING_REQUIRED", "DOUBLE_BOOKING_PREVENTED", message)
        audit_memory.record_event("Meeting Coordinator", "FINALIZE", meeting_id=meeting_id, status="ERROR", error=message)
        return {**state, "status": "RESCHEDULING_REQUIRED", "execution_error": {"step": "confirmation", "error": message,
                                                                                 "error_code": "ATTENDEE_UNAVAILABLE"}}

    if meeting.get("mode") == "OFFLINE" and not meeting.get("calendar_event_id"):
        # Older in-person meetings (room requested before invitations): send the invitation now (no Meet link).
        attendees = [p["email"] for p in meeting.get("participants", [])]
        organizer = _organizer_for(state, attendees)
        room = meeting.get("room_name", "")
        cal_res = tool_registry.get("create_calendar_event").execute(
            title=meeting.get("title") or "Meeting",
            start_time=meeting.get("scheduled_start") or "",
            end_time=meeting.get("scheduled_end") or "",
            attendees=attendees,
            description="\n".join(filter(None, [
                f"Venue: {room}" if room else "",
                f"Purpose: {meeting.get('purpose')}" if meeting.get("purpose") else "",
                f"Agenda:\n{meeting.get('agenda')}" if meeting.get("agenda") else "",
            ])),
            is_online=False,
            organizer_email=organizer,
            location="",
            event_id=meeting.get("calendar_event_id") or None,
            meeting_id=meeting_id,
        )
        if not cal_res.success:
            return _execution_failure(state, "calendar_event", cal_res.data, cal_res.error)
        _save(meeting_id=meeting_id, calendar_event_id=(cal_res.data or {}).get("event_id"))
        email_res = tool_registry.get("send_email").execute(
            to_emails=attendees,
            subject=f"Invitation: {meeting.get('title')}",
            body=f"You are invited to: {meeting.get('title')}\nWhen: {meeting.get('scheduled_start')} – {meeting.get('scheduled_end')} (UTC)\n"
                 f"Purpose: {meeting.get('purpose') or '(not provided)'}\n"
                 f"Where: {room} (booking confirmed)\n\nAgenda:\n{meeting.get('agenda') or '(no agenda provided)'}",
            meeting_id=meeting_id,
            sender_email=organizer or (cal_res.data or {}).get("organizer_email"),
        )
        if not email_res.success:
            warnings.append(f"Gmail invitation was NOT sent: {email_res.error}")
        note = f"Room {room} confirmed; calendar invitation sent to participants."

    state_manager.update_meeting_status(meeting_id, "CONFIRMED", "FINALIZE", "; ".join([note] + warnings))
    long_term_memory.record_completed_meeting(meeting_id, meeting.get("title", ""), meeting.get("purpose", ""), meeting.get("agenda", ""))
    audit_memory.record_event("Meeting Coordinator", "FINALIZE", meeting_id=meeting_id, status="WARNING" if warnings else "SUCCESS",
                              details={"note": note, "warnings": warnings})
    new_state = {**state, "status": "CONFIRMED", "warnings": warnings}
    _persist_workflow(new_state)
    return new_state

# ----------------- CONDITIONAL ROUTING -----------------

def route_entry(state: MeetingWorkflowState) -> Literal["node_intake", "node_identify_participants", "node_retrieve_context", "node_human_approval", "node_process_responses"]:
    action = (state.get("resume_action") or "START").upper()
    if action == "REPLAN" and state.get("pending_participant_names"):
        return "node_identify_participants"
    if action in {"EDIT", "REPLAN"}:
        return "node_retrieve_context"
    if action == "APPROVAL":
        return "node_human_approval"
    if action == "RESPONSE":
        return "node_process_responses"
    return "node_intake"

def route_check_information(state: MeetingWorkflowState) -> Literal["node_ask_for_information", "node_check_availability"]:
    if (state.get("parsed_details") or {}).get("missing"):
        return "node_ask_for_information"
    return "node_check_availability"

def route_check_availability(state: MeetingWorkflowState) -> Literal["node_find_alternative_slot", "node_prepare_agenda"]:
    if state.get("slot_locked"):
        return "node_prepare_agenda"  # a human-chosen slot is never moved automatically
    if state.get("requested_slot_busy") or not state.get("scheduling_slot"):
        return "node_find_alternative_slot"
    return "node_prepare_agenda"

def route_human_approval(state: MeetingWorkflowState) -> Literal["node_execute", "end"]:
    if (state.get("approval_status") or "PENDING").upper() == "APPROVED":
        return "node_execute"
    return "end"

def route_after_execute(state: MeetingWorkflowState) -> Literal["node_wait_for_responses", "node_finalize", "end"]:
    if state.get("execution_error"):
        return "end"
    if state.get("pending_room") or state.get("pending_participants"):
        return "node_wait_for_responses"
    return "node_finalize"

def route_after_responses(state: MeetingWorkflowState) -> Literal["node_revalidate", "node_wait_for_responses", "end"]:
    outcome = state.get("response_outcome")
    if outcome == "READY":
        return "node_revalidate"
    if outcome == "WAITING":
        return "node_wait_for_responses"
    return "end"

def route_after_revalidate(state: MeetingWorkflowState) -> Literal["node_finalize", "end"]:
    return "end" if state.get("status") == "RESCHEDULING_REQUIRED" else "node_finalize"

# ----------------- BUILD GRAPH -----------------

def build_workflow_graph():
    workflow = StateGraph(MeetingWorkflowState)

    workflow.add_node("node_intake", node_intake)
    workflow.add_node("node_parse_request", node_parse_request)
    workflow.add_node("node_identify_participants", node_identify_participants)
    workflow.add_node("node_retrieve_context", node_retrieve_context)
    workflow.add_node("node_ask_for_information", node_ask_for_information)
    workflow.add_node("node_check_availability", node_check_availability)
    workflow.add_node("node_find_alternative_slot", node_find_alternative_slot)
    workflow.add_node("node_prepare_agenda", node_prepare_agenda)
    workflow.add_node("node_prepare_resource", node_prepare_resource)
    workflow.add_node("node_validate", node_validate)
    workflow.add_node("node_create_draft_plan", node_create_draft_plan)
    workflow.add_node("node_human_approval", node_human_approval)
    workflow.add_node("node_execute", node_execute)
    workflow.add_node("node_wait_for_responses", node_wait_for_responses)
    workflow.add_node("node_process_responses", node_process_responses)
    workflow.add_node("node_revalidate", node_revalidate)
    workflow.add_node("node_finalize", node_finalize)

    # Entry: new request, or resume the SAME persisted meeting at the right step.
    workflow.add_conditional_edges(START, route_entry, {
        "node_intake": "node_intake",
        "node_identify_participants": "node_identify_participants",
        "node_retrieve_context": "node_retrieve_context",
        "node_human_approval": "node_human_approval",
        "node_process_responses": "node_process_responses",
    })
    workflow.add_edge("node_intake", "node_parse_request")
    workflow.add_edge("node_parse_request", "node_identify_participants")
    workflow.add_edge("node_identify_participants", "node_retrieve_context")

    workflow.add_conditional_edges("node_retrieve_context", route_check_information, {
        "node_ask_for_information": "node_ask_for_information",
        "node_check_availability": "node_check_availability"
    })
    workflow.add_edge("node_ask_for_information", END)

    workflow.add_conditional_edges("node_check_availability", route_check_availability, {
        "node_find_alternative_slot": "node_find_alternative_slot",
        "node_prepare_agenda": "node_prepare_agenda"
    })
    workflow.add_edge("node_find_alternative_slot", "node_prepare_agenda")
    workflow.add_edge("node_prepare_agenda", "node_prepare_resource")
    workflow.add_edge("node_prepare_resource", "node_validate")
    workflow.add_edge("node_validate", "node_create_draft_plan")
    workflow.add_edge("node_create_draft_plan", "node_human_approval")

    # Hard gate: only an APPROVED (persisted) decision continues to execution; otherwise the run pauses here.
    workflow.add_conditional_edges("node_human_approval", route_human_approval, {
        "node_execute": "node_execute",
        "end": END
    })

    workflow.add_conditional_edges("node_execute", route_after_execute, {
        "node_wait_for_responses": "node_wait_for_responses",
        "node_finalize": "node_finalize",
        "end": END,
    })

    workflow.add_edge("node_wait_for_responses", END)
    workflow.add_conditional_edges("node_process_responses", route_after_responses, {
        "node_revalidate": "node_revalidate",
        "node_wait_for_responses": "node_wait_for_responses",
        "end": END,
    })
    workflow.add_conditional_edges("node_revalidate", route_after_revalidate, {
        "node_finalize": "node_finalize",
        "end": END,
    })
    workflow.add_edge("node_finalize", END)

    return workflow.compile()

workflow_app = build_workflow_graph()


# ----------------- LIVE ENTRY POINTS (used by the runtime, API and worker) -----------------

def _run(initial: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = initial.get("meeting_id")
    before = _db_meeting(meeting_id) if meeting_id else None
    prior = (((before or {}).get("parsed_details") or {}).get("workflow") or {}).get("trace", [])
    with governed_run("scheduling", meeting_id) as run:
        final_state = workflow_app.invoke(initial, {"recursion_limit": run.budget.max_steps})
    # Record the complete node path of this run (nodes may persist before they finish).
    after = _db_meeting(final_state["meeting_id"])
    if after:
        parsed = after.get("parsed_details") or {}
        workflow = parsed.get("workflow") or {}
        workflow["trace"] = (prior + [t["node"] for t in final_state.get("trace", [])])[-40:]
        parsed["workflow"] = workflow
        _save(meeting_id=final_state["meeting_id"], parsed_details=parsed)
    return final_state


def start_meeting_workflow(
    raw_request: str,
    meeting_id: Optional[str] = None,
    skip_agenda: bool = False,
    organizer_id: Optional[int] = None,
    organizer_email: Optional[str] = None
) -> MeetingWorkflowState:
    return _run({
        "resume_action": "START",
        "raw_request": raw_request,
        "meeting_id": meeting_id or f"meet-{uuid.uuid4().hex[:10]}",
        "skip_agenda": skip_agenda,
        "organizer_id": organizer_id,
        "organizer_email": organizer_email,
        "trace": []
    })


def _state_from_db(meeting: Dict[str, Any]) -> MeetingWorkflowState:
    parsed = copy.deepcopy(meeting.get("parsed_details") or {})
    snapshot = parsed.get("workflow") or {}
    profiles = {p["email"].lower(): p for p in snapshot.get("participants", []) if p.get("email")}
    source_participants = meeting.get("participants") or parsed.get("participants") or []
    participants = []
    for p in source_participants:
        p_name = p.get("name") if isinstance(p, dict) else str(p)
        p_email = p.get("email") if isinstance(p, dict) else ""
        prof = profiles.get(p_email.lower()) if p_email else None
        if not prof:
            prof = {
                "name": p_name or (p_email.split("@")[0].capitalize() if p_email else "Attendee"),
                "email": p_email,
                "is_external": False,
                "calendar_status": availability_status(p.get("calendar_status") if isinstance(p, dict) else None),
                "response_status": p.get("response_status", "PENDING") if isinstance(p, dict) else "PENDING",
            }
        else:
            prof = {**prof, "calendar_status": availability_status(prof.get("calendar_status")), "response_status": p.get("response_status", "PENDING") if isinstance(p, dict) else "PENDING"}
        participants.append(prof)
    # The persisted plan (possibly edited by the organizer) is the source of truth on resume.
    if meeting.get("scheduled_start"):
        local = datetime.fromisoformat(meeting["scheduled_start"].replace("Z", "+00:00")).astimezone(coordinator._tz())
        parsed["date"], parsed["start_clock"] = local.strftime("%Y-%m-%d"), local.strftime("%H:%M")
        parsed["end_clock"] = None
        parsed.setdefault("ambiguities", {}).pop("date", None)
        parsed["ambiguities"].pop("start_time", None)
    parsed.update({
        "title": meeting.get("title") or parsed.get("title"),
        "duration_minutes": meeting.get("duration_minutes") or parsed.get("duration_minutes"),
        "purpose": meeting.get("purpose") or parsed.get("purpose"),
        "room_name": meeting.get("room_name") or parsed.get("room_name", ""),
        "mode": meeting.get("mode") or parsed.get("mode"),
    })
    parsed = coordinator.complete_details(parsed)
    return {
        "meeting_id": meeting["id"],
        "raw_request": meeting.get("raw_request", ""),
        "status": meeting.get("status"),
        "mode": parsed.get("mode") or "",
        "title": meeting.get("title"),
        "parsed_details": parsed,
        "participants": participants,
        "unknown_participants": snapshot.get("unknown_participants", []),
        "ambiguous_participants": snapshot.get("ambiguous_participants", []),
        "scheduling_slot": {"start": parsed.get("start_time"), "end": parsed.get("end_time")},
        "alternative_slots": snapshot.get("alternative_slots", []),
        "resource_info": snapshot.get("resource_info", {}),
        "venue_check": snapshot.get("venue_check"),
        "agenda": meeting.get("agenda", ""),
        "has_purpose": bool(parsed.get("purpose")),
        "approval_status": meeting.get("approval_status"),
        "organizer_email": snapshot.get("organizer_email"),
        "execution_result": snapshot.get("execution", {}),
        "execution_steps": snapshot.get("execution_steps") or {},
        "organizer_availability": snapshot.get("organizer_availability"),
        "trace": [],
    }


def resume_meeting_workflow(meeting_id: str, action: str, **extra) -> MeetingWorkflowState:
    """Re-enter the graph for an existing persisted meeting (EDIT / REPLAN / APPROVAL / RESPONSE)."""
    meeting = _db_meeting(meeting_id)
    if not meeting:
        raise WorkflowError("resume", meeting_id, LookupError(f"Meeting {meeting_id} not found"))
    state = _state_from_db(meeting)
    state.update(extra)
    state["resume_action"] = action.upper()
    audit_memory.record_event("LangGraph", "RESUME_WORKFLOW", meeting_id=meeting_id,
                              details={"action": state["resume_action"], "status": meeting.get("status")})
    return _run(state)


def workflow_view(state: MeetingWorkflowState) -> Dict[str, Any]:
    """Shape the graph result like the draft plan the API always returned (plus trace and execution info)."""
    meeting = _db_meeting(state["meeting_id"]) or {}
    snapshot = (meeting.get("parsed_details") or {}).get("workflow") or {}
    parsed = meeting.get("parsed_details") or {}
    result = {
        "meeting_id": state["meeting_id"],
        "id": state["meeting_id"],
        "title": meeting.get("title") or (state.get("draft_plan") or {}).get("title") or state.get("title") or (state.get("parsed_details") or {}).get("title") or "Meeting",
        "status": meeting.get("status") or state.get("status"),
        "mode": meeting.get("mode") or state.get("mode"),
        "scheduled_start": meeting.get("scheduled_start"),
        "scheduled_end": meeting.get("scheduled_end"),
        "duration_minutes": meeting.get("duration_minutes"),
        "purpose": meeting.get("purpose", ""),
        "agenda": meeting.get("agenda", ""),
        "participants": state.get("participants") or meeting.get("participants", []),
        "unknown_participants": state.get("unknown_participants", []),
        "ambiguous_participants": state.get("ambiguous_participants", []),
        "room_name": meeting.get("room_name", ""),
        "venue_coordinator_email": parsed.get("venue_coordinator_email", ""),
        "meet_url": meeting.get("meet_url", ""),
        "meet_error": snapshot.get("meet_error", ""),
        "calendar_event_id": meeting.get("calendar_event_id", ""),
        "conflicts": [c for c in state.get("conflicts", meeting.get("conflicts", []))
                      if not (c.get("status") == "FLAGGED_FOR_HUMAN"
                              and (c.get("conflicting_values") or {}).get("field") == "venue_availability")],
        "critique_notes": state.get("critique_notes", []),
        "validation": state.get("validation") or (meeting.get("parsed_details") or {}).get("validation", {}),
        "approval_status": meeting.get("approval_status") or state.get("approval_status", "PENDING"),
        "alternative_slots": state.get("alternative_slots", []),
        "requested_slot": state.get("requested_slot"),
        "organizer_availability": state.get("organizer_availability"),
        "rag_context": snapshot.get("rag_context", {}),
        "execution": state.get("execution_result") or {},
        "execution_error": state.get("execution_error"),
        "warnings": state.get("warnings", []),
        "room_bookings": meeting.get("room_bookings", []),
        "workflow_trace": [t["node"] for t in state.get("trace", [])],
        **clarification_view(state.get("parsed_details") or (meeting.get("parsed_details") or {})),
    }
    if (meeting.get("mode") or state.get("mode") or "").upper() == "OFFLINE":
        from backend.agents.venue_coordinator import venue_view
        result["venue_coordination"] = venue_view(meeting, snapshot, snapshot.get("organizer_email"))
    return result


def clarification_view(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """What the agent understood vs. what it still needs to ask (used by the details step in the UI)."""
    return {
        "missing_fields": parsed.get("missing", []),
        "questions": parsed.get("questions", []),
        "understood": {k: parsed.get(k) for k in ("title", "purpose", "date", "start_clock", "duration_minutes", "mode",
                                                   "room_name", "participants", "agenda_points", "venue_coordinator_email")},
        "agenda_points": parsed.get("agenda_points") or [],
        "details_confirmed": bool(parsed.get("details_confirmed")),
        "meeting_timezone": settings.MEETING_TIMEZONE,
    }
