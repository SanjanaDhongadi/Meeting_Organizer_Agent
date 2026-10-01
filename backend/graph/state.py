from typing import TypedDict, List, Dict, Any, Optional

class MeetingWorkflowState(TypedDict, total=False):
    meeting_id: str
    raw_request: str
    status: str
    mode: str
    title: str
    parsed_details: Dict[str, Any]
    participants: List[Dict[str, Any]]
    unknown_participants: List[Dict[str, Any]]
    ambiguous_participants: List[Dict[str, Any]]
    rag_context: Dict[str, Any]
    scheduling_slot: Optional[Dict[str, str]]
    requested_slot: Optional[Dict[str, str]]
    requested_slot_busy: bool
    alternative_slots: List[Dict[str, str]]
    participant_calendar_statuses: Dict[str, Any]
    scheduling_conflicts: List[Dict[str, Any]]
    agenda: str
    has_purpose: bool
    skip_agenda: bool
    resource_info: Dict[str, Any]
    conflicts: List[Dict[str, Any]]
    critique_notes: List[str]
    validation_status: str
    validation_errors: List[str]
    validation_warnings: List[str]
    validation: Dict[str, Any]
    draft_plan: Dict[str, Any]
    approval_status: str  # PENDING, APPROVED, REJECTED, EDITED
    approval_notes: str
    execution_result: Dict[str, Any]
    execution_error: Optional[Dict[str, Any]]
    warnings: List[str]
    pending_participants: List[str]
    pending_room: bool
    error_message: Optional[str]
    trace: List[Dict[str, Any]]
    # Resume control: which persisted step the workflow re-enters (START, EDIT, REPLAN, APPROVAL, RESPONSE)
    resume_action: str
    slot_locked: bool
    agenda_locked: bool
    response_event: Dict[str, Any]
    response_outcome: str
    organizer_email: Optional[str]
