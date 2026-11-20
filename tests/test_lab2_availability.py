"""
Lab 2 - calendar availability through typed tools.

Google is faked only at the client boundary (googleapiclient.discovery.build), so the real node, agent, skill,
tool, MCP gateway, calendar adapter and credential loader run unchanged.

Statuses (stored): AVAILABLE / BUSY / UNVERIFIED; UI labels: Available / Busy / Availability not verified.
"""
import json
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from backend.app.db import Employee, SessionLocal, User
from backend.app.main import app
from backend.graph.workflow_graph import node_check_availability
from backend.skills.scheduling_skill import AVAILABILITY_LABELS, availability_status

client = TestClient(app)
SLOT = {"start": "2030-01-08T09:00:00Z", "end": "2030-01-08T09:30:00Z"}


class _Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeGoogle:
    def __init__(self):
        self.calls = []
        self.busy = {}       # email -> busy blocks
        self.fail_for = {}   # email -> exception raised by freebusy
        self.events = {}

    def build(self, name, version, credentials=None, cache_discovery=False):
        google = self
        account = credentials.token.replace("access-", "") if credentials is not None else None

        class Calendar:
            def freebusy(self):
                class FB:
                    def query(_, body):
                        email = body["items"][0]["id"]
                        google.calls.append(("freebusy", account, email))

                        def run():
                            if email in google.fail_for:
                                raise google.fail_for[email]
                            return {"calendars": {email: {"busy": google.busy.get(email, [])}}}
                        return _Exec(run)
                return FB()

            def events(self):
                class Ev:
                    def insert(_, calendarId, body, conferenceDataVersion, sendUpdates):
                        google.calls.append(("events.insert", account, [a["email"] for a in body["attendees"]], sendUpdates))

                        def run():
                            event_id = f"gcal{uuid.uuid4().hex[:10]}"
                            event = {"id": event_id, "htmlLink": "https://calendar.google.com/x",
                                     "hangoutLink": "https://meet.google.com/abc-defg-hij",
                                     "attendees": [{"email": a["email"], "responseStatus": "needsAction"} for a in body["attendees"]]}
                            google.events[event_id] = event
                            return event
                        return _Exec(run)

                    def patch(_, calendarId, eventId, body, conferenceDataVersion, sendUpdates):
                        google.calls.append(("events.patch", account, [a["email"] for a in body.get("attendees", [])], sendUpdates))
                        return _Exec(lambda: {**google.events[eventId], **body})

                    def get(_, calendarId, eventId):
                        return _Exec(lambda: google.events[eventId])
                return Ev()

        class Gmail:
            def users(self):
                class Users:
                    def messages(_):
                        class Msgs:
                            def send(__, userId, body):
                                google.calls.append(("gmail.send", account))
                                return _Exec(lambda: {"id": "gm1"})
                        return Msgs()
                return Users()

        return {"calendar": Calendar, "gmail": Gmail}[name]()

    def checked(self):
        return {c[2] for c in self.calls if c[0] == "freebusy"}


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    monkeypatch.setattr("googleapiclient.discovery.build", fake.build)
    return fake


