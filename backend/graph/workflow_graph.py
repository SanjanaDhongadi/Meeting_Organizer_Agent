import copy
import functools
import json
import uuid
import logging
from typing import Dict, Any, List, Literal, Optional
from langgraph.graph import StateGraph, START, END

from backend.graph.state import MeetingWorkflowState
from backend.agents.coordinator_agent import MeetingCoordinator
from backend.tools.registry import tool_registry
from backend.memory.audit_memory import audit_memory
from backend.memory.long_term_memory import long_term_memory
from backend.memory.session_memory import session_memory
from backend.app.config import settings
from worker.state.state_manager import state_manager

logger = logging.getLogger("workflow_graph")

coordinator = MeetingCoordinator()

# Statuses that mean "the plan is waiting for the human gate / for more information".
PRE_APPROVAL_STATUSES = {"DRAFT", "WAITING_FOR_HUMAN_APPROVAL", "RESCHEDULING_REQUIRED"}


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
        try:
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
        return {**result, "trace": trace}
    return wrapper


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
        "resource_info": state.get("resource_info", {}),
        "organizer_email": state.get("organizer_email"),
        "execution": state.get("execution_result", {}),
        "execution_error": state.get("execution_error"),
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


def _db_meeting(meeting_id: str) -> Optional[Dict[str, Any]]:
    return state_manager.load_meeting(meeting_id)


# ----------------- GRAPH NODES -----------------

