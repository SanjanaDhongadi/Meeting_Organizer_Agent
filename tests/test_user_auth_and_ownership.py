import pytest
import uuid
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.db import SessionLocal, User, Meeting, MeetingParticipant

client = TestClient(app)

def test_1_new_user_registration():
    """Verify new user registration with email and password."""
    unique_email = f"user_{uuid.uuid4().hex[:8]}@example.com"
    payload = {
        "email": unique_email,
        "password": "securepassword123",
        "name": "Test Organizer"
    }
    response = client.post("/api/auth/register", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()
    assert "token" in data
    assert data["user"]["email"] == unique_email
    assert data["user"]["name"] == "Test Organizer"
    assert data["user"]["has_password"] is True


def test_2_email_password_login():
    """Verify email/password login returns valid JWT token."""
    unique_email = f"login_{uuid.uuid4().hex[:8]}@example.com"
    # Register first
    client.post("/api/auth/register", json={
        "email": unique_email,
        "password": "loginpassword123",
        "name": "Login User"
    })

    # Successful login
    response = client.post("/api/auth/login", json={
        "email": unique_email,
        "password": "loginpassword123"
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert "token" in data
    assert data["user"]["email"] == unique_email

    # Verify /api/auth/me with bearer token
    token = data["token"]
    me_resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == unique_email

    # Invalid password fails with 401
    bad_resp = client.post("/api/auth/login", json={
        "email": unique_email,
        "password": "wrongpassword"
    })
    assert bad_resp.status_code == 401


def test_3_continue_with_google():
    """Verify Google login initialization requests only minimal login scopes."""
    response = client.get("/api/auth/google/login")
    assert response.status_code == 200
    data = response.json()
    assert "authorization_url" in data
    url = data["authorization_url"]
    assert "accounts.google.com" in url
    # Verify minimal scopes for login (openid, email, profile) - NOT calendar or gmail
    assert "userinfo.email" in url
    assert "calendar" not in url
    assert "gmail" not in url


def test_4_connecting_google_later():
    """Verify authenticated user can connect Google with calendar/gmail scopes."""
    unique_email = f"googleconnect_{uuid.uuid4().hex[:8]}@example.com"
    reg = client.post("/api/auth/register", json={
        "email": unique_email,
        "password": "password123",
        "name": "Google Connect User"
    }).json()
    token = reg["token"]

    response = client.get("/api/auth/google/connect", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert "authorization_url" in data
    url = data["authorization_url"]
    assert "accounts.google.com" in url
    # Verify calendar and gmail scopes are requested here
    assert "calendar" in url
    assert "gmail.send" in url


def test_5_scheduling_meeting_without_google_connected():
    """Users without Google connected can still create and schedule meetings internally."""
    unique_email = f"nogauth_{uuid.uuid4().hex[:8]}@example.com"
    reg = client.post("/api/auth/register", json={
        "email": unique_email,
        "password": "password123",
        "name": "No Google User"
    }).json()
    token = reg["token"]

    # Schedule a meeting without Google connected
    response = client.post(
        "/api/meetings/orchestrate",
        headers={"Authorization": f"Bearer {token}"},
        json={"request": "Team sync tomorrow at 2 PM with user1@example.com"}
    )
    assert response.status_code == 200, response.text
    meeting_data = response.json()
    assert meeting_data["id"].startswith("meet-")


def test_6_correct_organizer_ownership_in_postgres():
    """Verify meeting organizer_id is set from session, not frontend."""
    unique_email = f"organizer_{uuid.uuid4().hex[:8]}@example.com"
    reg = client.post("/api/auth/register", json={
        "email": unique_email,
        "password": "password123",
        "name": "Real Organizer"
    }).json()
    user_id = reg["user"]["id"]
    token = reg["token"]

    response = client.post(
        "/api/meetings/orchestrate",
        headers={"Authorization": f"Bearer {token}"},
        json={"request": "Sync with attendee@example.com next Monday at 10 AM"}
    )
    meeting_id = response.json()["id"]

    # Check database directly
    db = SessionLocal()
    try:
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
        assert meeting is not None
        assert meeting.organizer_id == user_id
    finally:
        db.close()


def test_7_users_cannot_access_another_organizers_private_meetings():
    """Verify user A cannot view or approve user B's meeting."""
    # User A
    user_a = client.post("/api/auth/register", json={
        "email": f"usera_{uuid.uuid4().hex[:8]}@example.com",
        "password": "password123",
        "name": "User A"
    }).json()
    token_a = user_a["token"]

    # User B
    user_b = client.post("/api/auth/register", json={
        "email": f"userb_{uuid.uuid4().hex[:8]}@example.com",
        "password": "password123",
        "name": "User B"
    }).json()
    token_b = user_b["token"]

    # User A creates a meeting with person C
    created = client.post(
        "/api/meetings/orchestrate",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"request": "Confidential meeting with personc@example.com next Tuesday at 3 PM"}
    ).json()
    meeting_id = created["id"]

    # User B tries to view User A's meeting -> 403 Forbidden
    view_resp = client.get(f"/api/meetings/{meeting_id}", headers={"Authorization": f"Bearer {token_b}"})
    assert view_resp.status_code == 403

    # User B tries to approve User A's meeting -> 403 Forbidden
    appr_resp = client.post(
        f"/api/meetings/{meeting_id}/approval",
        headers={"Authorization": f"Bearer {token_b}"},
        json={"action": "APPROVE"}
    )
    assert appr_resp.status_code == 403


def test_8_participants_can_see_invited_meetings():
    """Verify participants can view meetings they are invited to in Meeting Requests."""
    participant_email = f"invited_{uuid.uuid4().hex[:8]}@example.com"
    part_user = client.post("/api/auth/register", json={
        "email": participant_email,
        "password": "password123",
        "name": "Invited Participant"
    }).json()
    part_token = part_user["token"]

    # Organizer creates meeting inviting participant_email
    org_user = client.post("/api/auth/register", json={
        "email": f"org_{uuid.uuid4().hex[:8]}@example.com",
        "password": "password123",
        "name": "Organizer"
    }).json()
    org_token = org_user["token"]

    created = client.post(
        "/api/meetings/orchestrate",
        headers={"Authorization": f"Bearer {org_token}"},
        json={"request": f"Strategy alignment with {participant_email} on Friday at 11 AM"}
    ).json()
    meeting_id = created["id"]

    # Participant can view the meeting
    view_resp = client.get(f"/api/meetings/{meeting_id}", headers={"Authorization": f"Bearer {part_token}"})
    assert view_resp.status_code == 200
    assert view_resp.json()["id"] == meeting_id

    # Meeting appears in Participant's "requests" section
    sections_resp = client.get("/api/meetings?section=requests", headers={"Authorization": f"Bearer {part_token}"})
    assert sections_resp.status_code == 200
    req_ids = [m["id"] for m in sections_resp.json()]
    assert meeting_id in req_ids


def test_9_attended_meetings_displays_only_recorded_attendance():
    """Verify Past Meetings section displays only meetings with recorded attendance."""
    attendee_email = f"attendee_{uuid.uuid4().hex[:8]}@example.com"
    att_user = client.post("/api/auth/register", json={
        "email": attendee_email,
        "password": "password123",
        "name": "Attending User"
    }).json()
    att_token = att_user["token"]

    # Organizer creates meeting
    org_user = client.post("/api/auth/register", json={
        "email": f"host_{uuid.uuid4().hex[:8]}@example.com",
        "password": "password123",
        "name": "Host"
    }).json()
    org_token = org_user["token"]

    created = client.post(
        "/api/meetings/orchestrate",
        headers={"Authorization": f"Bearer {org_token}"},
        json={"request": f"Product retro with {attendee_email} tomorrow at 4 PM"}
    ).json()
    meeting_id = created["id"]

    # Before attendance is recorded, Past Meetings is empty for attendee
    past_before = client.get("/api/meetings?section=past", headers={"Authorization": f"Bearer {att_token}"}).json()
    past_ids_before = [m["id"] for m in past_before]
    assert meeting_id not in past_ids_before

    # Record attendance
    rec_resp = client.post(
        f"/api/meetings/{meeting_id}/attendance",
        headers={"Authorization": f"Bearer {att_token}"},
        json={"email": attendee_email, "attended": True}
    )
    assert rec_resp.status_code == 200
    assert rec_resp.json()["participant"]["attended"] is True

    # Now meeting appears in Past Meetings!
    past_after = client.get("/api/meetings?section=past", headers={"Authorization": f"Bearer {att_token}"}).json()
    past_ids_after = [m["id"] for m in past_after]
    assert meeting_id in past_ids_after


def test_10_existing_meeting_workflow_still_works():
    """Verify full meeting workflow: orchestrate -> review/approval -> state persistence."""
    user = client.post("/api/auth/register", json={
        "email": f"workflow_{uuid.uuid4().hex[:8]}@example.com",
        "password": "password123",
        "name": "Workflow Tester"
    }).json()
    token = user["token"]

    # Orchestrate
    orch = client.post(
        "/api/meetings/orchestrate",
        headers={"Authorization": f"Bearer {token}"},
        json={"request": "Quarterly planning with devteam@example.com on Thursday at 10 AM"}
    )
    assert orch.status_code == 200
    plan = orch.json()
    meeting_id = plan["id"]
    assert plan["title"] is not None

    # Retrieve meeting details
    det = client.get(f"/api/meetings/{meeting_id}", headers={"Authorization": f"Bearer {token}"})
    assert det.status_code == 200
    assert det.json()["id"] == meeting_id

    # Audit logs
    audit = client.get(f"/api/audit-logs/{meeting_id}", headers={"Authorization": f"Bearer {token}"})
    assert audit.status_code == 200