def _tokens(email, expired=False, refresh=True):
    expiry = datetime.utcnow() + (timedelta(hours=-1) if expired else timedelta(hours=1))
    data = {"token": f"access-{email}", "client_id": "cid", "client_secret": "csecret",
            "token_uri": "https://oauth2.googleapis.com/token", "scopes": settings.google_scopes,
            "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if refresh:
        data["refresh_token"] = f"refresh-{email}"
    return json.dumps(data)


def _user(email, connected=True, **token_args):
    """A registered application user; calendar access exists only if Google was connected (separate OAuth)."""
    db = SessionLocal()
    try:
        db.query(User).filter(User.email == email).delete()
        user = User(email=email, name=email.split("@")[0].title(), password_hash="x",
                    google_calendar_connected=connected, google_tokens=_tokens(email, **token_args) if connected else "{}")
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    finally:
        db.close()


@pytest.fixture
def cleanup():
    emails = []
    yield emails
    db = SessionLocal()
    try:
        db.query(User).filter(User.email.in_(emails)).delete(synchronize_session=False)
        db.query(Employee).filter(Employee.email.in_(emails)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _run(participants, organizer=None, slot=SLOT):
    mid = f"lab2-{uuid.uuid4().hex[:8]}"
    state = {
        "meeting_id": mid,
        "participants": [{"name": e.split("@")[0], "email": e, "is_external": True, "calendar_status": "UNVERIFIED"} for e in participants],
        "parsed_details": {"start_time": slot["start"], "end_time": slot["end"], "duration_minutes": 30,
                           "date": slot["start"][:10]},
        "organizer_email": organizer,
        "trace": [],
    }
    return node_check_availability(state)


def _statuses(out):
    return {p["email"]: p["calendar_status"] for p in out["participants"]}


# 1 + 2: organizer free / busy
def test_organizer_calendar_free(google, cleanup):
    org = "org.free@lab2.test"
    cleanup.append(org)
    _user(org)
    out = _run(["guest@lab2.test"], organizer=org)
    assert out["organizer_availability"]["status"] == "AVAILABLE"
    assert ("freebusy", org, org) in google.calls            # read with the organizer's own credentials
    assert out["requested_slot_busy"] is False


def test_organizer_calendar_busy_is_reported_as_conflict(google, cleanup):
    org = "org.busy@lab2.test"
    cleanup.append(org)
    _user(org)
    google.busy[org] = [{"start": SLOT["start"], "end": SLOT["end"]}]
    out = _run(["guest@lab2.test"], organizer=org)
    assert out["organizer_availability"]["status"] == "BUSY"
    assert out["requested_slot_busy"] is True
    assert any(c["participant"] == org and c["severity"] == "HIGH" for c in out["scheduling_conflicts"])


# 3: registered + connected participant
def test_registered_connected_participant_is_checked_with_own_credentials(google, cleanup):
    p = "reg.connected@lab2.test"
    cleanup.append(p)
    _user(p)
    out = _run([p])
    assert _statuses(out) == {p: "AVAILABLE"}
    assert ("freebusy", p, p) in google.calls

    google.busy[p] = [{"start": SLOT["start"], "end": SLOT["end"]}]
    assert _statuses(_run([p])) == {p: "BUSY"}


# 4 + 5: registered but not connected / not registered -> never read, never free
def test_registered_but_not_connected_is_not_verified(google, cleanup):
    p = "reg.noconnect@lab2.test"
    cleanup.append(p)
    _user(p, connected=False)
    out = _run([p])
    assert _statuses(out) == {p: "UNVERIFIED"}
    assert "not connected" in out["participants"][0]["availability_reason"]
    assert p not in google.checked()                           # no Google call was made for them


def test_unregistered_participant_is_not_verified_and_does_not_block(google, cleanup):
    p = "stranger@lab2.test"
    out = _run([p])
    assert _statuses(out) == {p: "UNVERIFIED"}
    assert p not in google.checked()
    assert out["requested_slot_busy"] is False
    assert not any(c["severity"] == "HIGH" for c in out["scheduling_conflicts"])


# 6: mixture
def test_mixed_participants(google, cleanup):
    org, a, b, c = ("org.mix@lab2.test", "mix.free@lab2.test", "mix.busy@lab2.test", "mix.ext@lab2.test")
    cleanup.extend([org, a, b])
    _user(org); _user(a); _user(b)
    google.busy[b] = [{"start": SLOT["start"], "end": SLOT["end"]}]
    out = _run([a, b, c], organizer=org)
    assert _statuses(out) == {a: "AVAILABLE", b: "BUSY", c: "UNVERIFIED"}
    assert out["organizer_availability"]["status"] == "AVAILABLE"
    assert c not in google.checked()


# 7: OAuth failures degrade to UNVERIFIED, never to free and never an exception
def test_expired_token_without_refresh_token_is_not_verified(google, cleanup):
    p = "expired.norefresh@lab2.test"
    cleanup.append(p)
    _user(p, expired=True, refresh=False)
    out = _run([p])
    assert _statuses(out) == {p: "UNVERIFIED"}
    assert p not in google.checked()


def test_refresh_failure_and_missing_permission_are_not_verified(google, cleanup, monkeypatch):
    from google.auth.exceptions import RefreshError
    p1, p2 = "revoked@lab2.test", "noperm@lab2.test"
    cleanup.extend([p1, p2])
    _user(p1, expired=True)
    _user(p2)

    def refuse(self, request):
        raise RefreshError("invalid_grant")
    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", refuse)

    from googleapiclient.errors import HttpError
    resp = type("Resp", (), {"status": 403, "reason": "Forbidden"})()
    google.fail_for[p2] = HttpError(resp, json.dumps({"error": {"code": 403, "message": "Insufficient Permission",
                                                              "errors": [{"reason": "insufficientPermissions"}]}}).encode())
    out = _run([p1, p2])
    assert _statuses(out) == {p1: "UNVERIFIED", p2: "UNVERIFIED"}
    db = SessionLocal()
    try:                                                      # revoked token disconnects the account
        assert db.query(User).filter(User.email == p1).first().google_calendar_connected is False
    finally:
        db.close()


def test_demo_mode_never_reports_free():
    assert settings.DEMO_MODE is True
    out = _run(["demo.guest@lab2.test"], organizer="demo.org@lab2.test")
    assert _statuses(out) == {"demo.guest@lab2.test": "UNVERIFIED"}
    assert out["organizer_availability"]["status"] == "UNVERIFIED"


# Status vocabulary
def test_three_status_vocabulary():
    assert AVAILABILITY_LABELS == {"AVAILABLE": "Available", "BUSY": "Busy", "UNVERIFIED": "Availability not verified"}
    for raw in (None, "", "Email added", "Cannot determine", "unknown", {"status": "FREE"}):
        assert availability_status(raw) == "UNVERIFIED"
    assert availability_status({"status": "busy"}) == "BUSY"
    assert availability_status("AVAILABLE") == "AVAILABLE"


# 8 + 9: the full flow still invites everyone, including unverified external participants
def test_invitations_still_go_to_unverified_external_participants(google, cleanup):
    org, ext = "flow.org@lab2.test", "flow.external@lab2.test"
    cleanup.append(org)
    org_id = _user(org)
    from backend.graph.workflow_graph import start_meeting_workflow
    from backend.app.auth import create_access_token
    headers = {"Authorization": "Bearer " + create_access_token({"sub": str(org_id), "email": org})}

    plan = client.post("/api/meetings/orchestrate", headers=headers, json={
        "request": f"Schedule an online design review with {ext} on 2030-01-08 from 9 AM to 9:30 AM to review designs."}).json()
    assert plan["status"] in {"WAITING_FOR_HUMAN_APPROVAL", "NEEDS_INFORMATION"}, plan
    if plan["status"] == "NEEDS_INFORMATION":
        pytest.skip("request parser needs more details in this environment: " + str(plan.get("missing_fields")))
    assert {p["email"]: p["calendar_status"] for p in plan["participants"]} == {ext: "UNVERIFIED"}
    assert plan["organizer_availability"]["status"] == "AVAILABLE"
    assert ext not in google.checked()

    mid = plan["meeting_id"]
    res = client.post(f"/api/meetings/{mid}/approval", headers=headers, json={"action": "APPROVE"})
    assert res.status_code == 200, res.text
    invited = [c for c in google.calls if c[0] in {"events.insert", "events.patch"} and c[3] == "all"]
    assert invited and ext in invited[-1][2]                 # invited without ever registering
    assert any(c[0] == "gmail.send" for c in google.calls)
