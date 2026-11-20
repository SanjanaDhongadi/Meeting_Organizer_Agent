import uuid
import pytest
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.db import SessionLocal, Meeting, MeetingParticipant
from backend.skills.agenda_skill import improve_agenda, professionalize_sentence
from backend.agents.coordinator_agent import MeetingCoordinator

client = TestClient(app)


@pytest.mark.parametrize("names", [
    ["Nagveni and Sheetal"],
    ["Nagveni and Sheetal", "Kim"],
])
def test_grouped_participant_extraction_creates_one_email_question_per_name(names):
    coordinator = MeetingCoordinator()
    parsed = coordinator._normalize_llm_parse({"participants": names}, {
        "participants": [], "title": "Project Sync", "purpose": "Discuss project progress",
        "date": "2030-01-10", "start_clock": "10:00", "duration_minutes": 30,
        "mode": "ONLINE", "room_name": "", "equipment": "", "agenda_points": [],
        "ambiguities": {}, "parser": "rules",
    })

    expected = ["Nagveni", "Sheetal"] + (["Kim"] if len(names) == 2 else [])
    email_questions = [q for q in parsed["questions"] if q["input"] == "email"]
    assert [p["name"] for p in parsed["participants"]] == expected
    assert [q["participant_name"] for q in email_questions] == expected
    assert [q["participant_index"] for q in email_questions] == list(range(1, len(expected) + 1))


def test_agenda_rewritten_without_topic_headings():
    """Verify rough agenda is rewritten into professional sentences without separate topic headings."""
    raw_points = [
        "discuss project progress",
        "review what work is completed",
        "discuss pending tasks",
        "identify problems and challenges we are facing",
        "decide what needs to be done next",
        "assign responsibilities to team members"
    ]
    expected_points = [
        "Review the current project progress and assess the work completed so far.",
        "Review completed tasks and deliverables completed so far.",
        "Discuss pending tasks and outstanding deliverables.",
        "Identify the problems and challenges affecting project progress.",
        "Discuss the next steps required to move the project forward.",
        "Assign responsibilities to team members."
    ]

    improved_points, improved_text = improve_agenda(
        raw_points=raw_points,
        purpose="Project status synchronization",
        title="Weekly Status Review",
        raw_request="Weekly Status Review meeting"
    )

    assert len(improved_points) == len(expected_points)
    for actual, expected in zip(improved_points, expected_points):
        assert actual == expected
        # Crucial requirement: No separate topic titles or labels before descriptions (e.g. no ":")
        assert ":" not in actual

    # Verify numbered list formatting
    for i, expected in enumerate(expected_points, start=1):
        assert f"{i}. {expected}" in improved_text


def test_custom_agenda_topic_professional_sentence():
    """Verify custom technical topic produces a complete professional sentence without heading prefix."""
    sentence = professionalize_sentence("evaluate distributed database partitioning strategy")
    assert ":" not in sentence
    assert len(sentence.strip()) > 10
    assert sentence.endswith(".")
    assert "partitioning" in sentence.lower()


