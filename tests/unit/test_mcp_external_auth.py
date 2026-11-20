"""
Lab 5 - the external MCP endpoint (POST /api/mcp, GET /api/mcp/capabilities) is authenticated and caller-scoped.
Internal mcp_call() (no auth_context) is unchanged. DEMO_MODE adapters; the mock e-mail service is spied on.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.app.db import AuditLog, Meeting, MeetingParticipant, SessionLocal
from backend.app.main import app
from backend.connectors import email_service
from backend.connectors.mcp_gateway import mcp_call

client = TestClient(app)


def _user(name):
    email = f"{name.lower()}_{uuid.uuid4().hex[:6]}@example.test"
    res = client.post("/api/auth/register", json={"email": email, "password": "secret123", "name": name})
    assert res.status_code == 200, res.text
    return {"email": email, "id": res.json()["user"]["id"], "headers": {"Authorization": f"Bearer {res.json()['token']}"}}


def _meeting(owner, approved=True, event_id="evt-owned", participants=()):
    mid = f"mcp-{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    try:
        db.add(Meeting(id=mid, organizer_id=owner["id"], title="MCP test", raw_request="MCP test", status="CONFIRMED" if approved else "WAITING_FOR_HUMAN_APPROVAL",
                       approval_status="APPROVED" if approved else "PENDING", calendar_event_id=event_id, parsed_details="{}"))
        for email in participants:
            db.add(MeetingParticipant(meeting_id=mid, email=email, name=email.split("@")[0]))
        db.commit()
    finally:
        db.close()
    return mid


def _mcp(user, method, params):
    return client.post("/api/mcp", json={"method": method, "params": params}, headers=user["headers"] if user else None)


def _denied(res):
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["error"] and body["error"]["code"] == -32003, body
    return body["error"]["message"]


def _ok(res):
    assert res.status_code == 200, res.text
    body = res.json()
    assert not body["error"], body
    return body["result"]


@pytest.fixture
def spy(monkeypatch):
    seen = {"read": [], "send": []}
    service = email_service._mock_email_instance
    monkeypatch.setattr(service, "get_messages", lambda query="", account_email=None: seen["read"].append(account_email) or [])
    original_send = service.send_invitation

    def send(to_emails, subject, body, meeting_id, sender_email=None):
        seen["send"].append(sender_email)
        return original_send(to_emails, subject, body, meeting_id, sender_email)
    monkeypatch.setattr(service, "send_invitation", send)
    return seen


def test_endpoints_require_a_token():
    assert client.post("/api/mcp", json={"method": "gmail.read", "params": {}}).status_code == 401
    assert client.get("/api/mcp/capabilities").status_code == 401
    me = _user("Cap")
    assert "capabilities" in client.get("/api/mcp/capabilities", headers=me["headers"]).json()


def test_gmail_read_is_limited_to_the_callers_own_account(spy):
    me, other = _user("Reader"), _user("Other")
    assert "own Google account" in _denied(_mcp(me, "gmail.read", {"account_email": other["email"]}))
    assert spy["read"] == []
    _ok(_mcp(me, "gmail.read", {}))
    assert spy["read"] == [me["email"]]                              # never falls back to another connected account
    _ok(_mcp(me, "gmail.read", {"account_email": me["email"].upper()}))
    assert spy["read"][-1] == me["email"]


def test_gmail_send_requires_the_callers_own_approved_meeting(spy):
    me, other = _user("Sender"), _user("Stranger")
    body = {"to_emails": ["x@example.test"], "subject": "s", "body": "b"}
    assert "APPROVED" in _denied(_mcp(me, "gmail.send", body))                                                  # no meeting_id
    assert "APPROVED" in _denied(_mcp(me, "gmail.send", {**body, "meeting_id": "does-not-exist"}))
    assert "APPROVED" in _denied(_mcp(me, "gmail.send", {**body, "meeting_id": _meeting(other)}))               # someone else's
    assert "APPROVED" in _denied(_mcp(me, "gmail.send", {**body, "meeting_id": _meeting(me, approved=False)}))  # not approved
    assert "own Google account" in _denied(_mcp(me, "gmail.send", {**body, "meeting_id": _meeting(me), "sender_email": other["email"]}))
    assert spy["send"] == []
    _ok(_mcp(me, "gmail.send", {**body, "meeting_id": _meeting(me)}))
    assert spy["send"] == [me["email"]]                                                                        # sender forced to caller


def test_cancel_event_must_match_the_meetings_event():
    me = _user("Canceller")
    mid = _meeting(me, event_id="evt-mine")
    assert "does not belong" in _denied(_mcp(me, "calendar.cancel_event", {"meeting_id": mid, "event_id": "evt-someone-elses"}))
    assert not _mcp(me, "calendar.cancel_event", {"meeting_id": mid, "event_id": "evt-mine"}).json()["error"]
    no_event = _meeting(me, event_id="")
    assert "does not belong" in _denied(_mcp(me, "calendar.cancel_event", {"meeting_id": no_event, "event_id": ""}))


def test_availability_is_limited_to_self_and_participants_of_own_meetings():
    me, other = _user("Planner"), _user("Nosy")
    window = {"start_time": "2030-01-08T09:00:00Z", "end_time": "2030-01-08T09:30:00Z"}
    assert "own calendar" in _denied(_mcp(me, "calendar.check_availability", {"emails": ["stranger@example.test"], **window}))
    _ok(_mcp(me, "calendar.check_availability", {"emails": [me["email"]], **window}))
    mid = _meeting(me, participants=["guest@example.test"])
    _ok(_mcp(me, "calendar.check_availability", {"emails": ["guest@example.test", me["email"]], "meeting_id": mid, **window}))
    # the same meeting id does not help someone who does not organize it
    _denied(_mcp(other, "calendar.check_availability", {"emails": ["guest@example.test"], "meeting_id": mid, **window}))
    _denied(_mcp(me, "calendar.find_common_slots", {"emails": ["stranger@example.test"], "date_str": "2030-01-08"}))
    _ok(_mcp(me, "calendar.find_common_slots", {"emails": [me["email"]], "date_str": "2030-01-08"}))


def test_transcripts_stay_internal_only_even_when_authenticated():
    me = _user("Transcript")
    assert "internal only" in _denied(_mcp(me, "meet.get_transcript", {"meeting_code": "abc-defg-hij"}))


def test_internal_mcp_call_is_unaffected():
    assert mcp_call("gmail.read", {})["success"] is True
    res = mcp_call("gmail.send", {"to_emails": ["x@example.test"], "subject": "s", "body": "b", "meeting_id": "any-internal-id"})
    assert res["success"] is True                                    # internal callers are not caller-scoped


def test_external_calls_are_audited_without_message_content():
    me = _user("Audited")
    mid = _meeting(me)
    secret = f"SECRET-BODY-{uuid.uuid4().hex}"
    _ok(_mcp(me, "gmail.send", {"to_emails": ["x@example.test"], "subject": "subj", "body": secret, "meeting_id": mid}))
    _denied(_mcp(me, "gmail.send", {"to_emails": ["x@example.test"], "subject": "subj", "body": secret}))
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.action == "EXTERNAL_MCP_CALL", AuditLog.agent == "MCP Gateway").all()
        mine = [r for r in rows if json.loads(r.details).get("user_id") == me["id"]]
    finally:
        db.close()
    assert sorted(r.status for r in mine) == ["ERROR", "SUCCESS"]
    assert any(r.meeting_id == mid for r in mine)
    for row in mine:
        assert json.loads(row.details) == {"method": "gmail.send", "user_id": me["id"]}
        assert secret not in json.dumps(row.to_dict())
