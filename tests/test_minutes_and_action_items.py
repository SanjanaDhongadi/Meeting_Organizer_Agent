"""
Minutes of Meeting, action-item tracker and carry-forward tasks.

Google and the LLM are faked ONLY at their boundaries (the Meet service and MoMAgent._complete); the application's
own gateway, worker job, LangGraph workflow, API and database code run unchanged.
"""
import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.agents.mom_agent import MoMAgent
from backend.app.action_items import compose_agenda, strip_followups
from app.config import settings
from backend.app.db import ActionItem, Meeting, MeetingMinutes, MeetingParticipant, SessionLocal
from backend.app.main import app
from backend.connectors import mcp_gateway as gateway_module
from backend.connectors.meet_service import GoogleMeetService, meeting_code_from_url
from worker.worker import background_worker

client = TestClient(app)
job = background_worker.minutes_job

TRANSCRIPT = (
    "Alice Chen: Let's start with the budget review for the Phoenix project.\n"
    "Bob Smith: The vendor quotes came in higher than planned.\n"
    "Alice Chen: We decided to go with the second vendor.\n"
    "Alice Chen: Bob, please send the revised budget by 2026-12-04.\n"
    "Bob Smith: Sure, I will send the revised budget.\n"
    "Alice Chen: Someone should also update the risk log.\n"
)


# ----------------- helpers -----------------

def _user(name):
    email = f"{name.lower()}_{uuid.uuid4().hex[:6]}@example.com"
    res = client.post("/api/auth/register", json={"email": email, "password": "secret123", "name": name})
    assert res.status_code == 200, res.text
    return {"email": email, "headers": {"Authorization": f"Bearer {res.json()['token']}"}, "id": res.json()["user"]["id"]}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _meeting(organizer, participants, title="Phoenix budget review", ended_hours_ago=2.0, mode="ONLINE", purpose="Budget"):
    mid = f"meet-{uuid.uuid4().hex[:8]}"
    end = datetime.utcnow() - timedelta(hours=ended_hours_ago)
    db = SessionLocal()
    try:
        db.add(Meeting(
            id=mid, organizer_id=organizer["id"], title=title, raw_request=title, status="CONFIRMED", mode=mode,
            scheduled_start=_iso(end - timedelta(hours=1)), scheduled_end=_iso(end), duration_minutes=60, purpose=purpose,
            meet_url="https://meet.google.com/abc-defg-hij" if mode == "ONLINE" else "",
            calendar_event_id="evt-1" if mode == "ONLINE" else "", approval_status="APPROVED", parsed_details="{}"))
        for p in participants:
            db.add(MeetingParticipant(meeting_id=mid, name=p["name"], email=p["email"], response_status="ACCEPTED"))
        db.commit()
    finally:
        db.close()
    return mid


def _row(mid):
    db = SessionLocal()
    try:
        return db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == mid).first()
    finally:
        db.close()


def _set(mid, **fields):
    db = SessionLocal()
    try:
        db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == mid).update(fields)
        db.commit()
    finally:
        db.close()


class FakeMeet:
    def __init__(self, *results):
        self.results, self.calls = list(results), 0

    def get_transcript(self, meeting_code, organizer_email=None, not_before=None):
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]


READY = {"success": True, "ready": True, "reason": "READY", "transcript_text": TRANSCRIPT,
         "meta": {"actual_start": "2026-11-13T09:00:00Z", "actual_end": "2026-11-13T10:00:00Z", "speakers": ["Alice Chen", "Bob Smith"]}}
PROCESSING = {"success": True, "ready": False, "reason": "TRANSCRIPT_PROCESSING", "message": "Google is still generating the transcript."}


@pytest.fixture
def fake_meet(monkeypatch):
    def install(*results):
        fake = FakeMeet(*results)
        monkeypatch.setattr(gateway_module, "get_meet_service", lambda: fake)
        return fake
    return install


def _llm(monkeypatch, payload):
    monkeypatch.setattr(MoMAgent, "_complete", lambda self, system, user: payload)


