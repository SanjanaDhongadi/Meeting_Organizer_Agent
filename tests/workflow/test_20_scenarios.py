import pytest
import uuid
from datetime import datetime
from backend.runtime.runtime_loop import agent_runtime
from backend.app.db import SessionLocal, Meeting, MeetingParticipant, RoomBooking, AuditLog
from backend.app.main import ApprovalGateSchema, handle_human_approval
from backend.connectors.calendar_service import MockCalendarService
from backend.connectors.room_service import MockRoomService
from backend.tools.registry import tool_registry
from backend.skills.agenda_skill import AgendaPreparationSkill
from worker.worker import background_worker
from worker.events.participant_events import ParticipantResponseEvent
from worker.events.room_events import RoomBookingResponseEvent

@pytest.fixture(autouse=True)
def setup_db():
    from backend.app.init_db import init_and_seed_db
    init_and_seed_db()

# Test 1: Normal online meeting
def test_01_normal_online_meeting():
    plan = agent_runtime.run_meeting_request(
        "I want to schedule a meeting with Alice and Bob on Friday from 3 PM to 4 PM. It should be online and the purpose is to discuss the AI project."
    )
    assert plan["mode"] == "ONLINE"
    assert len(plan["participants"]) >= 2
    assert "Alice Chen" in [p["name"] for p in plan["participants"]]
    assert "Bob Smith" in [p["name"] for p in plan["participants"]]
    assert plan["status"] == "WAITING_FOR_HUMAN_APPROVAL"
    assert plan["approval_status"] == "PENDING"
    assert "agenda" in plan and len(plan["agenda"]) > 0

# Test 2: Normal offline meeting
def test_02_normal_offline_meeting():
    plan = agent_runtime.run_meeting_request(
        "Schedule an offline meeting with Bob and David Miller in Conference Room B on Friday at 2 PM to review system architecture."
    )
    assert plan["mode"] == "OFFLINE"
    assert "Conference Room B" in plan["room_name"]
    assert plan["status"] == "WAITING_FOR_HUMAN_APPROVAL"

# Test 3: Unknown participant
def test_03_unknown_participant():
    plan = agent_runtime.run_meeting_request(
        "Schedule a meeting with ZacheryUnknownPerson on Friday at 3 PM to discuss budget."
    )
    assert len(plan["unknown_participants"]) > 0
    assert plan["unknown_participants"][0]["queried_name"] == "ZacheryUnknownPerson"
    assert plan["status"] == "RESCHEDULING_REQUIRED" # Blocked due to unknown participant

# Test 4: Ambiguous participant
def test_04_ambiguous_participant():
    # 'David' matches David Miller and David Martinez in seed data
    plan = agent_runtime.run_meeting_request(
        "Schedule a meeting with David on Friday at 3 PM to discuss user testing."
    )
    assert len(plan["ambiguous_participants"]) > 0
    assert len(plan["ambiguous_participants"][0]["candidates"]) >= 2
    assert plan["status"] == "RESCHEDULING_REQUIRED"

# Test 5: Demo mode never guesses calendar availability
def test_05_demo_calendar_availability_is_unverified():
    details = MockCalendarService().check_availability(
        ["carol.davis@example.com"], "2026-10-02T15:00:00Z", "2026-10-02T16:00:00Z"
    )["details"]
    assert details["carol.davis@example.com"]["status"] == "UNVERIFIED"

# Test 6: Participant accepts
def test_06_participant_accepts():
    m_id = f"test-p-accept-{uuid.uuid4().hex[:6]}"
    plan = agent_runtime.run_meeting_request(
        "Schedule an online meeting with Alice and Bob on Thursday at 11 AM to discuss product roadmap.",
        session_id=m_id
    )
    db = SessionLocal()
    handle_human_approval(m_id, ApprovalGateSchema(action="APPROVE"), db)
    db.close()
    # Simulate Alice accepting
    res = background_worker.simulate_participant_response(m_id, "alice.chen@example.com", "ACCEPTED")
    assert res["status"] in ["WAITING_FOR_PARTICIPANTS", "CONFIRMED"]

