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

from backend.app.config import settings
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
                        google.calls.append(("events.patch", account, eventId, body))
                        return _Exec(lambda: google.events[eventId])

                    def get(_, calendarId, eventId):
                        google.calls.append(("events.get", account, eventId))
                        return _Exec(lambda: google.events[eventId])
                return Ev()

        class Gmail:
            def users(self):
                class Users:
                    def messages(_):
                        class Msgs:
                            def send(__, userId, body):
                                google.calls.append(("gmail.send", account, body))
                                return _Exec(lambda: {"id": f"gm{uuid.uuid4().hex[:8]}"})

                            def list(__, userId, q, maxResults=20):
                                google.calls.append(("gmail.list", account, q))
                                return _Exec(lambda: {"messages": [{"id": "reply1"}]})

                            def get(__, userId, id, format, metadataHeaders):
                                return _Exec(lambda: {"id": id, "threadId": "t1", "snippet": "Confirmed - the auditorium is booked for you.",
                                                      "payload": {"headers": [{"name": "From", "value": settings.AUDITORIUM_BOOKING_EMAIL},
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
        res = client.get("/api/auth/google/callback", params={"state": state, "code": "auth-code"}, follow_redirects=False)
        assert "calendar=connected" in res.headers["location"], res.headers["location"]

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
            "request": "Schedule an online meeting with Orla Organizer and Gus Guest on Wednesday from 10 AM to 11 AM to discuss the launch plan."
        }).json()
        meeting_id = plan["meeting_id"]
        # Full LangGraph path up to the gate, including RAG.
        assert plan["workflow_trace"][:6] == ["node_intake", "node_parse_request", "node_identify_participants",
                                              "node_retrieve_context", "node_check_availability", "node_prepare_agenda"]
        assert plan["workflow_trace"][-1] == "node_human_approval"
        assert plan["status"] == "WAITING_FOR_HUMAN_APPROVAL"
        # Availability came from the real adapter through MCP; nothing consequential happened before approval.
        assert "calendar.check_availability" in mcp_methods
        assert {c[1] for c in live.of("freebusy")} == {org, guest}
        assert not live.of("events.insert") and not live.of("gmail.send")
        assert plan["meet_url"] == "" and plan["calendar_event_id"] == ""

        approved = client.post(f"/api/meetings/{meeting_id}/approval", json={"action": "APPROVE"}).json()
        assert approved["success"] is True, approved
        assert approved["status"] == "WAITING_FOR_PARTICIPANTS"
        assert "calendar.create_event" in mcp_methods and "gmail.send" in mcp_methods

        insert = live.of("events.insert")[0]
        assert {a["email"] for a in insert[2]["attendees"]} == {org, guest}   # registered emails only
        assert insert[3] == 1 and insert[4] == "all"                           # Meet requested, invites sent
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
            "request": "Schedule an online meeting with Fiona Failure on Tuesday from 9 AM to 10 AM to discuss the budget."
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
            "request": "Schedule an online meeting with Nora Nomeet on Tuesday from 1 PM to 2 PM to discuss hiring."
        }).json()
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
            "request": "Schedule an online meeting with Una Unconnected on Tuesday from 3 PM to 4 PM to discuss onboarding."
        }).json()
        status = plan["participants"][0]["calendar_status"]
        assert status == "UNVERIFIED"
        res = client.post(f"/api/meetings/{plan['meeting_id']}/approval", json={"action": "APPROVE"}).json()
        assert res["success"] is False and "NO_CREDENTIALS" in res["error"]
    finally:
        _cleanup(person)


# ----------------- 7. Approval gate -----------------

def test_approval_gate_is_enforced_in_backend():
    from backend.graph.workflow_graph import resume_meeting_workflow
    plan = client.post("/api/meetings/orchestrate", json={
        "request": "Schedule an online meeting with Alice Chen on Thursday at 11 AM to discuss the gate."
    }).json()
    meeting_id = plan["meeting_id"]
    # Resuming at the gate without a human approval never reaches execution.
    state = resume_meeting_workflow(meeting_id, "APPROVAL")
    assert "node_execute" not in [t["node"] for t in state["trace"]]
    assert client.get(f"/api/meetings/{meeting_id}").json()["calendar_event_id"] == ""
    # The public MCP endpoint cannot bypass the gate for consequential capabilities.
    res = client.post("/api/mcp", json={"method": "gmail.send", "params": {
        "to_emails": ["alice.chen@example.com"], "subject": "x", "body": "y", "meeting_id": meeting_id}}).json()
    assert res["error"]["code"] == -32003
    # Approving something that is not waiting for approval is refused.
    client.post(f"/api/meetings/{meeting_id}/approval", json={"action": "REJECT"})
    assert client.post(f"/api/meetings/{meeting_id}/approval", json={"action": "APPROVE"}).status_code == 409


