import uuid
import logging
from typing import Dict, Any, Literal
from langgraph.graph import StateGraph, START, END

from backend.graph.state import MeetingWorkflowState
from backend.agents.coordinator_agent import MeetingCoordinator
from backend.tools.registry import tool_registry
from backend.memory.audit_memory import audit_memory
from backend.app.config import settings
from backend.connectors.email_service import get_email_service
from worker.state.state_manager import state_manager

logger = logging.getLogger("workflow_graph")

coordinator = MeetingCoordinator()

# ----------------- GRAPH NODES -----------------

def node_intake(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state.get("meeting_id") or f"meet-{uuid.uuid4().hex[:10]}"
    audit_memory.record_event("Meeting Coordinator", "INTAKE", meeting_id=meeting_id, details={"request": state.get("raw_request")})
    return {**state, "meeting_id": meeting_id, "status": "INTAKE"}

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

def node_ask_for_information(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    msg = "Action Required: Provide participant email or clarify ambiguous names."
    audit_memory.record_event("Meeting Coordinator", "ASK_FOR_INFORMATION", meeting_id=meeting_id, status="WAITING", details={"message": msg})
    return {**state, "status": "WAITING_FOR_INFORMATION", "error_message": msg}

def node_check_availability(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    emails = [p["email"] for p in state.get("participants", [])]
    parsed = state.get("parsed_details", {})
    
    output = coordinator.scheduling_agent.execute({
        "participant_emails": emails,
        "target_date": parsed.get("date"),
        "target_start_time": parsed.get("start_time"),
        "target_end_time": parsed.get("end_time"),
        "duration_minutes": parsed.get("duration_minutes", 30),
        "resolved_participants": state.get("participants", [])
    })
    
    audit_memory.record_event("Scheduling Agent", "CHECK_AVAILABILITY", meeting_id=meeting_id, details=output.data)
    
    return {
        **state,
        "scheduling_slot": output.data.get("selected_slot"),
        "alternative_slots": output.data.get("alternative_slots", []),
        "participant_calendar_statuses": output.data.get("participant_statuses", {}),
        "status": "AVAILABILITY_CHECKED"
    }

def node_find_alternative_slot(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    alts = state.get("alternative_slots", [])
    selected = alts[0] if alts else None
    audit_memory.record_event("Scheduling Agent", "FIND_ALTERNATIVE_SLOT", meeting_id=meeting_id, details={"selected_alternative": selected})
    return {**state, "scheduling_slot": selected, "status": "ALTERNATIVE_SLOT_SELECTED"}

def node_prepare_agenda(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    parsed = state.get("parsed_details", {})
    output = coordinator.agenda_agent.execute({
        "purpose": parsed.get("purpose", ""),
        "duration_minutes": parsed.get("duration_minutes", 30),
        "resolved_participants": state.get("participants", []),
        "skip_agenda": state.get("skip_agenda", False)
    })
    
    audit_memory.record_event("Agenda Agent", "PREPARE_AGENDA", meeting_id=meeting_id, details=output.data)
    return {
        **state,
        "agenda": output.data.get("agenda", ""),
        "has_purpose": output.data.get("has_purpose", False),
        "status": "AGENDA_PREPARED"
    }

def node_prepare_resource(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    parsed = state.get("parsed_details", {})
    output = coordinator.resource_agent.execute({
        "mode": state.get("mode", "ONLINE"),
        "resolved_participants": state.get("participants", []),
        "purpose": parsed.get("purpose", ""),
        "room_name": parsed.get("room_name", ""),
        "meeting_id": meeting_id,
        "target_start_time": state.get("scheduling_slot", {}).get("start"),
        "target_end_time": state.get("scheduling_slot", {}).get("end")
    })
    
    audit_memory.record_event("Resource Agent", "PREPARE_RESOURCE", meeting_id=meeting_id, details=output.data)
    return {
        **state,
        "resource_info": output.data,
        "status": "RESOURCE_PREPARED"
    }

def node_validate(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    val_out = coordinator.validation_agent.execute({
        "draft_plan": {
            "mode": state.get("mode", "ONLINE"),
            "start_time": state.get("scheduling_slot", {}).get("start"),
            "room_name": state.get("resource_info", {}).get("room_name")
        },
        "resolved_participants": state.get("participants", []),
        "mode": state.get("mode", "ONLINE"),
        "resource_data": state.get("resource_info", {})
    })
    
    v_data = val_out.data
    audit_memory.record_event("Validation Agent", "VALIDATE", meeting_id=meeting_id, status=v_data.get("validation_status", "PASS"), details=v_data)
    return {
        **state,
        "validation_status": v_data.get("validation_status", "PASS"),
        "validation_errors": v_data.get("errors", []),
        "validation_warnings": v_data.get("warnings", []),
        "status": "VALIDATED"
    }

def node_create_draft_plan(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    parsed = state.get("parsed_details", {})
    slot = state.get("scheduling_slot") or {}
    
    plan = {
        "meeting_id": meeting_id,
        "title": f"Meeting: {parsed.get('purpose') or 'Project Sync'}",
        "mode": state.get("mode", "ONLINE"),
        "scheduled_start": slot.get("start"),
        "scheduled_end": slot.get("end"),
        "duration_minutes": parsed.get("duration_minutes", 30),
        "purpose": parsed.get("purpose", ""),
        "agenda": state.get("agenda", ""),
        "participants": state.get("participants", []),
        "room_name": state.get("resource_info", {}).get("room_name", ""),
        "meet_url": state.get("resource_info", {}).get("provisional_meet_url", ""),
        "status": "WAITING_FOR_HUMAN_APPROVAL"
    }
    
    # Save meeting state to db
    save_tool = tool_registry.get("save_meeting_state")
    save_tool.execute(
        meeting_id=meeting_id,
        title=plan["title"],
        status="WAITING_FOR_HUMAN_APPROVAL",
        mode=plan["mode"],
        scheduled_start=plan["scheduled_start"],
        scheduled_end=plan["scheduled_end"],
        duration_minutes=plan["duration_minutes"],
        purpose=plan["purpose"],
        agenda=plan["agenda"],
        room_name=plan["room_name"],
        meet_url=plan["meet_url"],
        approval_status="PENDING",
        parsed_details={
            **parsed,
            "validation": {
                "errors": state.get("validation_errors", []),
                "warnings": state.get("validation_warnings", []),
            },
        },
        participants=plan["participants"],
    )
    
    audit_memory.record_event("Meeting Coordinator", "CREATE_DRAFT_PLAN", meeting_id=meeting_id, approval_status="WAITING", details=plan)
    return {**state, "draft_plan": plan, "status": "WAITING_FOR_HUMAN_APPROVAL", "approval_status": "PENDING"}

def node_human_approval(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    approval = state.get("approval_status", "PENDING")
    audit_memory.record_event("Human Operator", "HUMAN_APPROVAL_GATE", meeting_id=meeting_id, approval_status=approval, details={"notes": state.get("approval_notes", "")})
    return {**state, "status": f"HUMAN_{approval}"}

def node_execute(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    mode = state.get("mode", "ONLINE")
    slot = state.get("scheduling_slot") or {}
    attendee_emails = [p["email"] for p in state.get("participants", [])]
    
    title = state.get("draft_plan", {}).get("title", "Project Meeting")
    email_tool = tool_registry.get("send_email")
    if mode == "OFFLINE":
        room_name = state.get("resource_info", {}).get("room_name", "Auditorium")
        equipment = state.get("parsed_details", {}).get("equipment") or "Not specified"
        body = "\n".join([
            f"Date and time: {slot.get('start', 'TBD')}",
            f"Duration: {state.get('duration_minutes', 30)} minutes",
            f"Number of participants: {len(attendee_emails)}",
            f"Room/auditorium requirement: {room_name}",
            f"Required equipment: {equipment}",
            f"Meeting purpose: {state.get('purpose', '')}",
            f"Meeting ID: {meeting_id}",
        ])
        email_tool.execute(
            to_emails=[settings.AUDITORIUM_BOOKING_EMAIL],
            subject="Auditorium Booking Request",
            body=body,
            meeting_id=meeting_id,
        )
        state_manager.update_room_response(meeting_id, room_name, "PENDING", "Awaiting auditorium response.")
        execution_result = {"room_name": room_name, "booking_email": settings.AUDITORIUM_BOOKING_EMAIL}
        next_status = "WAITING_FOR_AUDITORIUM_RESPONSE"
        pending_room = True
        pending_participants = []
    else:
        cal_tool = tool_registry.get("create_calendar_event")
        cal_res = cal_tool.execute(
            title=title,
            start_time=slot.get("start", ""),
            end_time=slot.get("end", ""),
            attendees=attendee_emails,
            description=state.get("agenda", ""),
            is_online=True,
        )
        if settings.DEMO_MODE:
            email_tool.execute(
                to_emails=attendee_emails,
                subject=f"Invitation: {title}",
                body=f"Meeting scheduled for {slot.get('start')}.\nAgenda:\n{state.get('agenda')}\nMeet URL: {cal_res.data.get('meet_url', '')}",
                meeting_id=meeting_id,
            )
        execution_result = {
            "calendar_event_id": cal_res.data.get("event_id"),
            "meet_url": cal_res.data.get("meet_url"),
            "attendees": attendee_emails,
        }
        pending_room = False
        pending_participants = attendee_emails
        next_status = "WAITING_FOR_PARTICIPANTS"
        tool_registry.get("save_meeting_state").execute(
            meeting_id=meeting_id,
            status=next_status,
            calendar_event_id=execution_result["calendar_event_id"],
            meet_url=execution_result["meet_url"],
        )
    
    audit_memory.record_event("Meeting Coordinator", "EXECUTE", meeting_id=meeting_id, status="SUCCESS", details=execution_result)
    
    state_manager.update_meeting_status(meeting_id, next_status, "APPROVED_ACTIONS_SENT")
    return {
        **state,
        "execution_result": execution_result,
        "pending_participants": pending_participants,
        "pending_room": pending_room,
        "status": next_status
    }

def node_wait_for_responses(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    audit_memory.record_event("Meeting Coordinator", "WAIT_FOR_RESPONSES", meeting_id=meeting_id, status="WAITING", details={"pending_room": state.get("pending_room"), "pending_participants": state.get("pending_participants")})
    return state

def node_process_responses(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    audit_memory.record_event("Worker", "PROCESS_RESPONSES", meeting_id=meeting_id, status="SUCCESS")
    return {**state, "status": "RESPONSES_PROCESSED"}

def node_revalidate(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    audit_memory.record_event("Validation Agent", "REVALIDATE", meeting_id=meeting_id, status="SUCCESS")
    return {**state, "status": "REVALIDATED"}

def node_finalize(state: MeetingWorkflowState) -> MeetingWorkflowState:
    meeting_id = state["meeting_id"]
    save_tool = tool_registry.get("save_meeting_state")
    save_tool.execute(meeting_id=meeting_id, status="CONFIRMED")
    audit_memory.record_event("Meeting Coordinator", "FINALIZE", meeting_id=meeting_id, status="SUCCESS")
    return {**state, "status": "CONFIRMED"}

# ----------------- CONDITIONAL ROUTING -----------------

def route_check_information(state: MeetingWorkflowState) -> Literal["node_ask_for_information", "node_check_availability"]:
    if state.get("unknown_participants") or state.get("ambiguous_participants"):
        return "node_ask_for_information"
    return "node_check_availability"

def route_check_availability(state: MeetingWorkflowState) -> Literal["node_find_alternative_slot", "node_prepare_agenda"]:
    if not state.get("scheduling_slot"):
        return "node_find_alternative_slot"
    return "node_prepare_agenda"

def route_human_approval(state: MeetingWorkflowState) -> Literal["node_execute", "node_parse_request", "end"]:
    approval = state.get("approval_status", "PENDING").upper()
    if approval == "APPROVED":
        return "node_execute"
    elif approval == "EDITED":
        return "node_parse_request"
    return "end"

def route_after_execute(state: MeetingWorkflowState) -> Literal["node_wait_for_responses", "node_finalize"]:
    if state.get("pending_room") or state.get("pending_participants"):
        return "node_wait_for_responses"
    return "node_finalize"

# ----------------- BUILD GRAPH -----------------

def build_workflow_graph():
    workflow = StateGraph(MeetingWorkflowState)
    
    workflow.add_node("node_intake", node_intake)
    workflow.add_node("node_parse_request", node_parse_request)
    workflow.add_node("node_identify_participants", node_identify_participants)
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
    
    # Edges
    workflow.add_edge(START, "node_intake")
    workflow.add_edge("node_intake", "node_parse_request")
    workflow.add_edge("node_parse_request", "node_identify_participants")
    
    workflow.add_conditional_edges("node_identify_participants", route_check_information, {
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
    
    workflow.add_conditional_edges("node_human_approval", route_human_approval, {
        "node_execute": "node_execute",
        "node_parse_request": "node_parse_request",
        "end": END
    })
    
    workflow.add_conditional_edges("node_execute", route_after_execute, {
        "node_wait_for_responses": "node_wait_for_responses",
        "node_finalize": "node_finalize"
    })
    
    workflow.add_edge("node_wait_for_responses", END)
    workflow.add_edge("node_process_responses", "node_revalidate")
    workflow.add_edge("node_revalidate", "node_finalize")
    workflow.add_edge("node_finalize", END)
    
    return workflow.compile()

workflow_app = build_workflow_graph()
