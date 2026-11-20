"""
End-to-end checks of the live wiring: OAuth/PKCE, real Google adapters (HTTP boundary faked), MCP, LangGraph,
RAG, approval gate, asynchronous resume and the auditorium workflow.

Google is faked ONLY at the client boundary (googleapiclient.discovery.build and the OAuth token POST), so the
application's own OAuth, Calendar, Gmail, MCP and workflow code runs unchanged.
"""
import json
import urllib.parse
import uuid
from datetime import datetime, timedelta

import pytest
import requests
from fastapi.testclient import TestClient

from app.config import settings
from backend.app.db import SessionLocal, Employee, Meeting, MeetingParticipant, RoomBooking, OAuthPendingState, compute_simple_embedding
from backend.app.main import app
from backend.connectors.mcp_gateway import mcp_gateway

client = TestClient(app)


# ----------------- fixtures -----------------

def _employee(email, name, **extra):
    db = SessionLocal()
    try:
        db.query(Employee).filter(Employee.email == email).delete()
        db.commit()
        record = dict(employee_id=f"T-{uuid.uuid4().hex[:6]}", name=name, email=email, designation="Engineer",
                      department="R&D", working_days="Monday,Tuesday,Wednesday,Thursday,Friday",
                      working_hours_start="00:00:00", working_hours_end="23:59:00", timezone="UTC",
                      meeting_preferences="", mode_preference="Online", location="", other_info="")
        record.update(extra)
        emp = Employee(**record, embedding=json.dumps(compute_simple_embedding(f"{name} Engineer R&D")))
        db.add(emp)
        db.commit()
        db.refresh(emp)
        return emp.id
    finally:
        db.close()