@workflow_node
def node_intake(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state.get("meeting_id") or f"meet-{uuid.uuid4().hex[:10]}"
    raw_request = state.get("raw_request", "")
    audit_memory.record_event("Meeting Coordinator", "INTAKE", meeting_id=meeting_id, details={"request": raw_request})
    session_memory.init_session(meeting_id, raw_request)
    # The meeting exists as a DRAFT from the first step so every later step works on the same persisted entity.
    _save(meeting_id=meeting_id, title="Draft meeting", raw_request=raw_request, status="DRAFT", approval_status="PENDING")
    return {**state, "meeting_id": meeting_id, "status": "DRAFT"}

@workflow_node
def node_parse_request(state: MeetingWorkflowState) -> MeetingWorkflowState:
    parsed = coordinator.parse_natural_language_request(state.get("raw_request", ""))
    meeting_id = state["meeting_id"]
    audit_memory.record_event("Meeting Coordinator", "PARSE_REQUEST", meeting_id=meeting_id, details=parsed)
    return {
        **state,
        "parsed_details": parsed,
        "mode": parsed.get("mode", "ONLINE"),
        "status": "PARSED"
    }

@workflow_node
def node_identify_participants(state: MeetingWorkflowState) -> MeetingWorkflowState:
    parsed = state.get("parsed_details", {})
    p_names = parsed.get("participants", [])
    output = coordinator.participant_agent.execute({"parsed_participants": p_names})
    meeting_id = state["meeting_id"]
    audit_memory.record_event("Participant Agent", "IDENTIFY_PARTICIPANTS", meeting_id=meeting_id, details=output.data)

    return {
        **state,
        "participants": output.data.get("resolved_participants", []),
        "unknown_participants": output.data.get("unknown_participants", []),
        "ambiguous_participants": output.data.get("ambiguous_participants", []),
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
    meeting_id = state["meeting_id"]
    parsed = state.get("parsed_details", {})
    msg = "Action Required: Provide participant email or clarify ambiguous names."
    merged = {
        "participants": state.get("participants", []),
        "unknown_participants": state.get("unknown_participants", []),
        "ambiguous_participants": state.get("ambiguous_participants", []),
        "resource_info": state.get("resource_info", {}),
        "mode": state.get("mode", "ONLINE"),
    }
    conflicts = coordinator.detect_and_resolve_conflicts(merged, {"is_consistent": True, "critique_notes": []})
    audit_memory.record_event("Meeting Coordinator", "ASK_FOR_INFORMATION", meeting_id=meeting_id, status="WAITING", details={"message": msg})
    new_state = {**state, "status": "RESCHEDULING_REQUIRED", "error_message": msg, "conflicts": conflicts,
                 "approval_status": "PENDING", "critique_notes": []}
    # Nothing is scheduled; the draft waits (same meeting) until the organizer resolves the participants.
    _persist_workflow(
        new_state,
        title=state.get("title") or f"Meeting: {parsed.get('purpose') or 'Project Sync'}",
        status="RESCHEDULING_REQUIRED",
        mode=state.get("mode", "ONLINE"),
        scheduled_start=parsed.get("start_time"),
        scheduled_end=parsed.get("end_time"),
        duration_minutes=parsed.get("duration_minutes", 30),
        purpose=parsed.get("purpose", ""),
        approval_status="PENDING",
        conflicts=conflicts,
        participants=state.get("participants", []),
    )
    return new_state

@workflow_node
def node_check_availability(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    participants = state.get("participants", [])
    emails = [p["email"] for p in participants]
    parsed = state.get("parsed_details", {})
    locked = state.get("scheduling_slot") if state.get("slot_locked") else None
    start = (locked or {}).get("start") or parsed.get("start_time")
    end = (locked or {}).get("end") or parsed.get("end_time")

    output = coordinator.scheduling_agent.execute({
        "participant_emails": emails,
        "target_date": (start or "")[:10] or parsed.get("date"),
        "target_start_time": start,
        "target_end_time": end,
        "duration_minutes": parsed.get("duration_minutes", 30),
        "resolved_participants": participants,
    })

    audit_memory.record_event("Scheduling Agent", "CHECK_AVAILABILITY", meeting_id=meeting_id, details=output.data)

    return {
        **state,
        "requested_slot": output.data.get("requested_slot") or {"start": start, "end": end},
        "requested_slot_busy": output.data.get("requested_slot_busy", False),
        "scheduling_slot": output.data.get("requested_slot") or {"start": start, "end": end},
        "alternative_slots": output.data.get("alternative_slots", []),
        "participant_calendar_statuses": output.data.get("participant_statuses", {}),
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
    return {**state, "scheduling_slot": selected, "scheduling_conflicts": conflicts, "status": "ALTERNATIVE_SLOT_SELECTED"}

@workflow_node
def node_prepare_agenda(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    parsed = state.get("parsed_details", {})
    if state.get("agenda_locked") and state.get("agenda"):
        # The organizer edited the agenda; keep the human's version.
        audit_memory.record_event("Agenda Agent", "PREPARE_AGENDA", meeting_id=meeting_id, details={"kept_organizer_agenda": True})
        return {**state, "has_purpose": True, "status": "AGENDA_PREPARED"}
    output = coordinator.agenda_agent.execute({
        "purpose": parsed.get("purpose", ""),
        "duration_minutes": parsed.get("duration_minutes", 30),
        "resolved_participants": state.get("participants", []),
        "skip_agenda": state.get("skip_agenda", False),
        "rag_context": state.get("rag_context", {}),
    })

    audit_memory.record_event("Agenda Agent", "PREPARE_AGENDA", meeting_id=meeting_id, details=output.data)
    return {
        **state,
        "agenda": output.data.get("agenda", ""),
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
    return {
        **state,
        "resource_info": output.data,
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

    # Lab 8 merge -> critique (reads RAG preferences from state) -> conflict detection & resolution.
    merged = {
        "participants": state.get("participants", []),
        "unknown_participants": state.get("unknown_participants", []),
        "ambiguous_participants": state.get("ambiguous_participants", []),
        "scheduling_slot": slot,
        "scheduling_conflicts": state.get("scheduling_conflicts", []),
        "resource_info": state.get("resource_info", {}),
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
    plan = {
        "meeting_id": meeting_id,
        "title": state.get("title") or f"Meeting: {parsed.get('purpose') or 'Project Sync'}",
        "mode": state.get("mode", "ONLINE"),
        "scheduled_start": slot.get("start"),
        "scheduled_end": slot.get("end"),
        "duration_minutes": parsed.get("duration_minutes", 30),
        "purpose": parsed.get("purpose", ""),
        "agenda": state.get("agenda", ""),
        "participants": state.get("participants", []),
        "room_name": resource.get("room_name", ""),
        # No Meet link exists before approval; it is created by Google after APPROVE.
        "meet_url": "",
        "status": status
    }

    new_state = {**state, "draft_plan": plan, "status": status, "approval_status": "PENDING"}
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

def _organizer_for(state: MeetingWorkflowState, attendees: List[str]) -> Optional[str]:
    if settings.DEMO_MODE:
        return None
    from backend.connectors.google_auth import resolve_organizer_email
    return resolve_organizer_email(attendees)

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

    if mode == "OFFLINE":
        resource = state.get("resource_info") or {}
        room_name = meeting.get("room_name") or resource.get("room_name") or "Auditorium"
        room_res = tool_registry.get("request_room_booking").execute(
            room_name=room_name,
            start_time=start,
            end_time=end,
            capacity=resource.get("capacity") or max(len(attendee_emails), 2),
            equipment=(meeting.get("parsed_details") or {}).get("equipment") or resource.get("equipment") or "",
            meeting_id=meeting_id,
            duration_minutes=meeting.get("duration_minutes"),
            participant_count=len(attendee_emails),
            purpose=meeting.get("purpose", ""),
            sender_email=organizer,
        )
        if not room_res.success:
            return _execution_failure(state, "room_request", room_res.data, room_res.error)
        execution_result = {
            "room_name": room_name,
            "booking_id": room_res.data.get("booking_id"),
            "booking_status": room_res.data.get("booking_status"),
            "booking_email": settings.AUDITORIUM_BOOKING_EMAIL,
            "organizer_email": organizer,
        }
        next_status = "WAITING_FOR_AUDITORIUM_RESPONSE"
        pending_room, pending_participants = True, []
    else:
        cal_res = tool_registry.get("create_calendar_event").execute(
            title=title,
            start_time=start,
            end_time=end,
            attendees=attendee_emails,
            description=meeting.get("agenda", ""),
            is_online=True,
            organizer_email=organizer,
            event_id=meeting.get("calendar_event_id") or None,
            meeting_id=meeting_id,
        )
        if not cal_res.success:
            return _execution_failure(state, "calendar_event", cal_res.data, cal_res.error)
        data = cal_res.data or {}
        meet_url = data.get("meet_url") or ""
        if data.get("meet_error"):
            warnings.append(data["meet_error"])
        _save(meeting_id=meeting_id, calendar_event_id=data.get("event_id"), meet_url=meet_url)
        state_manager.reset_participant_responses(meeting_id)

        email_res = tool_registry.get("send_email").execute(
            to_emails=attendee_emails,
            subject=f"Invitation: {title}",
            body="\n".join([
                f"You are invited to: {title}",
                f"When: {start} – {end} (UTC)",
                f"Google Meet: {meet_url}" if meet_url else "Google Meet: not available (see calendar invitation)",
                "",
                "Agenda:",
                meeting.get("agenda") or "(none)",
                "",
                "Please accept or decline the Google Calendar invitation.",
            ]),
            meeting_id=meeting_id,
            sender_email=organizer or data.get("organizer_email"),
        )
        if not email_res.success:
            warnings.append(f"Gmail invitation was NOT sent: {email_res.error}")
            audit_memory.record_event("Meeting Coordinator", "SEND_INVITATION_EMAIL", meeting_id=meeting_id, status="ERROR",
                                      error=email_res.error or "", details=email_res.data or {})
        execution_result = {
            "calendar_event_id": data.get("event_id"),
            "meet_url": meet_url,
            "calendar_provider": data.get("provider"),
            "organizer_email": data.get("organizer_email") or organizer,
            "attendees": attendee_emails,
            "email_sent": bool(email_res.success),
            "email_message_id": (email_res.data or {}).get("message_id"),
            "email_provider": (email_res.data or {}).get("provider"),
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
    room_rejected = [r for r in rooms if r.get("status") == "REJECTED"]
    room_pending = any(r.get("status") == "PENDING" for r in rooms)
    room_confirmed = any(r.get("status") == "CONFIRMED" for r in rooms)
    all_accepted = bool(participants) and all(p.get("response_status") == "ACCEPTED" for p in participants)

    if rejected_people:
        outcome, next_status = "RESCHEDULE", "RESCHEDULING_REQUIRED"
        note = f"Participant {', '.join(rejected_people)} rejected the meeting invitation."
    elif room_rejected:
        outcome, next_status = "RESCHEDULE", "RESCHEDULING_REQUIRED"
        note = f"Room booking for {room_rejected[0]['room_name']} was rejected."
    elif mode == "OFFLINE":
        if room_confirmed:
            outcome, next_status, note = "READY", meeting.get("status"), "Room confirmed."
        else:
            outcome, next_status, note = "WAITING", "WAITING_FOR_AUDITORIUM_RESPONSE", "Still waiting for the auditorium response."
    elif all_accepted and room_pending:
        outcome, next_status, note = "WAITING", "WAITING_FOR_AUDITORIUM_RESPONSE", "All participants accepted. Still awaiting room confirmation."
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

    if meeting.get("mode") == "OFFLINE":
        # The room is confirmed: now send the calendar invitation for the in-person meeting (no Meet link).
        attendees = [p["email"] for p in meeting.get("participants", [])]
        organizer = _organizer_for(state, attendees)
        room = meeting.get("room_name", "")
        cal_res = tool_registry.get("create_calendar_event").execute(
            title=meeting.get("title") or "Meeting",
            start_time=meeting.get("scheduled_start") or "",
            end_time=meeting.get("scheduled_end") or "",
            attendees=attendees,
            description=meeting.get("agenda", ""),
            is_online=False,
            organizer_email=organizer,
            location=room,
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
                 f"Where: {room} (booking confirmed)\n\nAgenda:\n{meeting.get('agenda') or '(none)'}",
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

def route_entry(state: MeetingWorkflowState) -> Literal["node_intake", "node_retrieve_context", "node_human_approval", "node_process_responses"]:
    action = (state.get("resume_action") or "START").upper()
    if action in {"EDIT", "REPLAN"}:
        return "node_retrieve_context"
    if action == "APPROVAL":
        return "node_human_approval"
    if action == "RESPONSE":
        return "node_process_responses"
    return "node_intake"

def route_check_information(state: MeetingWorkflowState) -> Literal["node_ask_for_information", "node_check_availability"]:
    if state.get("unknown_participants") or state.get("ambiguous_participants"):
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
    final_state = workflow_app.invoke(initial, {"recursion_limit": 50})
    # Record the complete node path of this run (nodes may persist before they finish).
    after = _db_meeting(final_state["meeting_id"])
    if after:
        parsed = after.get("parsed_details") or {}
        workflow = parsed.get("workflow") or {}
        workflow["trace"] = (prior + [t["node"] for t in final_state.get("trace", [])])[-40:]
        parsed["workflow"] = workflow
        _save(meeting_id=final_state["meeting_id"], parsed_details=parsed)
    return final_state


def start_meeting_workflow(raw_request: str, meeting_id: Optional[str] = None, skip_agenda: bool = False) -> MeetingWorkflowState:
    return _run({"resume_action": "START", "raw_request": raw_request, "meeting_id": meeting_id or f"meet-{uuid.uuid4().hex[:10]}",
                 "skip_agenda": skip_agenda, "trace": []})


def _profile_for(email: str, name: str) -> Dict[str, Any]:
    res = tool_registry.get("retrieve_participant_profile").execute(identifier=email)
    if res.success:
        emp = res.data
        return {
            "name": emp["name"], "email": emp["email"], "employee_id": emp["employee_id"],
            "designation": emp["designation"], "department": emp["department"], "timezone": emp["timezone"],
            "working_days": emp["working_days"], "working_hours": f"{emp['working_hours_start']} - {emp['working_hours_end']}",
            "preferences": emp["meeting_preferences"], "is_external": False,
            "calendar_status": "AVAILABLE" if emp.get("google_calendar_connected") else "UNVERIFIED",
        }
    return {"name": name or email, "email": email, "employee_id": None, "designation": "External Guest", "department": "External",
            "timezone": "UTC", "working_days": "Unknown", "working_hours": "Unknown", "preferences": "None",
            "is_external": True, "calendar_status": "UNVERIFIED"}


def _state_from_db(meeting: Dict[str, Any]) -> MeetingWorkflowState:
    parsed = copy.deepcopy(meeting.get("parsed_details") or {})
    snapshot = parsed.get("workflow") or {}
    profiles = {p["email"].lower(): p for p in snapshot.get("participants", []) if p.get("email")}
    participants = [
        {**(profiles.get(p["email"].lower()) or _profile_for(p["email"], p.get("name"))), "response_status": p.get("response_status")}
        for p in meeting.get("participants", [])
    ]
    # The persisted plan (possibly edited by the organizer) is the source of truth on resume.
    parsed.update({
        "start_time": meeting.get("scheduled_start") or parsed.get("start_time"),
        "end_time": meeting.get("scheduled_end") or parsed.get("end_time"),
        "duration_minutes": meeting.get("duration_minutes") or parsed.get("duration_minutes", 30),
        "purpose": meeting.get("purpose") if meeting.get("purpose") is not None else parsed.get("purpose", ""),
        "room_name": meeting.get("room_name") or parsed.get("room_name", ""),
        "mode": meeting.get("mode") or parsed.get("mode", "ONLINE"),
    })
    return {
        "meeting_id": meeting["id"],
        "raw_request": meeting.get("raw_request", ""),
        "status": meeting.get("status"),
        "mode": parsed["mode"],
        "title": meeting.get("title"),
        "parsed_details": parsed,
        "participants": participants,
        "unknown_participants": snapshot.get("unknown_participants", []),
        "ambiguous_participants": snapshot.get("ambiguous_participants", []),
        "scheduling_slot": {"start": parsed["start_time"], "end": parsed["end_time"]},
        "alternative_slots": snapshot.get("alternative_slots", []),
        "resource_info": snapshot.get("resource_info", {}),
        "agenda": meeting.get("agenda", ""),
        "has_purpose": bool(parsed.get("purpose")),
        "approval_status": meeting.get("approval_status"),
        "organizer_email": snapshot.get("organizer_email"),
        "execution_result": snapshot.get("execution", {}),
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
    return {
        "meeting_id": state["meeting_id"],
        "id": state["meeting_id"],
        "title": meeting.get("title") or (state.get("draft_plan") or {}).get("title"),
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
        "meet_url": meeting.get("meet_url", ""),
        "calendar_event_id": meeting.get("calendar_event_id", ""),
        "conflicts": state.get("conflicts", meeting.get("conflicts", [])),
        "critique_notes": state.get("critique_notes", []),
        "validation": state.get("validation") or (meeting.get("parsed_details") or {}).get("validation", {}),
        "approval_status": meeting.get("approval_status") or state.get("approval_status", "PENDING"),
        "alternative_slots": state.get("alternative_slots", []),
        "requested_slot": state.get("requested_slot"),
        "rag_context": snapshot.get("rag_context", {}),
        "execution": state.get("execution_result") or {},
        "execution_error": state.get("execution_error"),
        "warnings": state.get("warnings", []),
        "room_bookings": meeting.get("room_bookings", []),
        "workflow_trace": [t["node"] for t in state.get("trace", [])],
    }