LLM_OUTPUT = {
    "summary": "The team reviewed the Phoenix budget and chose a vendor.",
    "topic": "Phoenix project",
    "discussion_points": [{"topic": "Vendor quotes", "details": "Quotes were higher than planned."}],
    "decisions": [
        {"decision": "Go with the second vendor.", "evidence": "We decided to go with the second vendor."},
        {"decision": "Hire two contractors.", "evidence": "We will hire two contractors immediately."},   # not in transcript
    ],
    "action_items": [
        {"description": "Send the revised budget", "owner": "Bob", "due_date": "2026-12-04",
         "due_date_text": "2026-12-04", "topic": "Phoenix project", "evidence": "Bob, please send the revised budget by 2026-12-04."},
        {"description": "Update the risk log", "owner": "Carol", "due_date": "2026-12-10",
         "due_date_text": "next Tuesday", "topic": None, "evidence": "Someone should also update the risk log."},
        {"description": "Book the conference room", "owner": "Alice", "due_date": None, "due_date_text": None,
         "topic": None, "evidence": "Please book the conference room for everyone."},                    # not in transcript
    ],
}


@pytest.fixture
def setup():
    alice, bob = _user("Alice"), _user("Bob")
    mid = _meeting(alice, [{"name": "Alice Chen", "email": alice["email"]}, {"name": "Bob Smith", "email": bob["email"]}])
    return alice, bob, mid


# ----------------- retrieval -----------------

def test_meet_code_and_public_mcp_cannot_read_transcripts():
    assert meeting_code_from_url("https://meet.google.com/abc-defg-hij?authuser=0") == "abc-defg-hij"
    call = {"method": "meet.get_transcript", "params": {"meeting_code": "abc-defg-hij"}}
    assert client.post("/api/mcp", json=call).status_code == 401
    res = client.post("/api/mcp", json=call, headers=_user("Mcp").get("headers"))
    assert res.json()["error"]["code"] == -32003


def test_scan_retries_with_backoff_and_never_duplicates(setup, fake_meet):
    alice, bob, mid = setup
    meet = fake_meet(PROCESSING, READY)
    job.scan()
    row = _row(mid)
    assert row.status == "WAITING_FOR_TRANSCRIPT" and row.retrieval_attempts == 1
    assert row.next_attempt_at > datetime.utcnow() and "still generating" in row.last_error
    job.scan()                                       # not due yet: no second Google call
    assert meet.calls == 1
    _set(mid, next_attempt_at=datetime.utcnow() - timedelta(seconds=1))
    assert job.retrieve(mid)["status"] == "TRANSCRIPT_RETRIEVED"
    assert _row(mid).transcript_text == TRANSCRIPT
    assert job.retrieve(mid, force=True).get("skipped") is True      # duplicate notification: ignored
    assert meet.calls == 2


def test_gives_up_after_the_wait_window_and_organizer_can_retry(setup, fake_meet):
    alice, bob, mid = setup
    other = _meeting(alice, [{"name": "Bob Smith", "email": bob["email"]}], ended_hours_ago=settings.MINUTES_MAX_WAIT_HOURS + 3)
    fake_meet({"success": True, "ready": False, "reason": "NO_TRANSCRIPT", "message": "No transcript."})
    db = SessionLocal()
    try:
        job.ensure_row(db, db.query(Meeting).filter(Meeting.id == other).first())
    finally:
        db.close()
    assert job.retrieve(other)["status"] == "TRANSCRIPT_UNAVAILABLE"
    fake_meet(READY)
    res = client.post(f"/api/meetings/{other}/minutes/check-transcript", headers=alice["headers"])
    assert res.status_code == 200 and res.json()["status"] in {"TRANSCRIPT_RETRIEVED", "GENERATING_MOM", "MOM_FAILED"}


def test_offline_and_unended_meetings_are_not_minuted(setup, fake_meet):
    alice, bob, _ = setup
    offline = _meeting(alice, [], mode="OFFLINE", title="In person")
    future = _meeting(alice, [], ended_hours_ago=-5, title="Later")
    meet = fake_meet(READY)
    job.scan()
    assert _row(offline) is None and _row(future) is None
    assert meet.calls == 1                       # only the fixture's ended online meeting was asked about
    assert client.get(f"/api/meetings/{offline}/minutes", headers=alice["headers"]).json()["status"] == "NOT_APPLICABLE"
    assert client.get(f"/api/meetings/{future}/minutes", headers=alice["headers"]).json()["status"] == "WAITING_FOR_MEETING_END"