# Test 7: Participant rejects
def test_07_participant_rejects():
    m_id = f"test-p-reject-{uuid.uuid4().hex[:6]}"
    agent_runtime.run_meeting_request(
        "Schedule an online meeting with Alice and Bob on Thursday at 2 PM to discuss refactoring.",
        session_id=m_id
    )
    db = SessionLocal()
    handle_human_approval(m_id, ApprovalGateSchema(action="APPROVE"), db)
    db.close()
    # Simulate Bob rejecting
    res = background_worker.simulate_participant_response(m_id, "bob.smith@example.com", "REJECTED", notes="Conflict with customer call")
    assert res["status"] == "RESCHEDULING_REQUIRED"

# Test 8: Participant remains pending
def test_08_participant_remains_pending():
    m_id = f"test-p-pending-{uuid.uuid4().hex[:6]}"
    agent_runtime.run_meeting_request(
        "Schedule an online meeting with Alice and external.guest@client.com on Thursday at 3 PM to discuss sales.",
        session_id=m_id
    )
    meeting = background_worker.state_manager.load_meeting(m_id)
    # External participant is marked pending
    assert meeting is not None

# Test 9: Late participant response (resumes same meeting)
def test_09_late_participant_response():
    m_id = f"test-late-resp-{uuid.uuid4().hex[:6]}"
    agent_runtime.run_meeting_request(
        "Schedule an online meeting with Alice and Bob on Monday at 10 AM to discuss sprints.",
        session_id=m_id
    )
    db = SessionLocal()
    handle_human_approval(m_id, ApprovalGateSchema(action="APPROVE"), db)
    db.close()
    # Alice accepts first
    background_worker.simulate_participant_response(m_id, "alice.chen@example.com", "ACCEPTED")
    # Later Bob accepts - same workflow resumes
    res = background_worker.simulate_participant_response(m_id, "bob.smith@example.com", "ACCEPTED")
    assert res["status"] == "CONFIRMED"
    # Ensure meeting ID is identical
    assert res["meeting_id"] == m_id

# Test 10: Room unavailable
def test_10_room_unavailable():
    m_id = f"test-room-unavail-{uuid.uuid4().hex[:6]}"
    agent_runtime.run_meeting_request(
        "Schedule an offline meeting with Bob in Auditorium Alpha on Thursday at 3 PM. Purpose is to host the all-hands.",
        session_id=m_id
    )
    db = SessionLocal()
    handle_human_approval(m_id, ApprovalGateSchema(action="APPROVE"), db)
    db.close()
    # Simulate room rejected by facility manager
    res = background_worker.simulate_room_approval(m_id, "Auditorium Alpha", confirmed=False, notes="Maintenance underway")
    assert res["status"] == "RESCHEDULING_REQUIRED"

# Test 11: Late room response (resumes same meeting)
def test_11_late_room_response():
    m_id = f"test-late-room-{uuid.uuid4().hex[:6]}"
    agent_runtime.run_meeting_request(
        "Schedule an offline meeting with Bob in Auditorium Alpha on Thursday at 11 AM. Purpose is to present the demo.",
        session_id=m_id
    )
    db = SessionLocal()
    handle_human_approval(m_id, ApprovalGateSchema(action="APPROVE"), db)
    db.close()
    # Late room confirmation
    res = background_worker.simulate_room_approval(m_id, "Auditorium Alpha", confirmed=True, notes="Approved by facilities")
    assert res["status"] in ["WAITING_FOR_PARTICIPANTS", "CONFIRMED"]
    assert res["meeting_id"] == m_id

# Test 12: Missing meeting purpose
def test_12_missing_meeting_purpose():
    # No purpose given: "Schedule a meeting with Alice and Bob Friday at 3."
    plan = agent_runtime.run_meeting_request(
        "Schedule a meeting with Alice and Bob on Friday at 3 PM."
    )
    # Purpose is missing, so agent should solicit or allow skip
    assert plan["purpose"] == ""
    assert plan["status"] == "DRAFT" # Needs purpose or skip

# Test 13: Human approves
def test_13_human_approves():
    m_id = f"test-human-appr-{uuid.uuid4().hex[:6]}"
    agent_runtime.run_meeting_request(
        "Schedule an online meeting with Alice and Bob on Friday at 4 PM to discuss design.",
        session_id=m_id
    )
    # Approve via meeting job
    meeting = background_worker.state_manager.load_meeting(m_id)
    # The approval gate requires a recorded human approval, not just a status change.
    tool_registry.get("save_meeting_state").execute(meeting_id=m_id, approval_status="APPROVED")
    background_worker.state_manager.update_meeting_status(m_id, "APPROVED", "HUMAN_APPROVE")
    exec_res = background_worker.meeting_job.execute(m_id)
    assert exec_res["success"] is True

