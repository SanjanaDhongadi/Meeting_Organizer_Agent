from typing import TypedDict, List, Dict, Any, Optional

class MeetingWorkflowState(TypedDict, total=False):
    meeting_id: str
    raw_request: str
    status: str
    mode: str
    parsed_details: Dict[str, Any]
    participants: List[Dict[str, Any]]
    unknown_participants: List[Dict[str, Any]]
    ambiguous_participants: List[Dict[str, Any]]
    scheduling_slot: Optional[Dict[str, str]]
    alternative_slots: List[Dict[str, str]]
    participant_calendar_statuses: Dict[str, Any]
    agenda: str
    has_purpose: bool
    skip_agenda: bool
    resource_info: Dict[str, Any]
    conflicts: List[Dict[str, Any]]
    critique_notes: List[str]
    validation_status: str
    validation_errors: List[str]
    validation_warnings: List[str]
    draft_plan: Dict[str, Any]
    approval_status: str  # PENDING, APPROVED, REJECTED, EDITED
    approval_notes: str
    execution_result: Dict[str, Any]
    pending_participants: List[str]
    pending_room: bool
    error_message: Optional[str]
    trace: List[Dict[str, Any]]