def test_google_meet_service_assembles_transcript_and_reports_waiting_states():
    svc = MagicMock()
    records = svc.conferenceRecords.return_value
    records.list.return_value.execute.return_value = {"conferenceRecords": [
        {"name": "conferenceRecords/r1", "startTime": "2026-11-13T09:00:00Z", "endTime": "2026-11-13T10:00:00Z"}]}
    transcripts = records.transcripts.return_value
    transcripts.list.return_value.execute.return_value = {"transcripts": [{"name": "conferenceRecords/r1/transcripts/t1", "state": "FILE_GENERATED"}]}
    records.participants.return_value.list.return_value.execute.return_value = {"participants": [
        {"name": "conferenceRecords/r1/participants/p1", "signedinUser": {"displayName": "Alice Chen"}}]}
    transcripts.entries.return_value.list.return_value.execute.return_value = {"transcriptEntries": [
        {"participant": "conferenceRecords/r1/participants/p1", "text": "Hello everyone"}]}
    service = GoogleMeetService(credentials=object())
    service._build = lambda credentials: svc
    ok = service.get_transcript("abc-defg-hij")
    assert ok["ready"] and ok["transcript_text"] == "Alice Chen: Hello everyone"

    transcripts.list.return_value.execute.return_value = {"transcripts": [{"name": "x", "state": "ENDED"}]}
    assert service.get_transcript("abc-defg-hij")["reason"] == "TRANSCRIPT_PROCESSING"
    transcripts.list.return_value.execute.return_value = {"transcripts": []}
    assert service.get_transcript("abc-defg-hij")["reason"] == "NO_TRANSCRIPT"
    records.list.return_value.execute.return_value = {"conferenceRecords": [{"name": "r", "startTime": "2026-11-13T09:00:00Z"}]}
    assert service.get_transcript("abc-defg-hij")["reason"] == "CONFERENCE_IN_PROGRESS"
    records.list.return_value.execute.return_value = {"conferenceRecords": []}
    assert service.get_transcript("abc-defg-hij")["reason"] == "NO_CONFERENCE_RECORD"
    assert service.get_transcript("not a code")["error_code"] == "INVALID_MEETING_CODE"


# ----------------- generation, review, approval -----------------