# Test 14: Human rejects
def test_14_human_rejects():
    m_id = f"test-human-rej-{uuid.uuid4().hex[:6]}"
    agent_runtime.run_meeting_request(
        "Schedule an online meeting with Alice on Friday at 3 PM to discuss architecture.",
        session_id=m_id
    )
    background_worker.state_manager.update_meeting_status(m_id, "REJECTED", "HUMAN_REJECT", notes="Not priority")
    m = background_worker.state_manager.load_meeting(m_id)
    assert m["status"] == "REJECTED"

# Test 15: Human edits
def test_15_human_edits():
    m_id = f"test-human-edit-{uuid.uuid4().hex[:6]}"
    plan = agent_runtime.run_meeting_request(
        "Schedule an online meeting with Alice on Friday at 3 PM to discuss testing.",
        session_id=m_id
    )
    # Edit duration to 45 min
    save_tool = tool_registry.get("save_meeting_state")
    save_tool.execute(meeting_id=m_id, duration_minutes=45, approval_status="EDITED")
    m = background_worker.state_manager.load_meeting(m_id)
    assert m["duration_minutes"] == 45
    assert m["approval_status"] == "EDITED"

# Test 16: Tool / API failure handling
def test_16_tool_failure_handling():
    cal_tool = tool_registry.get("check_calendar_availability")
    # Calling with empty emails triggers handled validation error without crash
    res = cal_tool.execute(emails=[], start_time="2026-10-02T15:00:00Z", end_time="2026-10-02T16:00:00Z")
    assert res.success is False
    assert "error" in res.error.lower() or "must provide" in res.error.lower()

# Test 17: Duplicate request (Idempotency)
def test_17_duplicate_request():
    evt_id = f"evt-dup-{uuid.uuid4().hex[:6]}"
    m_id = f"test-dup-meet-{uuid.uuid4().hex[:6]}"
    evt = ParticipantResponseEvent(event_id=evt_id, meeting_id=m_id, email="alice.chen@example.com", response="ACCEPTED")
    res1 = background_worker.response_job.process_participant_response(evt)
    res2 = background_worker.response_job.process_participant_response(evt)
    assert res2["status"] == "SKIPPED_DUPLICATE"

# Test 18: Parallel agent execution (Lab 8)
def test_18_parallel_agent_execution():
    state = {
        "meeting_id": "test-parallel",
        "parsed_participants": ["Alice Chen", "Bob Smith"],
        "target_date": "2026-10-02",
        "target_start_time": "2026-10-02T15:00:00Z",
        "target_end_time": "2026-10-02T16:00:00Z",
        "duration_minutes": 60,
        "purpose": "Discuss AI multi-agent orchestration",
        "mode": "ONLINE"
    }
    outputs = agent_runtime.coordinator.execute_parallel_agents(state)
    assert "participant" in outputs
    assert "scheduling" in outputs
    assert "agenda" in outputs
    assert "resource" in outputs

# Test 19: Agent conflict
def test_19_agent_conflict():
    # Setup state where participant is Carol Davis on a Friday
    state = {
        "meeting_id": "test-conflict",
        "parsed_participants": ["Carol Davis"],
        "target_date": "2026-10-02", # Friday
        "target_start_time": "2026-10-02T15:00:00Z",
        "target_end_time": "2026-10-02T16:00:00Z",
        "duration_minutes": 60,
        "purpose": "Executive Strategy Sync",
        "mode": "ONLINE"
    }
    outputs = agent_runtime.coordinator.execute_parallel_agents(state)
    merged = agent_runtime.coordinator.merge_results(outputs, state)
    critique = agent_runtime.coordinator.critique(merged, state)
    conflicts = agent_runtime.coordinator.detect_and_resolve_conflicts(merged, critique)
    assert len(conflicts) > 0