def test_case_a_missing_participant_names_requests_names_first():
    """Case A: When participant names are missing, ask for names first, NOT email addresses."""
    unique_email = f"org_a_{uuid.uuid4().hex[:8]}@example.com"
    reg = client.post("/api/auth/register", json={
        "email": unique_email,
        "password": "Password123!",
        "name": "Organizer Case A"
    }).json()
    token = reg["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Organizer requests meeting with NO participant names
    request_payload = {
        "request": "Schedule a quarterly review tomorrow at 3pm for 30 minutes online about Q3 progress"
    }
    res = client.post("/api/meetings/orchestrate", json=request_payload, headers=headers)
    assert res.status_code == 200, res.text
    data = res.json()
    meeting_id = data.get("meeting_id") or data.get("id")

    # Verify: Question asks for participant names, and NO email fields exist
    questions = data.get("questions", [])
    fields = [q["field"] for q in questions]
    assert "participants" in fields
    assert not any(f.startswith("participant_email_") for f in fields)
    p_q = next(q for q in questions if q["field"] == "participants")
    assert "Who should attend?" in p_q["question"]
    assert "Enter participant names" in p_q["question"]

    # 2. Organizer provides names
    clarify_res = client.post(f"/api/meetings/{meeting_id}/clarify", json={
        "attendees": ["Vijeta", "Karen"]
    }, headers=headers)
    assert clarify_res.status_code == 200, clarify_res.text
    clarified_data = clarify_res.json()

    # Now separate email input fields must appear for each participant
    next_questions = clarified_data.get("questions", [])
    q_fields = [q["field"] for q in next_questions]
    assert "participant_email_Vijeta" in q_fields
    assert "participant_email_Karen" in q_fields
    v_q = next(q for q in next_questions if q["field"] == "participant_email_Vijeta")
    k_q = next(q for q in next_questions if q["field"] == "participant_email_Karen")
    assert "Participant 1 – Vijeta" in v_q["question"]
    assert "Participant 2 – Karen" in k_q["question"]


def test_case_b_participant_names_already_provided_shows_emails_directly():
    """Case B: When participant names are already provided, directly display separate email fields."""
    unique_email = f"org_b_{uuid.uuid4().hex[:8]}@example.com"
    reg = client.post("/api/auth/register", json={
        "email": unique_email,
        "password": "Password123!",
        "name": "Organizer Case B"
    }).json()
    token = reg["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Organizer mentions Vijeta and Karen in the prompt
    request_payload = {
        "request": "Schedule a meeting with Vijeta and Karen tomorrow at 3pm for 30 minutes online about budget review"
    }
    res = client.post("/api/meetings/orchestrate", json=request_payload, headers=headers)
    assert res.status_code == 200, res.text
    data = res.json()

    # Verify: Directly displays separate email fields for Vijeta and Karen, does not ask for names again
    questions = data.get("questions", [])
    q_fields = [q["field"] for q in questions]
    assert "participants" not in q_fields  # Does NOT ask for names again
    assert "participant_email_Vijeta" in q_fields
    assert "participant_email_Karen" in q_fields

    v_q = next(q for q in questions if q["field"] == "participant_email_Vijeta")
    k_q = next(q for q in questions if q["field"] == "participant_email_Karen")
    assert "Participant 1 – Vijeta" in v_q["question"]
    assert "Participant 2 – Karen" in k_q["question"]


def test_invalid_email_validation_shows_participant_specific_error():
    """Verify that an invalid email address produces a participant-specific validation error."""
    unique_email = f"org_c_{uuid.uuid4().hex[:8]}@example.com"
    reg = client.post("/api/auth/register", json={
        "email": unique_email,
        "password": "Password123!",
        "name": "Organizer Case C"
    }).json()
    token = reg["token"]
    headers = {"Authorization": f"Bearer {token}"}

    res = client.post("/api/meetings/orchestrate", json={
        "request": "Schedule a sync with Vijeta tomorrow at 10am for 30 minutes online"
    }, headers=headers).json()
    meeting_id = res["meeting_id"]

    # Submit with invalid email format
    bad_res = client.post(f"/api/meetings/{meeting_id}/clarify", json={
        "participant_emails": {"Vijeta": "invalid-email-address"}
    }, headers=headers)
    assert bad_res.status_code == 422
    assert "Please enter a valid email address for Vijeta." in bad_res.text


def test_participant_storage_and_agenda_without_headings_in_postgres():
    """Verify participant names and emails are stored in PostgreSQL and agenda has no headings."""
    unique_email = f"org_d_{uuid.uuid4().hex[:8]}@example.com"
    reg = client.post("/api/auth/register", json={
        "email": unique_email,
        "password": "Password123!",
        "name": "Organizer Case D"
    }).json()
    token = reg["token"]
    headers = {"Authorization": f"Bearer {token}"}

    res = client.post("/api/meetings/orchestrate", json={
        "request": "Schedule a project sync with Nagveni tomorrow at 2pm for 30 minutes online"
    }, headers=headers).json()
    meeting_id = res["meeting_id"]

    clarify_res = client.post(f"/api/meetings/{meeting_id}/clarify", json={
        "participant_emails": {"Nagveni": "nagveni@example.com"},
        "purpose": "Project status review",
        "mode": "ONLINE",
        "agenda_points": [
            "discuss project progress",
            "review what work is completed",
            "discuss pending tasks",
            "identify problems and challenges we are facing",
            "decide what needs to be done next",
            "assign responsibilities to team members"
        ]
    }, headers=headers)
    assert clarify_res.status_code == 200, clarify_res.text
    clarified = clarify_res.json()

    assert clarified["status"] == "WAITING_FOR_HUMAN_APPROVAL"
    participants = clarified.get("participants", [])
    assert len(participants) == 1
    assert participants[0]["name"] == "Nagveni"
    assert participants[0]["email"] == "nagveni@example.com"
    assert participants[0].get("calendar_status") == "UNVERIFIED"  # Lab 2: no calendar access => not verified

    # Verify agenda in response has NO topic headings
    agenda = clarified.get("agenda", "")
    assert "Project Progress Review:" not in agenda
    assert "Completed Tasks:" not in agenda
    assert "Review the current project progress and assess the work completed so far." in agenda
    assert "Discuss pending tasks and outstanding deliverables." in agenda

    # Check database persistence
    with SessionLocal() as db:
        db_m = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        assert db_m is not None
        assert "Project Progress Review:" not in db_m.agenda
        assert "Review the current project progress and assess the work completed so far." in db_m.agenda

        db_parts = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id).all()
        assert len(db_parts) == 1
        assert db_parts[0].name == "Nagveni"
        assert db_parts[0].email == "nagveni@example.com"

    # Approval succeeds
    app_res = client.post(f"/api/meetings/{meeting_id}/approval", json={
        "action": "APPROVE",
        "notes": "Approved without heading labels."
    }, headers=headers)
    assert app_res.status_code == 200, app_res.text
    assert app_res.json()["status"] in ["SCHEDULED", "WAITING_FOR_PARTICIPANTS"]