def test_generation_is_grounded_in_the_transcript(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    fake_meet(READY)
    _llm(monkeypatch, LLM_OUTPUT)
    job.scan()
    row = _row(mid)
    assert row.status == "AWAITING_ORGANIZER_APPROVAL"
    draft = json.loads(row.mom_draft)
    assert [d["text"] for d in draft["decisions"]] == ["Go with the second vendor."]      # invented decision dropped
    assert any("left out" in w for w in draft["warnings"])
    items = {i["description"]: i for i in client.get(f"/api/meetings/{mid}/minutes", headers=alice["headers"]).json()["action_items"]}
    assert set(items) == {"Send the revised budget", "Update the risk log"}                # invented item dropped
    budget = items["Send the revised budget"]
    assert budget["owner_email"] == bob["email"] and budget["due_date"] == "2026-12-04" and budget["finalized"] is False
    risk = items["Update the risk log"]
    assert risk["owner_email"] is None and risk["due_date"] is None                        # unclear -> not specified


def test_missing_llm_configuration_is_reported_not_faked(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    fake_meet(READY)
    monkeypatch.setattr(settings, "GROQ_API_KEY", None)
    job.scan()
    row = _row(mid)
    assert row.status == "MOM_FAILED" and "GROQ_API_KEY" in row.last_error
    db = SessionLocal()
    try:
        assert db.query(ActionItem).count() == 0
    finally:
        db.close()


def test_review_approve_distribute_and_task_authorization(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    carol = _user("Carol")
    fake_meet(READY)
    _llm(monkeypatch, LLM_OUTPUT)
    job.scan()

    # Only the organizer reaches the minutes; participants and strangers do not.
    assert client.get(f"/api/meetings/{mid}/minutes", headers=bob["headers"]).status_code == 403
    assert client.get(f"/api/meetings/{mid}/minutes").status_code in (401, 403)
    # Draft tasks are invisible to everybody's tracker until approval.
    assert client.get("/api/action-items?scope=mine", headers=bob["headers"]).json() == []
    assert client.get("/api/action-items?scope=organized", headers=alice["headers"]).json() == []

    current = client.get(f"/api/meetings/{mid}/minutes", headers=alice["headers"]).json()
    edited = [{"id": i["id"], "description": i["description"], "owner_email": alice["email"] if i["owner_email"] is None else i["owner_email"],
               "due_date": "2026-12-12" if i["owner_email"] is None else i["due_date"], "project_topic": "Phoenix"}
              for i in current["action_items"]]
    res = client.put(f"/api/meetings/{mid}/minutes", headers=alice["headers"],
                     json={"summary": "Edited summary.", "action_items": edited})
    assert res.status_code == 200, res.text
    assert client.put(f"/api/meetings/{mid}/minutes", headers=alice["headers"],
                      json={"action_items": [{"description": "x", "owner_email": carol["email"]}]}).status_code == 422
    assert client.put(f"/api/meetings/{mid}/minutes", headers=alice["headers"],
                      json={"action_items": [{"description": "x", "due_date": "12/12/2026"}]}).status_code == 422
    assert client.post(f"/api/meetings/{mid}/minutes/approve", headers=bob["headers"]).status_code == 403

    approved = client.post(f"/api/meetings/{mid}/minutes/approve", headers=alice["headers"])
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED_AND_DISTRIBUTED"
    assert set(approved.json()["recipients"]) == {alice["email"], bob["email"]}
    assert client.post(f"/api/meetings/{mid}/minutes/approve", headers=alice["headers"]).status_code == 409
    assert client.put(f"/api/meetings/{mid}/minutes", headers=alice["headers"], json={"summary": "late"}).status_code == 409

    mine = client.get("/api/action-items?scope=mine", headers=bob["headers"]).json()
    assert [t["description"] for t in mine] == ["Send the revised budget"]
    assert client.get("/api/action-items?scope=mine", headers=carol["headers"]).json() == []
    assert len(client.get("/api/action-items?scope=organized", headers=alice["headers"]).json()) == 2
    assert client.get("/api/action-items?scope=organized", headers=bob["headers"]).json() == []

    task = mine[0]
    ok = client.patch(f"/api/action-items/{task['id']}", headers=bob["headers"], json={"status": "IN_PROGRESS"})
    assert ok.status_code == 200 and ok.json()["status"] == "IN_PROGRESS"
    done = client.patch(f"/api/action-items/{task['id']}", headers=bob["headers"], json={"status": "COMPLETED"}).json()
    assert done["status"] == "COMPLETED" and done["completed_at"]
    assert client.patch(f"/api/action-items/{task['id']}", headers=bob["headers"], json={"status": "DONE"}).status_code == 422
    assert client.patch(f"/api/action-items/{task['id']}", headers=bob["headers"], json={"description": "x"}).status_code == 403
    assert client.patch(f"/api/action-items/{task['id']}", headers=carol["headers"], json={"status": "COMPLETED"}).status_code == 404
    assert client.patch(f"/api/action-items/{task['id']}", json={"status": "COMPLETED"}).status_code in (401, 403)
    other = [t for t in client.get("/api/action-items?scope=organized", headers=alice["headers"]).json() if t["id"] != task["id"]][0]
    assert client.patch(f"/api/action-items/{other['id']}", headers=bob["headers"], json={"status": "COMPLETED"}).status_code == 404
    assert client.patch(f"/api/action-items/{other['id']}", headers=alice["headers"],
                        json={"owner_email": bob["email"], "due_date": "2026-12-20"}).json()["owner_email"] == bob["email"]


def test_failed_send_keeps_approval_and_can_be_retried(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    fake_meet(READY)
    _llm(monkeypatch, LLM_OUTPUT)
    job.scan()
    from backend.connectors import email_service
    monkeypatch.setattr(email_service._mock_email_instance, "send_invitation",
                        lambda **kw: {"success": False, "error": "smtp down"})
    res = client.post(f"/api/meetings/{mid}/minutes/approve", headers=alice["headers"])
    assert res.status_code == 502 and "smtp down" in res.json()["detail"]
    assert _row(mid).status == "APPROVED_SEND_FAILED"
    monkeypatch.undo()
    _llm(monkeypatch, LLM_OUTPUT)
    assert client.post(f"/api/meetings/{mid}/minutes/approve", headers=alice["headers"]).status_code == 200
    assert _row(mid).status == "APPROVED_AND_DISTRIBUTED"


def test_generation_can_only_be_claimed_once(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    fake_meet(READY)
    job.retrieve(mid)
    assert _row(mid).status == "TRANSCRIPT_RETRIEVED"
    assert job.claim_generation(mid) is True
    assert job.claim_generation(mid) is False


# ----------------- carry-forward tasks -----------------

def _task(mid, organizer, owner, description, status="NOT_STARTED", finalized=True, topic=""):
    db = SessionLocal()
    try:
        item = ActionItem(meeting_id=mid, organizer_id=organizer["id"], description=description,
                          owner_email=owner["email"], owner_name="Owner", status=status, finalized=finalized, project_topic=topic)
        db.add(item)
        db.commit()
        return item.id
    finally:
        db.close()


def test_followup_suggestions_selection_and_agenda():
    alice, bob, carol = _user("Alice"), _user("Bob"), _user("Carol")
    old = _meeting(alice, [{"name": "Bob", "email": bob["email"]}], title="Phoenix kickoff", purpose="Phoenix vendors")
    foreign = _meeting(carol, [{"name": "Bob", "email": bob["email"]}], title="Phoenix kickoff elsewhere")
    relevant = _task(old, alice, bob, "Send the Phoenix vendor budget", topic="Phoenix")
    facilities = _meeting(alice, [{"name": "Bob", "email": bob["email"]}], title="Facilities planning", purpose="Office layout")
    unrelated = _task(facilities, alice, alice, "Order office chairs")
    completed = _task(old, alice, bob, "Phoenix vendor contract", status="COMPLETED")
    draft = _task(old, alice, bob, "Phoenix draft task", finalized=False)
    others_task = _task(foreign, carol, bob, "Phoenix vendor budget")

    new = _meeting(alice, [{"name": "Bob", "email": bob["email"]}], title="Phoenix vendor review", ended_hours_ago=-24,
                   purpose="Choose a vendor")
    db = SessionLocal()
    try:
        db.query(Meeting).filter(Meeting.id == new).update({"status": "WAITING_FOR_HUMAN_APPROVAL", "agenda": "1. Compare quotes"})
        db.commit()
    finally:
        db.close()

    res = client.get(f"/api/meetings/{new}/followup-suggestions", headers=alice["headers"]).json()
    assert [t["id"] for t in res["relevant"]] == [relevant]
    assert res["others"] == [] and res["other_count"] == 1                    # others hidden until asked for
    assert any("Same topic" in r for r in res["relevant"][0]["reasons"])
    everything = client.get(f"/api/meetings/{new}/followup-suggestions?show_all=true", headers=alice["headers"]).json()
    assert [t["id"] for t in everything["others"]] == [unrelated]
    shown = {t["id"] for t in everything["relevant"] + everything["others"]}
    assert not shown & {completed, draft, others_task}                         # database filters, not relevance
    assert client.get(f"/api/meetings/{new}/followup-suggestions", headers=bob["headers"]).status_code == 403

    assert client.put(f"/api/meetings/{new}/followups", headers=alice["headers"],
                      json={"action_item_ids": [others_task]}).status_code == 422
    assert client.put(f"/api/meetings/{new}/followups", headers=alice["headers"],
                      json={"action_item_ids": [completed]}).status_code == 422
    chosen = client.put(f"/api/meetings/{new}/followups", headers=alice["headers"], json={"action_item_ids": [relevant]})
    assert chosen.status_code == 200, chosen.text
    agenda = chosen.json()["agenda"]
    assert agenda.startswith("1. Compare quotes") and "Send the Phoenix vendor budget" in agenda
    assert "Order office chairs" not in agenda                                 # skipped task is not added
    again = client.put(f"/api/meetings/{new}/followups", headers=alice["headers"], json={"action_item_ids": [relevant]}).json()["agenda"]
    assert again == agenda                                                     # idempotent, a single section

    db = SessionLocal()
    try:
        statuses = {i.id: i.status for i in db.query(ActionItem).all()}
        assert statuses[unrelated] == "NOT_STARTED" and statuses[relevant] == "NOT_STARTED"   # nothing closed or deleted
        assert db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == new).count() == 1  # no participants added
        meeting = db.query(Meeting).filter(Meeting.id == new).first()
        assert compose_agenda(db, meeting, "1. New agenda point") .startswith("1. New agenda point\n\nFollow-up")
        assert strip_followups(agenda) == "1. Compare quotes"
    finally:
        db.close()
    cleared = client.put(f"/api/meetings/{new}/followups", headers=alice["headers"], json={"action_item_ids": []}).json()["agenda"]
    assert cleared == "1. Compare quotes"


# ----------------- Meet event notifications -----------------

def test_meet_event_webhook_is_authenticated_and_idempotent(setup, monkeypatch):
    alice, bob, mid = setup
    monkeypatch.setattr(settings, "MEET_EVENTS_TOKEN", None)
    assert client.post("/api/meet/events?token=x", json={}).status_code == 503
    monkeypatch.setattr(settings, "MEET_EVENTS_TOKEN", "s3cret")
    assert client.post("/api/meet/events?token=wrong", json={}).status_code == 403
    assert client.post("/api/meet/events?token=s3cret", content=b"not json").status_code == 400
    body = {"message": {"messageId": f"m-{uuid.uuid4().hex}", "data": "e30="}}
    first = client.post("/api/meet/events?token=s3cret", json=body)
    assert first.status_code == 200 and first.json()["status"] == "accepted"
    assert client.post("/api/meet/events?token=s3cret", json=body).json()["status"] == "duplicate"