def test_edit_revalidates_same_meeting_and_keeps_human_changes():
    plan = client.post("/api/meetings/orchestrate", json={
        "request": "Schedule an online meeting with Alice Chen and Bob Smith on Thursday at 11 AM to discuss edits."
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
            "request": "Schedule an online meeting with Carol Davis on Friday from 11 AM to 12 PM to discuss quantum roadmap."
        }).json()
        assert history_id in [m["meeting_id"] for m in plan["rag_context"]["historical_meetings"]]
        assert "Follow-up on related past meeting(s): Meeting: Discuss quantum roadmap" in plan["agenda"]
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
        "request": "Schedule an online meeting with David on Thursday at 11 AM to discuss research."
    }).json()
    meeting_id = plan["meeting_id"]
    assert plan["status"] == "RESCHEDULING_REQUIRED" and plan["participants"] == []
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
        "request": "Schedule an online meeting with Alice Chen on Thursday from 3 PM to 4 PM to discuss alternatives."
    }).json()
    assert "node_find_alternative_slot" in plan["workflow_trace"]
    assert plan["scheduled_start"] != plan["requested_slot"]["start"]
    assert any(c["status"] == "RESOLVED" for c in plan["conflicts"])


# ----------------- 11. Auditorium workflow truthfulness -----------------

def test_auditorium_statuses_and_simulated_label():
    plan = client.post("/api/meetings/orchestrate", json={
        "request": "Schedule an offline meeting with Bob Smith in Auditorium Alpha on Thursday at 11 AM. Purpose is to present the roadmap."
    }).json()
    meeting_id = plan["meeting_id"]
    assert plan["room_bookings"] == []                     # nothing requested before approval
    client.post(f"/api/meetings/{meeting_id}/approval", json={"action": "APPROVE"})
    meeting = client.get(f"/api/meetings/{meeting_id}").json()
    assert meeting["status"] == "WAITING_FOR_AUDITORIUM_RESPONSE"
    assert meeting["room_bookings"][0]["status"] == "PENDING" and "Request sent" in meeting["room_bookings"][0]["notes"]
    assert meeting["calendar_event_id"] == ""              # not booked yet, no invitation yet

    res = client.post(f"/api/meetings/{meeting_id}/simulate-room", json={"room_name": "Auditorium Alpha", "confirmed": True}).json()
    assert res["status"] == "CONFIRMED" and res["source"] == "SIMULATED"
    booking = client.get(f"/api/meetings/{meeting_id}").json()["room_bookings"][0]
    assert booking["status"] == "CONFIRMED" and "[SIMULATED]" in booking["notes"]


def test_room_request_failure_is_reported(live):
    # No connected Google account -> the email request cannot be sent -> FAILED, never "booked".
    plan = client.post("/api/meetings/orchestrate", json={
        "request": "Schedule an offline meeting with Bob Smith in Auditorium Alpha on Thursday at 2 PM. Purpose is to host training."
    }).json()
    res = client.post(f"/api/meetings/{plan['meeting_id']}/approval", json={"action": "APPROVE"}).json()
    assert res["success"] is False and "MCP room.request" in res["error"]
    meeting = client.get(f"/api/meetings/{plan['meeting_id']}").json()
    assert meeting["status"] == "ACTION_FAILED" and meeting["room_bookings"][0]["status"] == "FAILED"


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
            "request": "Schedule an online meeting with Rita Reapprove and Gil Guest on Tuesday from 9 AM to 10 AM to discuss pricing."
        }).json()
        mid = plan["meeting_id"]
        assert client.post(f"/api/meetings/{mid}/approval", json={"action": "APPROVE"}).json()["success"]
        first = client.get(f"/api/meetings/{mid}").json()
        # Simulated rejection -> same meeting needs rescheduling (labelled as simulated)
        res = client.post(f"/api/meetings/{mid}/simulate-participant", json={"email": guest, "response": "REJECTED"}).json()
        assert res["status"] == "RESCHEDULING_REQUIRED" and res["source"] == "SIMULATED"
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