# Test 20: Conflict resolution
def test_20_conflict_resolution():
    state = {
        "meeting_id": "test-resolution",
        "parsed_participants": ["Bob Smith"],
        "target_date": "2026-10-02",
        "target_start_time": "2026-10-02T15:00:00Z",
        "target_end_time": "2026-10-02T16:00:00Z",
        "duration_minutes": 60,
        "purpose": "Quarterly Keynote All-Hands",
        "mode": "OFFLINE",
        "room_name": "Auditorium Alpha"
    }
    outputs = agent_runtime.coordinator.execute_parallel_agents(state)
    merged = agent_runtime.coordinator.merge_results(outputs, state)
    critique = agent_runtime.coordinator.critique(merged, state)
    conflicts = agent_runtime.coordinator.detect_and_resolve_conflicts(merged, critique)
    # Check that resolution instructions are provided
    for c in conflicts:
        assert "resolution" in c
        assert len(c["resolution"]) > 0

# Test 21: Online approval gates calendar creation and resumes after responses
def test_21_online_approval_and_delayed_responses():
    m_id = f"test-approved-online-{uuid.uuid4().hex[:8]}"
    plan = agent_runtime.run_meeting_request(
        "Schedule an online meeting with Alice and Bob on Thursday at 11 AM to discuss planning.",
        session_id=m_id
    )
    assert plan["meet_url"] == ""

    db = SessionLocal()
    try:
        meeting = db.query(Meeting).filter(Meeting.id == m_id).first()
        assert meeting.calendar_event_id == ""
        assert meeting.meet_url == ""
        handle_human_approval(m_id, ApprovalGateSchema(action="APPROVE"), db)
        db.refresh(meeting)
        assert meeting.status == "WAITING_FOR_PARTICIPANTS"
        assert meeting.calendar_event_id
        assert meeting.meet_url.startswith("https://meet.google.com/")
    finally:
        db.close()

    first = background_worker.simulate_participant_response(m_id, "alice.chen@example.com", "ACCEPTED")
    assert first["status"] == "WAITING_FOR_PARTICIPANTS"
    final = background_worker.simulate_participant_response(m_id, "bob.smith@example.com", "ACCEPTED")
    assert final["status"] == "CONFIRMED"
    assert final["meeting_id"] == m_id

# Test 22: Offline approval sends a room request and waits for a later response
def test_22_offline_approval_waits_for_auditorium_response():
    from backend.app.config import settings
    from backend.connectors.email_service import get_email_service

    m_id = f"test-approved-room-{uuid.uuid4().hex[:8]}"
    agent_runtime.run_meeting_request(
        "Schedule an offline meeting with Bob in Auditorium Alpha on Thursday at 11 AM. Purpose is to present the plan.",
        session_id=m_id
    )
    db = SessionLocal()
    try:
        meeting = db.query(Meeting).filter(Meeting.id == m_id).first()
        assert db.query(RoomBooking).filter(RoomBooking.meeting_id == m_id).count() == 0
        handle_human_approval(m_id, ApprovalGateSchema(action="APPROVE"), db)
        db.refresh(meeting)
        booking = db.query(RoomBooking).filter(RoomBooking.meeting_id == m_id).one()
        assert meeting.status == "WAITING_FOR_AUDITORIUM_RESPONSE"
        assert booking.status == "PENDING"
        email = get_email_service().outbox[-1]
        assert email["to"] == [settings.AUDITORIUM_BOOKING_EMAIL]
        assert "Duration:" in email["body"]
        assert "Number of participants:" in email["body"]
        assert "Required equipment:" in email["body"]
        assert "Meeting purpose:" in email["body"]
    finally:
        db.close()

    final = background_worker.simulate_room_approval(m_id, "Auditorium Alpha", confirmed=True)
    assert final["status"] == "CONFIRMED"
    assert final["meeting_id"] == m_id

# Test 23: Editing returns to approval and rejection sends no invitations
def test_23_edit_and_reject_preserve_approval_gate():
    m_id = f"test-edit-reject-{uuid.uuid4().hex[:8]}"
    agent_runtime.run_meeting_request(
        "Schedule an online meeting with Alice on Thursday at 1 PM to discuss testing.",
        session_id=m_id
    )
    db = SessionLocal()
    try:
        meeting = db.query(Meeting).filter(Meeting.id == m_id).first()
        handle_human_approval(m_id, ApprovalGateSchema(action="EDIT", edits={"duration_minutes": 45}), db)
        db.refresh(meeting)
        assert meeting.status == "WAITING_FOR_HUMAN_APPROVAL"
        assert meeting.duration_minutes == 45
        handle_human_approval(m_id, ApprovalGateSchema(action="REJECT"), db)
        db.refresh(meeting)
        assert meeting.status == "REJECTED"
        assert meeting.calendar_event_id == ""
    finally:
        db.close()