def _connect(email):
    """Store valid (non-expired) OAuth tokens for an employee, as the OAuth callback would."""
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter(Employee.email == email).first()
        emp.google_tokens = json.dumps({
            "token": f"access-{email}", "refresh_token": f"refresh-{email}", "client_id": "cid", "client_secret": "csecret",
            "token_uri": "https://oauth2.googleapis.com/token", "scopes": settings.google_scopes,
            "expiry": (datetime.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        emp.google_calendar_connected = True
        db.commit()
    finally:
        db.close()


def _cleanup(*emails):
    db = SessionLocal()
    try:
        db.query(Employee).filter(Employee.email.in_(emails)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


class _Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeGoogle:
    """Records every Google API call made by the real adapters."""

    def __init__(self):
        self.calls = []
        self.events = {}
        self.busy = {}          # email -> list of busy blocks
        self.fail_insert = None  # HttpError to raise on events.insert
        self.meet = True

    def build(self, name, version, credentials=None, cache_discovery=False):
        google = self
        account = credentials.token.replace("access-", "") if credentials is not None else None

        class Calendar:
            def settings(self):
                class Settings:
                    def get(_, setting):
                        google.calls.append(("settings.get", account, setting))
                        return _Exec(lambda: {"value": getattr(google, "calendar_timezone", "UTC")})
                return Settings()

            def freebusy(self):
                class FB:
                    def query(_, body):
                        google.calls.append(("freebusy", account, body))
                        email = body["items"][0]["id"]
                        return _Exec(lambda: {"calendars": {email: {"busy": google.busy.get(email, [])}}})
                return FB()

            def events(self):
                class Ev:
                    def insert(_, calendarId, body, conferenceDataVersion, sendUpdates):
                        google.calls.append(("events.insert", account, body, conferenceDataVersion, sendUpdates))

                        def run():
                            if google.fail_insert:
                                raise google.fail_insert
                            event_id = f"gcal{uuid.uuid4().hex[:10]}"
                            event = {"id": event_id, "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
                                     "attendees": [{"email": a["email"], "responseStatus": "needsAction"} for a in body["attendees"]]}
                            if "conferenceData" in body and google.meet:
                                event["hangoutLink"] = "https://meet.google.com/abc-defg-hij"
                            google.events[event_id] = event
                            return event
                        return _Exec(run)

                    def patch(_, calendarId, eventId, body, conferenceDataVersion, sendUpdates):
                        google.calls.append(("events.patch", account, eventId, body, conferenceDataVersion, sendUpdates))
                        def run():
                            google.events[eventId].update(body)
                            return google.events[eventId]
                        return _Exec(run)

                    def get(_, calendarId, eventId):
                        google.calls.append(("events.get", account, eventId))
                        return _Exec(lambda: google.events[eventId])

                    def delete(_, calendarId, eventId, sendUpdates):
                        google.calls.append(("events.delete", account, eventId, sendUpdates))

                        def run():
                            if getattr(google, "fail_delete", None):
                                raise google.fail_delete
                            google.events.pop(eventId, None)
                            return ""
                        return _Exec(run)
                return Ev()

        class Gmail:
            def users(self):
                class Users:
                    def messages(_):
                        class Msgs:
                            def send(__, userId, body):
                                google.calls.append(("gmail.send", account, body))
                                if getattr(google, "fail_send", None):
                                    raise google.fail_send
                                return _Exec(lambda: {"id": f"gm{uuid.uuid4().hex[:8]}"})

                            def list(__, userId, q, maxResults=20):
                                google.calls.append(("gmail.list", account, q))
                                return _Exec(lambda: {"messages": [{"id": "reply1"}]})

                            def get(__, userId, id, format, metadataHeaders):
                                return _Exec(lambda: {"id": id, "threadId": "t1", "snippet": getattr(google, "reply_snippet", "Confirmed - the auditorium is booked for you."),
                                                      "payload": {"headers": [{"name": "From", "value": COORD},
                                                                              {"name": "Subject", "value": "Re: Auditorium Booking Request"}]}})
                        return Msgs()
                return Users()

        class OAuth2:
            def userinfo(self):
                class UI:
                    def get(_):
                        return _Exec(lambda: {"email": google.userinfo_email})
                return UI()

        return {"calendar": Calendar, "gmail": Gmail, "oauth2": OAuth2}[name]()

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


@pytest.fixture
def live(monkeypatch):
    """Live (non-demo) mode with Google faked at the googleapiclient boundary."""
    fake = FakeGoogle()
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    monkeypatch.setattr("googleapiclient.discovery.build", fake.build)
    return fake


def _google_reply(fake, event_id, email, status):
    """An attendee answers the invitation in Google Calendar (accepted / declined)."""
    for a in fake.events[event_id]["attendees"]:
        if a["email"] == email:
            a["responseStatus"] = status


def _http_error(status, reason, message):
    from googleapiclient.errors import HttpError
    resp = type("Resp", (), {"status": status, "reason": reason})()
    content = json.dumps({"error": {"code": status, "message": message, "errors": [{"reason": reason}]}}).encode()
    return HttpError(resp, content)


# ----------------- 1. OAuth / PKCE -----------------

@pytest.fixture
def oauth_env(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(settings, "SECRET_KEY", "test-secret")
    captured = {}

    def fake_request(self, method, url, data=None, headers=None, **kwargs):
        captured.setdefault("bodies", []).append(data)
        if captured.get("token_error"):
            r = requests.Response()
            r.status_code = 400
            r._content = json.dumps({"error": "invalid_grant", "error_description": "Missing code verifier."}).encode()
        else:
            r = requests.Response()
            r.status_code = 200
            r._content = json.dumps({"access_token": "access-oauth.user@example.test", "refresh_token": "refresh-1",
                                     "expires_in": 3600, "token_type": "Bearer", "scope": " ".join(settings.google_scopes)}).encode()
        r.headers["Content-Type"] = "application/json"
        r.url = url
        r.request = requests.Request(method, url).prepare()
        return r

    monkeypatch.setattr(requests.Session, "request", fake_request)
    return captured


def _start_oauth(emp_id):
    res = client.get(f"/api/employees/{emp_id}/calendar/connect", follow_redirects=False)
    assert res.status_code in (302, 307), res.text
    query = urllib.parse.parse_qs(urllib.parse.urlparse(res.headers["location"]).query)
    return query


def test_oauth_pkce_verifier_preserved_and_tokens_saved(oauth_env, live):
    email = "oauth.user@example.test"
    emp_id = _employee(email, "OAuth User")
    try:
        query = _start_oauth(emp_id)
        assert query["code_challenge_method"] == ["S256"] and query["code_challenge"]
        state = query["state"][0]
        db = SessionLocal()
        stored = db.query(OAuthPendingState).filter(OAuthPendingState.state == state).one()
        verifier = stored.code_verifier
        db.close()

        live.userinfo_email = email
        live.calendar_timezone = "Asia/Kolkata"
        res = client.get("/api/auth/google/callback", params={"state": state, "code": "auth-code"}, follow_redirects=False)
        assert "calendar=connected" in res.headers["location"], res.headers["location"]
        # R6: the employee's time zone comes from their Google Calendar settings.
        assert client.get(f"/api/employees/by-email/{email}").json()["timezone"] == "Asia/Kolkata"

        # The SAME verifier generated at authorization time was sent to Google's token endpoint.
        token_body = oauth_env["bodies"][-1]
        assert token_body["code_verifier"] == verifier
        # Challenge in the authorization URL matches that verifier.
        import base64, hashlib
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        assert query["code_challenge"][0] == expected

        status = client.get(f"/api/employees/{emp_id}/calendar/status").json()
        assert status["google_calendar_connected"] is True and status["status"] == "connected"
        db = SessionLocal()
        tokens = json.loads(db.query(Employee).filter(Employee.id == emp_id).one().google_tokens)
        db.close()
        assert tokens["refresh_token"] == "refresh-1"

        # State / verifier are single use.
        again = client.get("/api/auth/google/callback", params={"state": state, "code": "auth-code"}, follow_redirects=False)
        assert "calendar=error" in again.headers["location"]
    finally:
        _cleanup(email)


def test_oauth_rejects_tampered_state_and_account_mismatch(oauth_env, live):
    email = "oauth.mismatch@example.test"
    emp_id = _employee(email, "Mismatch User")
    try:
        bad = client.get("/api/auth/google/callback", params={"state": "tampered", "code": "x"}, follow_redirects=False)
        assert bad.status_code == 400

        state = _start_oauth(emp_id)["state"][0]
        live.userinfo_email = "someone.else@gmail.com"
        res = client.get("/api/auth/google/callback", params={"state": state, "code": "auth-code"}, follow_redirects=False)
        assert "calendar=account_mismatch" in res.headers["location"]
        assert client.get(f"/api/employees/{emp_id}/calendar/status").json()["google_calendar_connected"] is False
    finally:
        _cleanup(email)


def test_oauth_token_error_is_reported_not_hidden(oauth_env, live):
    email = "oauth.error@example.test"
    emp_id = _employee(email, "Error User")
    try:
        state = _start_oauth(emp_id)["state"][0]
        oauth_env["token_error"] = True
        res = client.get("/api/auth/google/callback", params={"state": state, "code": "auth-code"}, follow_redirects=False)
        location = urllib.parse.unquote_plus(res.headers["location"])
        assert "calendar=error" in location and "invalid_grant" in location
        assert client.get(f"/api/employees/{emp_id}/calendar/status").json()["google_calendar_connected"] is False
    finally:
        _cleanup(email)


def test_token_refresh_and_revocation(monkeypatch):
    from google.oauth2.credentials import Credentials
    from google.auth.exceptions import RefreshError
    from backend.connectors.google_auth import load_google_credentials_with_error

    email = "refresh.user@example.test"
    _employee(email, "Refresh User")
    try:
        _connect(email)
        db = SessionLocal()
        emp = db.query(Employee).filter(Employee.email == email).one()
        tokens = json.loads(emp.google_tokens)
        tokens["expiry"] = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        emp.google_tokens = json.dumps(tokens)
        db.commit()
        db.close()

        def ok_refresh(self, request):
            self.token = "access-refreshed"
            self.expiry = datetime.utcnow() + timedelta(hours=1)
        monkeypatch.setattr(Credentials, "refresh", ok_refresh)
        creds, error = load_google_credentials_with_error(email)
        assert error is None and creds.token == "access-refreshed"
        db = SessionLocal()
        assert json.loads(db.query(Employee).filter(Employee.email == email).one().google_tokens)["token"] == "access-refreshed"
        db.close()

        # Revoked refresh token -> clear error + marked disconnected (never "connected" after a failure).
        db = SessionLocal()
        emp = db.query(Employee).filter(Employee.email == email).one()
        tokens = json.loads(emp.google_tokens)
        tokens["expiry"] = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        emp.google_tokens = json.dumps(tokens)
        db.commit()
        db.close()

        def revoked(self, request):
            raise RefreshError("invalid_grant: Token has been expired or revoked.")
        monkeypatch.setattr(Credentials, "refresh", revoked)
        creds, error = load_google_credentials_with_error(email)
        assert creds is None and error["error_code"] == "REFRESH_TOKEN_REVOKED"
        status = client.get(f"/api/employees/by-email/{email}/calendar/status").json()
        assert status["google_calendar_connected"] is False
    finally:
        _cleanup(email)


# ----------------- 2-6. Live online flow: LangGraph + MCP + real Google adapters -----------------

def test_live_online_flow_uses_graph_mcp_and_google(live, monkeypatch):
    org, guest = "organizer.live@example.test", "guest.live@example.test"
    _employee(org, "Orla Organizer")
    _employee(guest, "Gus Guest")
    mcp_methods = []
    real_execute = mcp_gateway.execute

    def spy(method, params, req_id=None, auth_context=None):
        mcp_methods.append(method)
        return real_execute(method=method, params=params, req_id=req_id, auth_context=auth_context)
    monkeypatch.setattr(mcp_gateway, "execute", spy)
    try:
        _connect(org)
        _connect(guest)
        plan = client.post("/api/meetings/orchestrate", json={
            "request": "Schedule an online team sync meeting with Orla Organizer and Gus Guest on 2030-01-09 from 10 AM to 11 AM to discuss the launch plan."
        }).json()
        if plan["status"] == "NEEDS_INFORMATION":
            plan = client.post(f"/api/meetings/{plan['meeting_id']}/clarify", json={
                "participant_emails": {"Orla Organizer": org, "Gus Guest": guest}
            }).json()
        meeting_id = plan["meeting_id"]
        # Full LangGraph path up to the gate, including RAG.
        assert plan["status"] == "WAITING_FOR_HUMAN_APPROVAL"
        # A private, organizer-only draft event obtains Meet before review. No invitations are sent before approval.
        assert len(live.of("events.insert")) == 1 and not live.of("gmail.send")
        draft_insert = live.of("events.insert")[0]
        assert draft_insert[2]["attendees"] == [] and draft_insert[4] == "none"
        assert plan["meet_url"] == "https://meet.google.com/abc-defg-hij" and plan["calendar_event_id"]

        approved = client.post(f"/api/meetings/{meeting_id}/approval", json={"action": "APPROVE"}).json()
        assert approved["success"] is True, approved
        assert approved["status"] == "WAITING_FOR_PARTICIPANTS"
        assert "calendar.create_event" in mcp_methods and "gmail.send" in mcp_methods

        assert len(live.of("events.insert")) == 1                               # approval updates the draft event
        update = live.of("events.patch")[-1]
        assert {a["email"] for a in update[3]["attendees"]} == {org, guest}    # registered emails only
        assert update[5] == "all"                                               # invites sent on approval
        meeting = client.get(f"/api/meetings/{meeting_id}").json()
        assert meeting["calendar_event_id"] in live.events                    # ID comes from Google
        assert meeting["meet_url"] == "https://meet.google.com/abc-defg-hij"  # link comes from Google
        assert live.of("gmail.send")[0][1] == org                              # sent through organizer's account

        # Real external responses: attendee status read from the Google event via MCP calendar.get_event.
        event = live.events[meeting["calendar_event_id"]]
        for attendee in event["attendees"]:
            attendee["responseStatus"] = "accepted"
        synced = client.post(f"/api/meetings/{meeting_id}/sync-responses").json()
        assert "calendar.get_event" in mcp_methods
        assert synced["status"] == "CONFIRMED"
        meeting = client.get(f"/api/meetings/{meeting_id}").json()
        assert all(p["notes"].startswith("[GOOGLE_CALENDAR]") for p in meeting["participants"])
        trace = client.get(f"/api/audit-logs/{meeting_id}").json()
        actions = [t["action"] for t in trace]
        assert "PROCESS_RESPONSES" in actions and "REVALIDATE" in actions and "FINALIZE" in actions
    finally:
        _cleanup(org, guest)


def test_google_api_failure_is_surfaced_with_status_reason_message(live):
    org = "fail.organizer@example.test"
    _employee(org, "Fiona Failure")
    try:
        _connect(org)
        live.fail_insert = _http_error(403, "insufficientPermissions", "Request had insufficient authentication scopes.")
        plan = client.post("/api/meetings/orchestrate", json={
            "request": "Schedule an online team sync meeting with Fiona Failure on 2030-01-08 from 9 AM to 10 AM to discuss the budget."
        }).json()
        if plan["status"] == "NEEDS_INFORMATION":
            plan = client.post(f"/api/meetings/{plan['meeting_id']}/clarify", json={
                "participant_emails": {"Fiona Failure": org}
            }).json()
        res = client.post(f"/api/meetings/{plan['meeting_id']}/approval", json={"action": "APPROVE"}).json()
        assert res["success"] is False
        assert "HTTP 403" in res["error"] and "insufficientPermissions" in res["error"]
        assert res["error_data"]["http_status"] == 403
        assert res["error_data"]["mcp_capability"] == "calendar.create_event"
        assert res["error_data"]["error_reason"] == "insufficientPermissions"
        meeting = client.get(f"/api/meetings/{plan['meeting_id']}").json()
        assert meeting["status"] == "ACTION_FAILED"
        assert meeting["calendar_event_id"] == "" and meeting["meet_url"] == ""   # no fabricated IDs/links
    finally:
        _cleanup(org)


def test_missing_meet_link_is_reported_not_faked(live):
    org = "nomeet.organizer@example.test"
    _employee(org, "Nora Nomeet")
    try:
        _connect(org)
        live.meet = False
        plan = client.post("/api/meetings/orchestrate", json={
            "request": "Schedule an online team sync meeting with Nora Nomeet on 2030-01-08 from 1 PM to 2 PM to discuss hiring."
        }).json()
        if plan["status"] == "NEEDS_INFORMATION":
            plan = client.post(f"/api/meetings/{plan['meeting_id']}/clarify", json={
                "participant_emails": {"Nora Nomeet": org}
            }).json()
        assert not plan["meet_url"] and "did not return a Google Meet link" in plan["meet_error"]
        res = client.post(f"/api/meetings/{plan['meeting_id']}/approval", json={"action": "APPROVE"}).json()
        assert res["success"] is True
        assert any("did not return a Google Meet link" in w for w in res["warnings"])
        assert client.get(f"/api/meetings/{plan['meeting_id']}").json()["meet_url"] == ""
    finally:
        _cleanup(org)


def test_live_mode_without_google_connection_fails_truthfully(live):
    person = "unconnected.person@example.test"
    _employee(person, "Una Unconnected")
    try:
        plan = client.post("/api/meetings/orchestrate", json={
            "request": "Schedule an online team sync meeting with Una Unconnected on 2030-01-08 from 3 PM to 4 PM to discuss onboarding."
        }).json()
        status = plan["participants"][0]["calendar_status"]
        assert status == "UNVERIFIED"
        # Refused at the gate (nothing is half-executed); the meeting stays reviewable.
        res = client.post(f"/api/meetings/{plan['meeting_id']}/approval", json={"action": "APPROVE"})
        assert res.status_code == 409 and "No employee has a connected Google account" in res.json()["detail"]
        assert client.get(f"/api/meetings/{plan['meeting_id']}").json()["status"] == "WAITING_FOR_HUMAN_APPROVAL"
    finally:
        _cleanup(person)


# ----------------- 7. Approval gate -----------------

def test_approval_gate_is_enforced_in_backend():
    from backend.graph.workflow_graph import resume_meeting_workflow
    plan = client.post("/api/meetings/orchestrate", json={
        "request": "Schedule an online team sync meeting with Alice Chen on 2030-01-10 at 11 AM for 30 minutes to discuss the gate."
    }).json()
    meeting_id = plan["meeting_id"]
    # Resuming at the gate without a human approval never reaches execution.
    state = resume_meeting_workflow(meeting_id, "APPROVAL")
    assert "node_execute" not in [t["node"] for t in state["trace"]]
    assert client.get(f"/api/meetings/{meeting_id}").json()["calendar_event_id"] == ""
    # The MCP endpoint cannot bypass the gate for consequential capabilities (it now also needs a login).
    mcp_call = {"method": "gmail.send", "params": {
        "to_emails": ["alice.chen@example.com"], "subject": "x", "body": "y", "meeting_id": meeting_id}}
    assert client.post("/api/mcp", json=mcp_call).status_code == 401
    reg = client.post("/api/auth/register", json={"email": f"gate.{uuid.uuid4().hex[:6]}@example.test",
                                                  "password": "secret123", "name": "Gate Organizer"}).json()
    db = SessionLocal()
    try:   # the caller organizes this (still unapproved) meeting, so the denial is the approval gate, as before
        db.query(Meeting).filter(Meeting.id == meeting_id).update({"organizer_id": reg["user"]["id"]})
        db.commit()
    finally:
        db.close()
    res = client.post("/api/mcp", json=mcp_call, headers={"Authorization": f"Bearer {reg['token']}"}).json()
    assert res["error"]["code"] == -32003
    # Approving something that is not waiting for approval is refused.
    client.post(f"/api/meetings/{meeting_id}/approval", json={"action": "REJECT"})
    assert client.post(f"/api/meetings/{meeting_id}/approval", json={"action": "APPROVE"}).status_code == 409


def test_edit_revalidates_same_meeting_and_keeps_human_changes():
    plan = client.post("/api/meetings/orchestrate", json={
        "request": "Schedule an online team sync meeting with Alice Chen and Bob Smith on 2030-01-10 at 11 AM for 30 minutes to discuss edits."
    }).json()
    meeting_id = plan["meeting_id"]
    res = client.post(f"/api/meetings/{meeting_id}/approval", json={"action": "EDIT", "edits": {
        "scheduled_start": "2030-01-08T10:00:00Z", "scheduled_end": "2030-01-08T10:45:00Z", "duration_minutes": 45,
        "agenda": "1. Custom agenda"}}).json()
    assert res["status"] == "WAITING_FOR_HUMAN_APPROVAL"
    meeting = client.get(f"/api/meetings/{meeting_id}").json()
    assert meeting["id"] == meeting_id
    assert meeting["scheduled_start"] == "2030-01-08T10:00:00Z" and meeting["agenda"] == "1. Custom agenda"
    assert "node_check_availability" in res["plan"]["workflow_trace"] and "node_validate" in res["plan"]["workflow_trace"]


# ----------------- 8. RAG influences decisions -----------------

def test_rag_context_reaches_agents():
    carol = "carol.davis@example.com"  # conftest: "No Friday meetings"
    db = SessionLocal()
    history_id = f"hist-{uuid.uuid4().hex[:6]}"
    db.add(Meeting(id=history_id, title="Meeting: Discuss quantum roadmap", raw_request="history", status="CONFIRMED",
                   purpose="Discuss quantum roadmap", agenda="Quantum roadmap review"))
    db.commit()
    db.close()
    try:
        plan = client.post("/api/meetings/orchestrate", json={
            "request": "Schedule an online team sync meeting with Carol Davis on 2030-01-11 from 11 AM to 12 PM to discuss quantum roadmap."
        }).json()
        assert history_id in [m["meeting_id"] for m in plan["rag_context"]["historical_meetings"]]
        assert plan["agenda"] == ""   # R9: related history is context only, never written into the agenda
        assert carol in plan["rag_context"]["participant_preferences"]
        assert any("Friday" in n and "long-term memory" in n for n in plan["critique_notes"])
    finally:
        db = SessionLocal()
        db.query(Meeting).filter(Meeting.id == history_id).delete()
        db.commit()
        db.close()


# ----------------- 9. Participant identity -----------------

def test_ambiguous_name_is_never_auto_resolved():
    plan = client.post("/api/meetings/orchestrate", json={
        "request": "Schedule an online team sync meeting with David on 2030-01-10 at 11 AM for 30 minutes to discuss research."
    }).json()
    meeting_id = plan["meeting_id"]
    assert plan["status"] == "NEEDS_INFORMATION" and plan["participants"] == []
    candidates = {c["email"] for c in plan["ambiguous_participants"][0]["candidates"]}
    assert {"david.miller@example.com", "david.martinez@example.com"} <= candidates
    # Only one of the registered candidates may be chosen.
    bad = client.post(f"/api/meetings/{meeting_id}/resolve-participant",
                      json={"queried_name": "David", "selected_email": "someone@example.com"})
    assert bad.status_code == 400
    ok = client.post(f"/api/meetings/{meeting_id}/resolve-participant",
                     json={"queried_name": "David", "selected_email": "david.miller@example.com"}).json()
    assert ok["status"] == "WAITING_FOR_HUMAN_APPROVAL"
    meeting = client.get(f"/api/meetings/{meeting_id}").json()
    assert [p["email"] for p in meeting["participants"]] == ["david.miller@example.com"]
    assert meeting["ambiguous_participants"] == []


# ----------------- 10. Alternative slot -----------------

def test_busy_slot_routes_to_alternative(monkeypatch):
    from backend.connectors.calendar_service import MockCalendarService

    def busy(self, emails, start_time, end_time):
        return {"all_available": False, "details": {e: {"status": "BUSY", "reason": "Busy"} for e in emails}}
    monkeypatch.setattr(MockCalendarService, "check_availability", busy)
    plan = client.post("/api/meetings/orchestrate", json={
        "request": "Schedule an online team sync meeting with Alice Chen on 2030-01-10 from 3 PM to 4 PM to discuss alternatives."
    }).json()
    assert "node_find_alternative_slot" in plan["workflow_trace"]
    assert plan["scheduled_start"] != plan["requested_slot"]["start"]
    assert any(c["status"] == "RESOLVED" for c in plan["conflicts"])


# ----------------- 11. Auditorium workflow truthfulness -----------------

def test_auditorium_email_reply_classification():
    from worker.worker import BackgroundWorker
    assert BackgroundWorker.classify_auditorium_reply("Re: request — Confirmed, room is booked") == "CONFIRMED"
    assert BackgroundWorker.classify_auditorium_reply("Sorry, the auditorium is unavailable") == "REJECTED"
    assert BackgroundWorker.classify_auditorium_reply("Can we talk tomorrow?") is None


# ----------------- 12. Live data only -----------------

def test_employee_defaults_do_not_invent_data():
    email = "plain.person@example.test"
    try:
        res = client.post("/api/employees", json={"employee_id": "PLAIN-1", "name": "Plain Person", "email": email,
                                                  "designation": "Analyst", "department": "Ops"}).json()
        assert res["meeting_preferences"] == "" and res["location"] == ""
        assert client.post("/api/employees", json={"employee_id": "X", "name": "X", "email": "not-an-email",
                                                   "designation": "a", "department": "b"}).status_code == 422
    finally:
        _cleanup(email)


# ----------------- 13. Credentials are configuration-driven -----------------

def test_google_client_configuration_is_switchable(oauth_env, monkeypatch):
    """Swapping test credentials for organization credentials is configuration only."""
    email = "config.user@example.test"
    emp_id = _employee(email, "Config User")
    try:
        monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "org-production-client.apps.example")
        monkeypatch.setattr(settings, "GOOGLE_REDIRECT_URI", "https://meetings.example.org/api/auth/google/callback")
        monkeypatch.setattr(settings, "GOOGLE_HOSTED_DOMAIN", "example.org")
        query = _start_oauth(emp_id)
        assert query["client_id"] == ["org-production-client.apps.example"]
        assert query["redirect_uri"] == ["https://meetings.example.org/api/auth/google/callback"]
        assert query["hd"] == ["example.org"] and query["login_hint"] == [email]
        assert set(query["scope"][0].split()) == set(settings.google_scopes)
        info = client.get("/api/system-info").json()
        assert "csecret" not in json.dumps(info)  # secrets are never exposed to the frontend
    finally:
        _cleanup(email)


def test_rejection_edit_and_reapproval_update_the_same_google_event(live):
    org, guest = "reapprove.org@example.test", "reapprove.guest@example.test"
    _employee(org, "Rita Reapprove")
    _employee(guest, "Gil Guest")
    try:
        _connect(org)
        _connect(guest)
        plan = client.post("/api/meetings/orchestrate", json={
            "request": "Schedule an online team sync meeting with Rita Reapprove and Gil Guest on 2030-01-08 from 9 AM to 10 AM to discuss pricing."
        }).json()
        mid = plan["meeting_id"]
        assert client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"}).json()["success"]
        first = client.get(f"/api/meetings/{mid}").json()
        # The guest declines in Google Calendar; Refresh reads it -> same meeting needs rescheduling
        _google_reply(live, first["calendar_event_id"], guest, "declined")
        client.post(f"/api/meetings/{mid}/sync-responses")
        declined = client.get(f"/api/meetings/{mid}").json()
        assert declined["status"] == "RESCHEDULING_REQUIRED"
        assert [p["response_status"] for p in declined["participants"] if p["email"] == guest] == ["REJECTED"]
        edited = client.post(f"/api/meetings/{mid}/approval", json={"action": "EDIT", "edits": {
            "scheduled_start": "2030-01-08T11:00:00Z", "scheduled_end": "2030-01-08T12:00:00Z"}}).json()
        assert edited["status"] == "WAITING_FOR_HUMAN_APPROVAL"
        again = client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"}).json()
        assert again["success"] is True
        second = client.get(f"/api/meetings/{mid}").json()
        assert len(live.of("events.insert")) == 1 and len(live.of("events.patch")) == 1
        assert second["calendar_event_id"] == first["calendar_event_id"]
        assert second["meet_url"] == first["meet_url"]
        assert all(p["response_status"] == "PENDING" for p in second["participants"])  # new round of responses
        assert "conferenceData" not in live.of("events.patch")[0][3]
    finally:
        _cleanup(org, guest)


# ----------------- R6. Employee profile form -----------------

def test_employee_working_hours_normalized_and_validated():
    email = "hours.person@example.test"
    try:
        res = client.post("/api/employees", json={"employee_id": "HRS-1", "name": "Hours Person", "email": email,
                                                  "designation": "Analyst", "department": "Ops",
                                                  "working_hours_start": "9:30 AM", "working_hours_end": "6:00 PM"})
        assert res.status_code == 200, res.text
        body = res.json()
        assert (body["working_hours_start"], body["working_hours_end"]) == ("09:30:00", "18:00:00")
        assert body["timezone"] == settings.MEETING_TIMEZONE  # not asked in the form
        same = client.post("/api/employees", json={"employee_id": "HRS-2", "name": "Same", "email": "same.hours@example.test",
                                                   "designation": "a", "department": "b",
                                                   "working_hours_start": "09:00", "working_hours_end": "09:00"})
        assert same.status_code == 422
        bad = client.put(f"/api/employees/by-email/{email}", json={"working_hours_start": "25:00"})
        assert bad.status_code == 422
        overnight = client.put(f"/api/employees/by-email/{email}", json={"working_hours_start": "22:00:00", "working_hours_end": "06:00:00"})
        assert overnight.status_code == 200 and overnight.json()["working_hours_end"] == "06:00:00"
    finally:
        _cleanup(email)


def test_overnight_working_hours_check():
    from backend.skills.scheduling_skill import MeetingSchedulingSkill
    night = [{"name": "Nina", "email": "n@x", "timezone": "UTC", "working_days": "Monday,Tuesday,Wednesday,Thursday,Friday",
              "working_hours": "22:00:00 - 06:00:00"}]
    check = MeetingSchedulingSkill.working_hours_conflicts
    assert check("2030-01-08T23:00:00Z", "2030-01-09T00:30:00Z", night) == []   # inside the night shift
    assert check("2030-01-08T03:00:00Z", "2030-01-08T05:00:00Z", night) == []
    assert check("2030-01-08T05:30:00Z", "2030-01-08T06:30:00Z", night)         # runs past shift end
    assert check("2030-01-08T14:00:00Z", "2030-01-08T15:00:00Z", night)         # daytime


# ----------------- R3 / R10. Clarifying questions, title vs purpose -----------------

def test_incomplete_request_asks_instead_of_inventing_and_resumes_same_meeting():
    from backend.agents.coordinator_agent import MeetingCoordinator
    plan = client.post("/api/meetings/orchestrate", json={"request": "Schedule a project review with Alice Chen tomorrow."}).json()
    mid = plan["meeting_id"]
    assert plan["status"] == "NEEDS_INFORMATION"
    assert set(plan["missing_fields"]) == {"purpose", "start_time", "duration", "mode"}  # title/date/attendee were given
    understood = plan["understood"]
    tomorrow = (datetime.now(MeetingCoordinator._tz()) + timedelta(days=1)).strftime("%Y-%m-%d")
    assert understood["title"] == "Project Review" and understood["date"] == tomorrow
    assert understood["start_clock"] is None and understood["duration_minutes"] is None and understood["mode"] is None
    assert plan["scheduled_start"] in ("", None) and plan["agenda"] == ""   # nothing invented, nothing scheduled
    assert [p["email"] for p in plan["participants"]] == ["alice.chen@example.com"]

    # Partial answer: still asks only for what is missing
    partial = client.post(f"/api/meetings/{mid}/clarify", json={"start_time": "10:00", "duration_minutes": 45}).json()
    assert partial["meeting_id"] == mid and partial["status"] == "NEEDS_INFORMATION"
    assert set(partial["missing_fields"]) == {"purpose", "mode"}

    done = client.post(f"/api/meetings/{mid}/clarify", json={"mode": "online", "purpose": "Review progress and agree next steps"}).json()
    assert done["meeting_id"] == mid and done["status"] == "WAITING_FOR_HUMAN_APPROVAL", done
    meeting = client.get(f"/api/meetings/{mid}").json()
    assert meeting["title"] == "Project Review" and meeting["purpose"] == "Review progress and agree next steps"
    expected_start = MeetingCoordinator._to_utc_iso(tomorrow, 10, 0)
    assert meeting["duration_minutes"] == 45 and meeting["scheduled_start"] == expected_start
    assert meeting["missing_fields"] == []


def test_clarify_validation_and_ambiguous_dates():
    plan = client.post("/api/meetings/orchestrate", json={
        "request": "Schedule an online roadmap review with Alice Chen next Monday at 10 AM for 30 minutes to discuss the roadmap."}).json()
    date_q = [q for q in plan["questions"] if q["field"] == "date"]
    assert date_q and "Next Monday" in date_q[0]["note"]
    bad = client.post(f"/api/meetings/{plan['meeting_id']}/clarify", json={"mode": "hybrid", "date": "12/01/2030"})
    assert bad.status_code == 422
    ok = client.post(f"/api/meetings/{plan['meeting_id']}/clarify", json={"date": "2030-01-14"}).json()
    assert ok["status"] == "WAITING_FOR_HUMAN_APPROVAL" and ok["scheduled_start"] == "2030-01-14T10:00:00Z"


def test_offline_requires_venue_and_attendees_can_be_added_while_clarifying():
    plan = client.post("/api/meetings/orchestrate", json={
        "request": "Schedule an in-person design review with Alice Chen on 2030-01-10 at 11 AM for 30 minutes to discuss the mockups."}).json()
    assert plan["missing_fields"] == ["venue"] and plan["room_name"] == ""   # no venue assumed
    res = client.post(f"/api/meetings/{plan['meeting_id']}/clarify", json={"venue": "Design Studio, 3rd floor", "attendees": ["Bob Smith"]}).json()
    assert res["status"] == "WAITING_FOR_HUMAN_APPROVAL", res
    assert res["room_name"] == "Design Studio, 3rd floor"
    assert {p["email"] for p in res["participants"]} == {"alice.chen@example.com", "bob.smith@example.com"}


def test_parser_never_fills_defaults():
    from backend.agents.coordinator_agent import MeetingCoordinator
    c = MeetingCoordinator()
    p = c.parse_natural_language_request("Schedule a meeting with Alice Chen")
    assert p["mode"] is None and p["date"] is None and p["start_clock"] is None and p["duration_minutes"] is None
    assert p["title"] is None and p["purpose"] is None
    p = c.parse_natural_language_request("Schedule a Q3 project review with Alice Chen on 2030-01-10 from 2 PM to 3 PM online to review progress.")
    assert p["title"] == "Q3 Project Review" and p["purpose"] == "Review progress" and p["missing"] == []
    assert c.parse_natural_language_request("Schedule a sync with Bob on 2030-01-10 at 3")["ambiguities"]["start_time"]
    assert "date" in c.parse_natural_language_request("Schedule a sync with Bob on 2020-01-10 at 3 PM")["ambiguities"]


# ----------------- R4. Unregistered attendees -----------------

def test_unregistered_attendee_is_reported_not_given_an_email_box():
    plan = client.post("/api/meetings/orchestrate", json={
        "request": "Schedule an online team sync meeting with Alice Chen and Zed Newperson on 2030-01-10 at 11 AM for 30 minutes to discuss onboarding."}).json()
    mid = plan["meeting_id"]
    assert plan["status"] == "NEEDS_INFORMATION"
    unknown = plan["unknown_participants"][0]
    assert unknown["queried_name"] == "Zed Newperson"
    assert unknown["message"] == ("The attendee Zed Newperson is not registered in the employee directory. "
                                  "Please ensure that the employee is registered before scheduling the meeting.")
    # Supplying an e-mail for an unregistered person is refused (no invented/auto-created attendees).
    refused = client.post(f"/api/meetings/{mid}/resolve-participant", json={"queried_name": "Zed Newperson", "new_email": "zed@x.com"})
    assert refused.status_code == 400
    # Register the employee, then re-check the same meeting.
    email = "zed.newperson@example.test"
    try:
        client.post("/api/employees", json={"employee_id": "ZED-1", "name": "Zed Newperson", "email": email,
                                            "designation": "Engineer", "department": "R&D"})
        res = client.post(f"/api/meetings/{mid}/recheck-participant", json={"queried_name": "Zed Newperson"}).json()
        assert res["meeting_id"] == mid and res["status"] == "WAITING_FOR_HUMAN_APPROVAL", res
        assert {p["email"] for p in res["participants"]} == {"alice.chen@example.com", email}
    finally:
        _cleanup(email)


def test_unregistered_email_and_removal():
    plan = client.post("/api/meetings/orchestrate", json={
        "request": "Schedule an online team sync meeting with Alice Chen and someone@outside.org on 2030-01-10 at 11 AM for 30 minutes to discuss sales."}).json()
    assert [u["queried_name"] for u in plan["unknown_participants"]] == ["someone@outside.org"]
    assert all(p["email"] != "someone@outside.org" for p in plan["participants"])   # not added as a guest
    res = client.post(f"/api/meetings/{plan['meeting_id']}/remove-participant", json={"queried_name": "someone@outside.org"}).json()
    assert res["status"] == "WAITING_FOR_HUMAN_APPROVAL" and [p["email"] for p in res["participants"]] == ["alice.chen@example.com"]


def test_ambiguous_is_not_reported_as_unregistered():
    plan = client.post("/api/meetings/orchestrate", json={
        "request": "Schedule an online team sync meeting with David on 2030-01-10 at 11 AM for 30 minutes to discuss research."}).json()
    assert plan["unknown_participants"] == [] and plan["ambiguous_participants"][0]["queried_name"] == "David"


# ----------------- R9 / R11. Organizer agenda points, venue -----------------

def test_agenda_points_flow_through_review_invite_and_calendar(live):
    org = "agenda.org@example.test"
    _employee(org, "Ada Agenda")
    try:
        _connect(org)
        plan = client.post("/api/meetings/orchestrate", json={
            "request": "Schedule an online roadmap review with Ada Agenda on 2030-01-08 from 9 AM to 10 AM to review the roadmap."}).json()
        mid = plan["meeting_id"]
        assert plan["agenda"] == "" and plan["details_confirmed"] is False     # nothing generated
        res = client.post(f"/api/meetings/{mid}/clarify", json={"agenda_points": ["  q3 milestones", "", "risks;", "next steps"]}).json()
        assert res["status"] == "WAITING_FOR_HUMAN_APPROVAL" and res["details_confirmed"] is True
        assert res["agenda_points"] == ["Q3 milestones", "Risks", "Next steps"]
        assert res["agenda"] == "1. Q3 milestones\n2. Risks\n3. Next steps"
        # Edit in review with the same structured points (order preserved, empties dropped)
        edited = client.post(f"/api/meetings/{mid}/approval", json={"action": "EDIT", "edits": {
            "agenda_points": ["Risks", "Q3 milestones", " "]}}).json()
        assert edited["meeting"]["agenda"] == "1. Risks\n2. Q3 milestones"
        client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"})
        insert = live.of("events.insert")[0][2]
        assert "1. Risks\n2. Q3 milestones" in insert["description"] and "Purpose: Review the roadmap" in insert["description"]
        import base64, email
        sent = email.message_from_bytes(base64.urlsafe_b64decode(live.of("gmail.send")[0][2]["raw"]))
        assert "1. Risks\n2. Q3 milestones" in sent.get_payload(decode=True).decode()
    finally:
        _cleanup(org)


def test_offline_invite_names_venue_without_map_link_and_sends_no_venue_request(live):
    org = "venue.org@example.test"
    _employee(org, "Vera Venue")
    try:
        _connect(org)
        plan = client.post("/api/meetings/orchestrate", json={
            "request": "Schedule an in-person planning session with Vera Venue on 2030-01-08 from 9 AM to 10 AM to plan the offsite."}).json()
        assert plan["missing_fields"] == ["venue"]
        ready = client.post(f"/api/meetings/{plan['meeting_id']}/clarify", json={"venue": "Cafeteria, 2nd floor"}).json()
        assert ready["status"] == "WAITING_FOR_HUMAN_APPROVAL" and ready["room_name"] == "Cafeteria, 2nd floor"
        res = client.post(f"/api/meetings/{ready['meeting_id']}/approval", json={"action": "APPROVE"}).json()
        assert res["success"] is True and res["status"] == "WAITING_FOR_PARTICIPANTS"
        insert = live.of("events.insert")[0]
        assert not insert[2].get("location") and "conferenceData" not in insert[2]   # no map link, no Meet for offline
        assert "Venue: Cafeteria, 2nd floor" in insert[2]["description"]
        sent_to = [c for c in live.of("gmail.send")]
        assert len(sent_to) == 1   # attendee invitation only; the venue request is sent separately by the organizer
        assert client.get(f"/api/meetings/{ready['meeting_id']}").json()["room_bookings"] == []
    finally:
        _cleanup(org)


# ----------------- R8 / R13A. Details + Refresh -----------------

def test_refresh_all_reads_real_responses_and_details_is_fresh(live):
    org, guest = "refresh.org@example.test", "refresh.guest@example.test"
    _employee(org, "Rhea Refresh")
    _employee(guest, "Gus Refresh")
    try:
        _connect(org)
        _connect(guest)
        plan = client.post("/api/meetings/orchestrate", json={
            "request": "Schedule an online status review with Rhea Refresh and Gus Refresh on 2030-01-09 from 10 AM to 11 AM to review status."}).json()
        mid = plan["meeting_id"]
        client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"})
        event = live.events[client.get(f"/api/meetings/{mid}").json()["calendar_event_id"]]
        for attendee in event["attendees"]:
            attendee["responseStatus"] = "accepted"
        res = client.post("/api/meetings/sync-responses").json()
        assert [e for e in res["errors"] if e["meeting_id"] == mid] == []   # other meetings may report their own errors
        details = client.get(f"/api/meetings/{mid}").json()
        assert details["status"] == "CONFIRMED"
        assert {p["response_status"] for p in details["participants"]} == {"ACCEPTED"}
        assert client.get("/api/meetings/does-not-exist").status_code == 404
    finally:
        _cleanup(org, guest)


# ----------------- R12. Availability, double booking, re-checks -----------------

def _plan(request):
    return client.post("/api/meetings/orchestrate", json={"request": request}).json()


def test_google_busy_attendee_is_reported_and_statuses_are_real(live):
    a, b = "busy.a@example.test", "free.b@example.test"
    _employee(a, "Asha Busy")
    _employee(b, "Bala Free")
    c = "unconnected.c@example.test"
    _employee(c, "Chen Unconnected")
    try:
        _connect(a)
        _connect(b)
        live.busy[a] = [{"start": "2030-01-08T09:00:00Z", "end": "2030-01-08T10:00:00Z"}]
        plan = _plan("Schedule an online design review with Asha Busy, Bala Free and Chen Unconnected on 2030-01-08 from 9 AM to 9:30 AM to review designs.")
        # The requested 9 AM is busy for Asha, so an alternative is proposed; statuses describe THAT proposed slot.
        assert plan["scheduled_start"] != "2030-01-08T09:00:00Z"
        statuses = {p["email"]: p["calendar_status"] for p in plan["participants"]}
        assert statuses == {a: "AVAILABLE", b: "AVAILABLE", c: "UNVERIFIED"}     # never "available" without proof
        texts = " ".join(x["conflict"] for x in plan["conflicts"])
        assert "Asha Busy is not available during the selected time slot" in texts
        assert "Availability could not be verified for unconnected.c@example.test" in texts
    finally:
        _cleanup(a, b, c)


def test_confirmed_meeting_blocks_overlapping_booking():
    from worker.state.state_manager import state_manager
    first = _plan("Schedule an online team sync meeting with Alice Chen on 2030-01-10 from 2 PM to 3 PM to discuss the plan.")
    client.post(f"/api/meetings/{first['meeting_id']}/approval", json={"action": "APPROVE"})
    client.post(f"/api/meetings/{first['meeting_id']}/simulate-participant", json={"email": "alice.chen@example.com", "response": "ACCEPTED"})
    assert client.get(f"/api/meetings/{first['meeting_id']}").json()["status"] == "CONFIRMED"

    second = _plan("Schedule an online design review with Alice Chen on 2030-01-10 from 2:30 PM to 3:30 PM to review designs.")
    assert second["scheduled_start"] != "2030-01-10T09:00:00Z"   # the busy requested slot is not kept
    assert any("Alice Chen is not available during the selected time slot" in c["conflict"] for c in second["conflicts"])
    # Demo slot search proposes alternatives; none may overlap the confirmed meeting.
    for alt in second["alternative_slots"]:
        assert not state_manager.find_conflicting_meetings(["alice.chen@example.com"], alt["start"], alt["end"],
                                                           exclude_meeting_id=second["meeting_id"], include_tentative=False)


def test_tentative_reservation_warns_and_blocks_second_approval():
    first = _plan("Schedule an online team sync meeting with Bob Smith on 2030-01-10 from 4 PM to 5 PM to discuss the plan.")
    client.post(f"/api/meetings/{first['meeting_id']}/approval", json={"action": "APPROVE"})   # waiting for Bob
    second = _plan("Schedule an online budget review with Bob Smith on 2030-01-10 from 4:30 PM to 5 PM to review the budget.")
    assert any("tentatively reserved" in c["conflict"] for c in second["conflicts"])
    assert second["status"] == "WAITING_FOR_HUMAN_APPROVAL"   # warning only, organizer decides
    res = client.post(f"/api/meetings/{second['meeting_id']}/approval", json={"action": "APPROVE"}).json()
    assert res["success"] is False and "Bob Smith is not available during the selected time slot" in res["error"]
    assert client.get(f"/api/meetings/{second['meeting_id']}").json()["status"] == "RESCHEDULING_REQUIRED"


def test_concurrent_approvals_cannot_double_book():
    import threading
    plans = [_plan(f"Schedule an online team sync meeting with Carol Davis on 2030-01-09 from 10 AM to 11 AM to discuss item {i}.")
             for i in range(2)]
    for p in plans:
        db = SessionLocal()
        db.query(Meeting).filter(Meeting.id == p["meeting_id"]).update({"status": "APPROVED", "approval_status": "APPROVED"})
        db.commit()
        db.close()
    from worker.worker import background_worker
    results = []
    threads = [threading.Thread(target=lambda mid=p["meeting_id"]: results.append(background_worker.meeting_job.execute(mid))) for p in plans]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r["success"] for r in results) == [False, True]
    statuses = sorted(client.get(f"/api/meetings/{p['meeting_id']}").json()["status"] for p in plans)
    assert statuses == ["RESCHEDULING_REQUIRED", "WAITING_FOR_PARTICIPANTS"]


def test_availability_is_rechecked_right_before_invitations(live):
    a = "late.busy@example.test"
    _employee(a, "Lata Late")
    try:
        _connect(a)
        plan = _plan("Schedule an online team sync meeting with Lata Late on 2030-01-08 from 1 PM to 2 PM to discuss the release.")
        assert plan["status"] == "WAITING_FOR_HUMAN_APPROVAL"
        live.busy[a] = [{"start": "2030-01-08T13:30:00Z", "end": "2030-01-08T14:30:00Z"}]   # calendar changed after the draft
        res = client.post(f"/api/meetings/{plan['meeting_id']}/approval", json={"action": "APPROVE"}).json()
        assert res["success"] is False and "Lata Late is not available during the selected time slot" in res["error"]
        assert not live.of("events.insert") and not live.of("gmail.send")      # nothing sent
        assert client.get(f"/api/meetings/{plan['meeting_id']}").json()["status"] == "RESCHEDULING_REQUIRED"
    finally:
        _cleanup(a)


def test_confirmation_is_rechecked_against_confirmed_meetings():
    first = _plan("Schedule an online team sync meeting with David Miller on 2030-01-11 from 9 AM to 10 AM to discuss infra.")
    second = _plan("Schedule an online team sync meeting with David Miller on 2030-01-11 from 11 AM to 12 PM to discuss costs.")
    for p in (first, second):
        assert client.post(f"/api/meetings/{p['meeting_id']}/approval", json={"action": "APPROVE"}).json()["success"]
    client.post(f"/api/meetings/{first['meeting_id']}/simulate-participant", json={"email": "david.miller@example.com", "response": "ACCEPTED"})
    # Simulate a race where the second meeting ended up overlapping the now-confirmed first one.
    db = SessionLocal()
    db.query(Meeting).filter(Meeting.id == second["meeting_id"]).update(
        {"scheduled_start": "2030-01-11T09:30:00Z", "scheduled_end": "2030-01-11T10:30:00Z"})
    db.commit()
    db.close()
    res = client.post(f"/api/meetings/{second['meeting_id']}/simulate-participant", json={"email": "david.miller@example.com", "response": "ACCEPTED"}).json()
    assert res["status"] == "RESCHEDULING_REQUIRED"



# ----------------- R13B. Delete a meeting -----------------

def test_delete_draft_soft_deletes_and_keeps_audit():
    plan = _plan("Schedule an online team sync meeting with Alice Chen on 2030-01-10 from 9 AM to 10 AM to discuss the draft.")
    mid = plan["meeting_id"]
    res = client.delete(f"/api/meetings/{mid}").json()
    assert res["success"] and res["cancelled"] == []
    assert mid not in [m["id"] for m in client.get("/api/meetings").json()]
    assert client.get(f"/api/meetings/{mid}").status_code == 404
    actions = [t["action"] for t in client.get(f"/api/audit-logs/{mid}").json()]
    assert "INTAKE" in actions and "MEETING_DELETED" in actions          # history preserved
    db = SessionLocal()
    assert db.query(Meeting).filter(Meeting.id == mid).one().status == "DELETED"
    db.close()


def test_delete_scheduled_meeting_cancels_google_event_and_releases_slot(live):
    org = "delete.org@example.test"
    _employee(org, "Dina Delete")
    try:
        _connect(org)
        plan = _plan("Schedule an online team sync meeting with Dina Delete on 2030-01-08 from 3 PM to 4 PM to discuss the plan.")
        mid = plan["meeting_id"]
        client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"})
        event_id = client.get(f"/api/meetings/{mid}").json()["calendar_event_id"]
        res = client.delete(f"/api/meetings/{mid}").json()
        assert res["success"] and res["cancelled"] == ["calendar_event_cancelled"]
        assert live.of("events.delete") == [("events.delete", org, event_id, "all")]   # Google notifies attendees
        again = _plan("Schedule an online review with Dina Delete on 2030-01-08 from 3 PM to 4 PM to review the plan.")
        assert not any("not available" in c["conflict"] for c in again["conflicts"])  # slot released
    finally:
        _cleanup(org)


def test_delete_fails_visibly_when_google_cancel_fails(live):
    org = "delete.fail@example.test"
    _employee(org, "Dev Fail")
    try:
        _connect(org)
        plan = _plan("Schedule an online team sync meeting with Dev Fail on 2030-01-08 from 5 PM to 6 PM to discuss risks.")
        mid = plan["meeting_id"]
        client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"})
        live.fail_delete = _http_error(403, "forbidden", "The user does not have permission.")
        res = client.delete(f"/api/meetings/{mid}")
        assert res.status_code == 502 and "HTTP 403" in res.json()["detail"]
        assert client.get(f"/api/meetings/{mid}").json()["status"] == "WAITING_FOR_PARTICIPANTS"   # not hidden
    finally:
        _cleanup(org)


def test_reject_after_decline_cancels_the_event(live):
    org, guest = "reject.org@example.test", "reject.guest@example.test"
    _employee(org, "Ravi Reject")
    _employee(guest, "Gita Guest")
    try:
        _connect(org)
        plan = _plan("Schedule an online team sync meeting with Ravi Reject and Gita Guest on 2030-01-08 from 6 PM to 7 PM to discuss scope.")
        mid = plan["meeting_id"]
        client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"})
        _google_reply(live, client.get(f"/api/meetings/{mid}").json()["calendar_event_id"], guest, "declined")
        client.post(f"/api/meetings/{mid}/sync-responses")
        assert client.post(f"/api/meetings/{mid}/approval", json={"action": "REJECT"}).json()["status"] == "REJECTED"
        assert len(live.of("events.delete")) == 1
    finally:
        _cleanup(org, guest)


def test_delete_refused_while_executing():
    plan = _plan("Schedule an online team sync meeting with Alice Chen on 2030-01-10 from 1 PM to 2 PM to discuss timing.")
    db = SessionLocal()
    db.query(Meeting).filter(Meeting.id == plan["meeting_id"]).update({"status": "EXECUTING"})
    db.commit()
    db.close()
    assert client.delete(f"/api/meetings/{plan['meeting_id']}").status_code == 409


# ----------------- Live-mode readiness before approval (missing Google connection / configuration) -----------------

def test_approval_without_connected_google_is_refused_before_anything_runs(live):
    a, b = "nocred.a@example.test", "nocred.b@example.test"
    _employee(a, "Nora Nocred")
    _employee(b, "Ned Nocred")
    try:
        plan = client.post("/api/meetings/orchestrate", json={
            "request": "Schedule an online team sync meeting with Nora Nocred and Ned Nocred on 2030-01-08 from 9 AM to 10 AM to discuss the budget."
        }).json()
        mid = plan["meeting_id"]
        meeting = client.get(f"/api/meetings/{mid}").json()
        assert meeting["status"] == "WAITING_FOR_HUMAN_APPROVAL"
        assert meeting["execution_readiness"]["ready"] is False                    # the review screen is told up front
        assert meeting["execution_readiness"]["error_code"] == "NO_CREDENTIALS"
        res = client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"})
        assert res.status_code == 409 and "connect Google Calendar" in res.json()["detail"]
        after = client.get(f"/api/meetings/{mid}").json()
        assert after["status"] == "WAITING_FOR_HUMAN_APPROVAL" and after["approval_status"] != "APPROVED"
        assert live.of("events.insert") == []                                      # nothing was sent
        # Once the organizer connects Google, the same plan can be approved.
        _connect(a)
        assert client.get(f"/api/meetings/{mid}").json()["execution_readiness"]["ready"] is True
        ok = client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"}).json()
        assert ok["success"] is True and len(live.of("events.insert")) == 1
    finally:
        _cleanup(a, b)


def test_failed_send_can_be_retried_after_the_cause_is_fixed(live):
    org = "retry.org@example.test"
    _employee(org, "Rory Retry")
    try:
        _connect(org)
        live.fail_insert = _http_error(403, "insufficientPermissions", "Request had insufficient authentication scopes.")
        mid = client.post("/api/meetings/orchestrate", json={
            "request": "Schedule an online team sync meeting with Rory Retry on 2030-01-08 from 9 AM to 10 AM to discuss the budget."
        }).json()["meeting_id"]
        assert client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"}).json()["success"] is False
        failed = client.get(f"/api/meetings/{mid}").json()
        assert failed["status"] == "ACTION_FAILED" and failed["execution_readiness"]["ready"] is True
        live.fail_insert = None   # e.g. the organizer reconnected with the right permissions
        retry = client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"}).json()
        assert retry["success"] is True, retry
        done = client.get(f"/api/meetings/{mid}").json()
        assert done["status"] == "WAITING_FOR_PARTICIPANTS" and done["calendar_event_id"] and done["meet_url"]
        assert done["execution_error"] is None
    finally:
        _cleanup(org)


def test_revoked_refresh_token_disconnects_records_audit_and_falls_back(live, monkeypatch):
    """An expired/revoked refresh token (e.g. Google 'Testing' apps after 7 days) disconnects that account, is
    recorded in the audit log, and approval falls back to another connected organizer."""
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from backend.memory.audit_memory import audit_memory
    stale, good = "stale.org@example.test", "good.org@example.test"
    _employee(stale, "Sam Stale")
    _employee(good, "Gina Good")
    try:
        _connect(stale)
        _connect(good)
        db = SessionLocal()
        emp = db.query(Employee).filter(Employee.email == stale).first()
        tokens = json.loads(emp.google_tokens)
        tokens["expiry"] = "2020-01-01T00:00:00Z"            # forces a refresh
        emp.google_tokens = json.dumps(tokens)
        db.commit(); db.close()

        def refresh(self, request):
            raise RefreshError("invalid_grant: Token has been expired or revoked.")
        monkeypatch.setattr(Credentials, "refresh", refresh)
        monkeypatch.setattr(settings, "GOOGLE_CALENDAR_OWNER_EMAIL", stale)
        mid = client.post("/api/meetings/orchestrate", json={
            "request": "Schedule an online team sync meeting with Sam Stale and Gina Good on 2030-01-08 from 9 AM to 10 AM to discuss the budget."
        }).json()["meeting_id"]
        res = client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"}).json()
        assert res["success"] is True, res
        assert live.of("events.insert")[0][1] == good                      # organized by the still-connected account
        assert client.get(f"/api/employees/by-email/{stale}").json()["google_calendar_connected"] is False
        assert any(l["action"] == "GOOGLE_ACCOUNT_DISCONNECTED" and l["details"].get("employee_email") == stale
                   for l in audit_memory.get_recent_logs(50))
    finally:
        _cleanup(stale, good)


# ----------------- Live mode: no simulations; real responses from Google -----------------

def test_simulated_responses_are_refused_in_live_mode(live):
    org = "nosim.org@example.test"
    _employee(org, "Nina Nosim")
    try:
        _connect(org)
        mid = _plan("Schedule an online team sync meeting with Nina Nosim on 2030-01-08 from 9 AM to 10 AM to discuss scope.")["meeting_id"]
        client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"})
        res = client.post(f"/api/meetings/{mid}/simulate-participant", json={"email": org, "response": "ACCEPTED"})
        assert res.status_code == 403 and "DEMO_MODE" in res.json()["detail"]
        assert client.post(f"/api/meetings/{mid}/simulate-room", json={"room_name": "X", "confirmed": True}).status_code == 403
        assert client.get("/api/system-info").json()["demo_mode"] is False      # the UI hides the simulator from this
    finally:
        _cleanup(org)


def test_google_decline_is_shown_after_refresh_and_organizer_counts_as_attending(live, monkeypatch):
    org, guest = "sync.org@example.test", "sync.guest@example.test"
    _employee(org, "Omar Organizer")
    _employee(guest, "Gauri Guest")
    monkeypatch.setattr(settings, "GOOGLE_CALENDAR_OWNER_EMAIL", org)
    try:
        _connect(org)
        _connect(guest)
        mid = _plan("Schedule an online team sync meeting with Omar Organizer and Gauri Guest on 2030-01-08 from 9 AM to 10 AM to discuss scope.")["meeting_id"]
        client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"})
        event_id = client.get(f"/api/meetings/{mid}").json()["calendar_event_id"]
        # Nobody answered yet: the organizer is attending their own event (Google never asks them to RSVP).
        client.post("/api/meetings/sync-responses")
        m = client.get(f"/api/meetings/{mid}").json()
        responses = {p["email"]: p["response_status"] for p in m["participants"]}
        assert responses == {org: "ACCEPTED", guest: "PENDING"} and m["status"] == "WAITING_FOR_PARTICIPANTS"
        assert m["google_sync"]["attendees"][guest] == "needsAction" and m["google_sync"]["checked_at"]
        # The guest declines in Google Calendar -> Refresh shows Rejected and the meeting needs rescheduling.
        _google_reply(live, event_id, guest, "declined")
        client.post("/api/meetings/sync-responses")
        m = client.get(f"/api/meetings/{mid}").json()
        assert {p["email"]: p["response_status"] for p in m["participants"]}[guest] == "REJECTED"
        assert m["status"] == "RESCHEDULING_REQUIRED"
    finally:
        _cleanup(org, guest)


def test_organizer_only_meeting_confirms_when_the_guest_accepts(live, monkeypatch):
    org, guest = "conf.org@example.test", "conf.guest@example.test"
    _employee(org, "Olga Organizer")
    _employee(guest, "Gopal Guest")
    monkeypatch.setattr(settings, "GOOGLE_CALENDAR_OWNER_EMAIL", org)
    try:
        _connect(org)
        mid = _plan("Schedule an online team sync meeting with Olga Organizer and Gopal Guest on 2030-01-08 from 9 AM to 10 AM to discuss scope.")["meeting_id"]
        client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"})
        _google_reply(live, client.get(f"/api/meetings/{mid}").json()["calendar_event_id"], guest, "accepted")
        client.post("/api/meetings/sync-responses")
        assert client.get(f"/api/meetings/{mid}").json()["status"] == "CONFIRMED"
    finally:
        _cleanup(org, guest)


# ----------------- Scheduling: no past times; statuses match the proposed slot -----------------

def test_time_in_the_past_is_blocked_not_replaced():
    """A plan whose time is (or is edited to) already past is blocked for approval, never silently moved."""
    from datetime import datetime, timedelta
    plan = _plan("Schedule an online team sync meeting with Alice Chen on 2030-01-08 from 9 AM to 10 AM to discuss the budget.")
    past = datetime.utcnow() - timedelta(hours=3)
    edited = client.post(f"/api/meetings/{plan['meeting_id']}/approval", json={"action": "EDIT", "edits": {
        "scheduled_start": past.strftime("%Y-%m-%dT%H:%M:00Z"),
        "scheduled_end": (past + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:00Z")}}).json()
    # The organizer is asked for a new time (nothing is booked or silently moved).
    assert edited["status"] == "NEEDS_INFORMATION"
    m = edited["meeting"]
    assert "start_time" in m["missing_fields"]
    assert any("already passed" in (q.get("note") or "") for q in m["questions"])
    res = client.post(f"/api/meetings/{plan['meeting_id']}/approval", json={"action": "APPROVE"})
    assert res.status_code == 409


def test_alternative_slots_are_never_in_the_past(live):
    from datetime import datetime, timedelta
    a = "past.alt@example.test"
    _employee(a, "Pia Pastalt")
    try:
        _connect(a)
        now = datetime.utcnow()
        later = now + timedelta(hours=2)
        live.busy[a] = [{"start": later.strftime("%Y-%m-%dT%H:00:00Z"), "end": (later + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00Z")}]
        from backend.skills.scheduling_skill import MeetingSchedulingSkill
        out = MeetingSchedulingSkill().run({"participant_emails": [a], "target_date": later.strftime("%Y-%m-%d"),
                                            "target_start_time": later.strftime("%Y-%m-%dT%H:00:00Z"),
                                            "target_end_time": (later + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00Z"),
                                            "duration_minutes": 30, "profiles": []})
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert all(s["start"] > now_iso for s in out["alternative_slots"])
    finally:
        _cleanup(a)


def test_unreachable_google_is_reported_as_a_network_problem(live):
    import httplib2
    org = "net.org@example.test"
    _employee(org, "Nikhil Network")
    try:
        _connect(org)
        live.fail_insert = httplib2.ServerNotFoundError("Unable to find the server at www.googleapis.com")
        mid = _plan("Schedule an online team sync meeting with Nikhil Network on 2030-01-08 from 9 AM to 10 AM to discuss scope.")["meeting_id"]
        res = client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"}).json()
        assert res["success"] is False and res["error_data"]["error_code"] == "NETWORK_UNREACHABLE"
        assert "Could not reach Google" in res["error"] and "Nothing was sent" in res["error"]
        failed = client.get(f"/api/meetings/{mid}").json()
        assert failed["status"] == "ACTION_FAILED" and failed["execution_error"]["failed_at"]   # when it failed
    finally:
        _cleanup(org)


# ----------------- In person: venue coordinator -----------------

COORD = "venue.coord@college.edu"


@pytest.fixture
def coordinator():
    # Venue contacts are external meeting-specific recipients, with no employee or OAuth record.
    yield COORD


def _offline(names, when="on 2030-01-08 from 9 AM to 10 AM", venue="Conference Room B (Innovation Lab)", coordinator_email=COORD):
    plan = _plan(f"Schedule an offline design workshop with {names} {when} in {venue} to review the component layouts.")
    if plan.get("status") == "NEEDS_INFORMATION":
        people = plan.get("understood", {}).get("participants", [])
        emails = {}
        with SessionLocal() as db:
            for person in people:
                name = person.get("name") if isinstance(person, dict) else str(person)
                employee = db.query(Employee).filter(Employee.name.ilike(name)).first()
                if employee:
                    emails[name] = employee.email
        plan = client.post(f"/api/meetings/{plan['meeting_id']}/clarify", json={
            "participant_emails": emails, "venue_coordinator_email": coordinator_email,
        }).json()
    return plan


def test_venue_available_in_draft_and_tab_data(live, coordinator):
    org = "vt.org@example.test"
    _employee(org, "Vani Venue")
    try:
        _connect(org)
        plan = _offline("Vani Venue")
        m = client.get(f"/api/meetings/{plan['meeting_id']}").json()
        assert m["status"] == "WAITING_FOR_HUMAN_APPROVAL"
        venue = m["venue_coordination"]
        assert venue["availability"]["status"] == "AVAILABLE" and venue["request_status"] == "NOT_SENT"
        assert venue["coordinator_name"] == "Venue coordinator" and venue["coordinator_email"] == COORD
        draft = venue["draft"]
        assert draft["to"] == COORD and plan["meeting_id"] in draft["subject"]
        for text in ("Design Workshop", "Conference Room B (Innovation Lab)", "Tuesday, 08 January 2030", "60 minutes",
                     "Review the component layouts", "Number of participants: 1"):
            assert text in draft["body"], text
        # No employee or Google Calendar lookup is required for the external coordinator.
        assert not [c for c in live.of("freebusy") if c[1] == COORD]
        assert COORD not in [p["email"] for p in m["participants"]]
        # Online meetings have no venue coordination at all.
        online = _plan("Schedule an online team sync meeting with Vani Venue on 2030-01-09 from 9 AM to 10 AM to discuss scope.")
        assert "venue_coordination" not in client.get(f"/api/meetings/{online['meeting_id']}").json()
    finally:
        _cleanup(org)


def test_offline_meetings_store_independent_coordinator_recipients(live, coordinator):
    first_org, second_org = "vc.first@example.test", "vc.second@example.test"
    _employee(first_org, "First Coordinator Meeting")
    _employee(second_org, "Second Coordinator Meeting")
    try:
        first = _offline("First Coordinator Meeting", when="on 2030-01-08 from 9 AM to 10 AM",
                         coordinator_email="first.venue@example.org")
        second = _offline("Second Coordinator Meeting", when="on 2030-01-09 from 9 AM to 10 AM",
                          coordinator_email="second.venue@example.org")
        a = client.get(f"/api/meetings/{first['meeting_id']}").json()
        b = client.get(f"/api/meetings/{second['meeting_id']}").json()
        assert a["venue_coordination"]["draft"]["to"] == "first.venue@example.org"
        assert b["venue_coordination"]["draft"]["to"] == "second.venue@example.org"
        assert a["venue_coordinator_email"] != b["venue_coordinator_email"]
    finally:
        _cleanup(first_org, second_org)


def test_busy_venue_blocks_approval(live, coordinator):
    org = "vb.org@example.test"
    _employee(org, "Vikram Busy")
    try:
        _connect(org)
        # An external coordinator's calendar never blocks organizer approval.
        plan = _offline("Vikram Busy")
        assert plan["status"] == "WAITING_FOR_HUMAN_APPROVAL"
        assert client.post(f"/api/meetings/{plan['meeting_id']}/approval", json={"action": "APPROVE"}).json()["success"]
        assert not [c for c in live.of("freebusy") if c[1] == COORD]
    finally:
        _cleanup(org)


def test_external_coordinator_needs_no_calendar_and_same_venue_overlap_is_busy(live, coordinator):
    org = "vu.org@example.test"
    _employee(org, "Usha Unverified")
    try:
        _connect(org)                                  # the external coordinator has no account
        first = _offline("Usha Unverified")
        m = client.get(f"/api/meetings/{first['meeting_id']}").json()
        assert m["venue_coordination"]["availability"]["status"] == "AVAILABLE"
        assert m["status"] == "WAITING_FOR_HUMAN_APPROVAL"
        assert not any("could not be verified" in c["conflict"].lower() for c in m["conflicts"])
        assert client.post(f"/api/meetings/{first['meeting_id']}/approval", json={"action": "APPROVE"}).json()["success"]
        # Another in-person meeting in the same venue at an overlapping time: the venue is taken.
        second = _offline("Usha Unverified", when="on 2030-01-08 from 9:30 AM to 10:30 AM")
        m2 = client.get(f"/api/meetings/{second['meeting_id']}").json()
        assert m2["venue_coordination"]["availability"]["status"] == "BUSY"
        assert "already used by 'Design Workshop'" in m2["venue_coordination"]["availability"]["reason"]
    finally:
        _cleanup(org)


def test_venue_request_is_sent_only_on_click_with_the_edited_email(live, coordinator):
    org = "vs.org@example.test"
    _employee(org, "Vasu Sender")
    try:
        _connect(org)
        mid = _offline("Vasu Sender")["meeting_id"]
        assert live.of("gmail.send") == []                                       # nothing sent by drafting
        assert client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"}).json()["success"]
        assert len(live.of("gmail.send")) == 1                                  # participant invitation only
        assert len(live.of("events.insert")) == 1
        bad = client.post(f"/api/meetings/{mid}/venue-request", json={"to": "not-an-email", "subject": "s", "body": "b"})
        assert bad.status_code == 422
        res = client.post(f"/api/meetings/{mid}/venue-request", json={
            "to": COORD, "subject": "Room please", "body": "Edited body: please book Conference Room B."})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["subject"] == f"Room please [{mid}]"                         # reference kept for reply matching
        sent = live.of("gmail.send")
        assert len(sent) == 2 and all(item[1] == org for item in sent)            # both use organizer account
        assert len(live.of("events.insert")) == 1                                # same offline meeting invitation event
        m = body["meeting"]
        assert m["approval_status"] == "APPROVED"
        assert m["venue_coordination"]["request_status"] == "SENT"               # not confirmed
        again = client.post(f"/api/meetings/{mid}/venue-request", json={"to": COORD, "subject": "x", "body": "y"})
        assert again.status_code == 409 and "waiting for a reply" in again.json()["detail"]
    finally:
        _cleanup(org)


def test_missing_coordinator_email_and_gmail_failure(live):
    org = "vc.org@example.test"
    _employee(org, "Veda Config")
    try:
        _connect(org)
        missing = _plan("Schedule an offline design workshop with Veda Config on 2030-01-08 from 9 AM to 10 AM in Conference Room B to review the component layouts.")
        mid = missing["meeting_id"]
        m = client.get(f"/api/meetings/{mid}").json()
        assert m["venue_coordination"]["configured"] is False
        assert "valid venue coordinator email" in m["venue_coordination"]["configuration_error"]
        invalid = client.post(f"/api/meetings/{mid}/clarify", json={"venue_coordinator_email": "bad-address"})
        assert invalid.status_code == 422 and "valid venue coordinator email" in invalid.json()["detail"]
        res = client.post(f"/api/meetings/{mid}/venue-request", json={"to": COORD, "subject": "s", "body": "b"})
        assert res.status_code == 422 and "valid venue coordinator email" in res.json()["detail"]
        ready = client.post(f"/api/meetings/{mid}/clarify", json={
            "participant_emails": {"Veda Config": org}, "venue_coordinator_email": COORD,
        })
        assert ready.status_code == 200
        assert client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"}).json()["success"]
        live.fail_send = _http_error(403, "insufficientPermissions", "Request had insufficient authentication scopes.")
        res = client.post(f"/api/meetings/{mid}/venue-request", json={"to": COORD, "subject": "s", "body": "b"})
        assert res.status_code == 502 and "NOT sent" in res.json()["detail"]
        assert client.get(f"/api/meetings/{mid}").json()["venue_coordination"]["request_status"] == "NOT_SENT"
    finally:
        _cleanup(org)


def test_venue_coordinator_is_external_and_has_no_employee_requirement(live, coordinator):
    org = "vn.org@example.test"
    _employee(org, "Neel Notcoord")
    try:
        plan = _offline("Neel Notcoord")
        meeting = client.get(f"/api/meetings/{plan['meeting_id']}").json()
        assert meeting["venue_coordination"]["coordinator_email"] == COORD
        assert meeting["venue_coordination"]["configured"] is True
        assert COORD not in [p["email"] for p in meeting["participants"]]
    finally:
        _cleanup(org)


def test_meeting_confirms_only_after_attendees_and_venue_reply(live, coordinator):
    org, guest = "vr.org@example.test", "vr.guest@example.test"
    _employee(org, "Rohan Reply")
    _employee(guest, "Rina Guest")
    try:
        _connect(org)
        mid = _offline("Rohan Reply and Rina Guest")["meeting_id"]
        assert client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"}).json()["success"]
        event_id = client.get(f"/api/meetings/{mid}").json()["calendar_event_id"]
        assert COORD not in [a["email"] for a in live.events[event_id]["attendees"]]   # not invited as attendee
        _google_reply(live, event_id, guest, "accepted")
        client.post("/api/meetings/sync-responses")
        m = client.get(f"/api/meetings/{mid}").json()
        assert m["status"] == "WAITING_FOR_AUDITORIUM_RESPONSE"          # attendees done; venue not requested yet
        client.post(f"/api/meetings/{mid}/venue-request", json={"to": COORD, "subject": "Venue", "body": "Please book."})
        live.reply_snippet = "Confirmed - the room is booked for you."
        client.post("/api/meetings/sync-responses")
        m = client.get(f"/api/meetings/{mid}").json()
        assert m["venue_coordination"]["request_status"] == "CONFIRMED" and m["status"] == "CONFIRMED"
    finally:
        _cleanup(org, guest)


def test_venue_declined_then_edit_supersedes_request_and_updates_same_event(live, coordinator):
    org = "vd.org@example.test"
    _employee(org, "Dev Declined")
    try:
        _connect(org)
        mid = _offline("Dev Declined")["meeting_id"]
        client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"})
        client.post(f"/api/meetings/{mid}/venue-request", json={"to": COORD, "subject": "Venue", "body": "Please book."})
        live.reply_snippet = "Sorry, the venue is not available that day."
        client.post("/api/meetings/sync-responses")
        m = client.get(f"/api/meetings/{mid}").json()
        assert m["venue_coordination"]["request_status"] == "DECLINED" and m["status"] == "RESCHEDULING_REQUIRED"
        # Organizer changes the time: the declined request no longer applies; a new one is needed.
        edited = client.post(f"/api/meetings/{mid}/approval", json={"action": "EDIT", "edits": {
            "scheduled_start": "2030-01-09T09:00:00Z", "scheduled_end": "2030-01-09T10:00:00Z"}}).json()
        assert edited["status"] == "WAITING_FOR_HUMAN_APPROVAL"
        venue = edited["meeting"]["venue_coordination"]
        assert venue["request_status"] == "NOT_SENT" and venue["history"][0]["status"] == "SUPERSEDED"
        assert client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"}).json()["success"]
        assert len(live.of("events.insert")) == 1 and len(live.of("events.patch")) == 1   # updated, not duplicated
    finally:
        _cleanup(org)


def test_delete_offline_meeting_cancels_event_and_notifies_coordinator(live, coordinator):
    org = "vx.org@example.test"
    _employee(org, "Xena Delete")
    try:
        _connect(org)
        mid = _offline("Xena Delete")["meeting_id"]
        client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"})
        client.post(f"/api/meetings/{mid}/venue-request", json={"to": COORD, "subject": "Venue", "body": "Please book."})
        res = client.delete(f"/api/meetings/{mid}").json()
        assert res["cancelled"][0].startswith("calendar_event_cancelled")
        assert res["cancelled"][1] == "venue_request_cancelled:Conference Room B (Innovation Lab)"
        last = live.of("gmail.send")[-1]
        assert last[1] == org
    finally:
        _cleanup(org)

