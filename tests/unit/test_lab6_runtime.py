"""
Lab 6 - runtime governance (budgets + one audit event per run), per-node audit, execution checkpoints and crash
recovery of stuck EXECUTING meetings. DEMO_MODE adapters; nothing touches the network.
"""
import json
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from backend.app.db import Meeting, MeetingParticipant, SessionLocal
from backend.connectors import email_service, groq_client
from backend.graph.workflow_graph import clear_execution_steps, coordinator, start_meeting_workflow
from backend.memory.audit_memory import audit_memory
from backend.runtime.governance import BudgetExceeded, count_llm_call, count_step, governed_run
from backend.tools.registry import tool_registry
from worker.jobs.meeting_job import MeetingJob

job = MeetingJob()


def _details(entry):
    d = entry["details"]
    return json.loads(d) if isinstance(d, str) else d


def _events(mid, agent=None, action=None):
    return [e for e in audit_memory.get_meeting_traces(mid)
            if (agent is None or e["agent"] == agent) and (action is None or e["action"] == action)]


def _plan(email=None):
    email = email or f"lab6.{uuid.uuid4().hex[:8]}@example.test"
    state = start_meeting_workflow(f"Schedule an online roadmap sync with {email} on 2030-01-10 at 11 AM for 30 minutes "
                                   "to discuss the product roadmap.")
    if state.get("status") == "NEEDS_INFORMATION":
        pytest.skip("the request parser asked for more details in this environment")
    return state["meeting_id"], email


def _set(mid, **fields):
    db = SessionLocal()
    try:
        db.query(Meeting).filter(Meeting.id == mid).update(fields, synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _meeting(mid):
    db = SessionLocal()
    try:
        m = db.query(Meeting).filter(Meeting.id == mid).first()
        db.expunge(m)
        return m
    finally:
        db.close()


def _steps(mid):
    return (json.loads(_meeting(mid).parsed_details).get("workflow") or {}).get("execution_steps") or {}


def _put_steps(mid, steps):
    parsed = json.loads(_meeting(mid).parsed_details)
    parsed.setdefault("workflow", {})["execution_steps"] = steps
    _set(mid, parsed_details=json.dumps(parsed))


def _stuck(minutes_old=30, approval="APPROVED", steps=None):
    """A meeting whose execution crashed: EXECUTING, last touched `minutes_old` minutes ago."""
    mid, email = _plan()
    _set(mid, approval_status=approval, status="EXECUTING")
    if steps:
        _put_steps(mid, steps)
    _set(mid, updated_at=datetime.utcnow() - timedelta(minutes=minutes_old))
    return mid, email


def _sent_to(email):
    return [m for m in email_service._mock_email_instance.outbox if email in m["to"]]


@pytest.fixture
def spy_calendar(monkeypatch):
    calls = []
    tool = tool_registry.get("create_calendar_event")
    original = tool.execute
    monkeypatch.setattr(tool, "execute", lambda **kw: calls.append(kw) or original(**kw))
    return calls


# ----------------- governance -----------------

def test_successful_run_is_audited_once_with_budget_usage():
    mid, _ = _plan()
    runs = _events(mid, agent="Agent Runtime")
    assert [r["action"] for r in runs] == ["RUN_COMPLETED"]
    d = _details(runs[0])
    assert d["kind"] == "scheduling" and d["steps"] > 0 and d["llm_calls"] == 0 and d["exceeded"] is None
    assert d["budgets"] == {"max_steps": settings.RUNTIME_MAX_STEPS, "max_llm_calls": settings.RUNTIME_MAX_LLM_CALLS,
                            "max_seconds": settings.RUNTIME_MAX_SECONDS}
    assert d["elapsed_seconds"] >= 0


def test_counters_are_noops_outside_a_governed_run():
    count_step(); count_llm_call()          # no run -> nothing raised, nothing recorded


def test_failed_run_is_audited_and_the_original_error_survives(monkeypatch):
    monkeypatch.setattr(audit_memory, "record_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit down")))
    with pytest.raises(ValueError, match="original"):
        with governed_run("scheduling", "lab6-audit-down"):
            raise ValueError("original")     # an audit failure must not replace this


def test_llm_budget_exceeded_falls_back_to_the_rule_parser(monkeypatch):
    class NoRequests:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise AssertionError("a request was sent although the budget was used up")
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    monkeypatch.setattr(settings, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(settings, "RUNTIME_MAX_LLM_CALLS", 0)
    monkeypatch.setattr(groq_client, "get_client", lambda timeout=120.0: NoRequests)
    mid = f"lab6-llm-{uuid.uuid4().hex[:6]}"
    with governed_run("scheduling", mid) as run:
        parsed = coordinator.parse_natural_language_request(
            "Schedule an online sync with ann@example.test on 2030-01-10 at 11 AM for 30 minutes to discuss the roadmap.")
    assert any("ann@example.test" in str(p) for p in parsed["participants"])        # rule-based result
    assert run.exceeded == "llm_calls" and run.llm_calls == 0
    assert isinstance(BudgetExceeded("llm_calls", "x"), groq_client.LLMError)
    done = _details(_events(mid, agent="Agent Runtime")[0])
    assert done["exceeded"] == "llm_calls"


def test_llm_calls_are_counted_per_request(monkeypatch):
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"title": "T"}'))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: reply)))
    monkeypatch.setattr(settings, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(groq_client, "get_client", lambda timeout=120.0: client)
    with governed_run("minutes", f"lab6-count-{uuid.uuid4().hex[:6]}") as run:
        groq_client.chat_json("s", "u")
        groq_client.chat_json("s", "u")
    assert run.llm_calls == 2
    assert groq_client.chat_json("s", "u") == {"title": "T"}      # outside a run: unlimited, uncounted


def test_time_budget_exceeded_fails_the_run_and_the_meeting(monkeypatch):
    mid, _ = _plan()
    _set(mid, approval_status="APPROVED", status="APPROVED")
    monkeypatch.setattr(settings, "RUNTIME_MAX_SECONDS", 0)
    result = job.execute(mid)
    assert result["success"] is False
    assert _meeting(mid).status == "ACTION_FAILED"
    failed = [e for e in _events(mid, agent="Agent Runtime") if e["action"] == "RUN_FAILED"]
    assert len(failed) == 1 and _details(failed[0])["exceeded"] == "seconds"
    assert any(e["action"] == "NODE_FAILED" for e in _events(mid, agent="LangGraph"))


# ----------------- node audit -----------------

def test_node_audit_records_metadata_only():
    mid, email = _plan()
    nodes = _events(mid, agent="LangGraph", action="NODE_COMPLETED")
    names = [_details(n)["node"] for n in nodes]
    assert names[:3] == ["node_intake", "node_parse_request", "node_identify_participants"]
    assert "node_create_draft_plan" in names and "node_human_approval" in names
    for n in nodes:
        d = _details(n)
        assert set(d) == {"node", "status_before", "status_after", "changed_keys", "duration_ms"}
        assert d["changed_keys"] == sorted(d["changed_keys"]) and "trace" not in d["changed_keys"]
        assert isinstance(d["duration_ms"], int) and d["duration_ms"] >= 0
        assert email not in json.dumps(d) and "roadmap" not in json.dumps(d)       # no request/participant values
    parse = next(_details(n) for n in nodes if _details(n)["node"] == "node_parse_request")
    assert parse["status_after"] == "PARSED" and "parsed_details" in parse["changed_keys"]


# ----------------- checkpoints and crash recovery -----------------

def test_recovery_resumes_from_the_calendar_checkpoint(spy_calendar):
    done = {"calendar": {"done": True, "event_id": "EVT-LAB6", "at": "2030-01-01T00:00:00Z"}}
    mid, email = _stuck(steps=done)
    _set(mid, calendar_event_id="EVT-LAB6", updated_at=datetime.utcnow() - timedelta(minutes=30))
    db = SessionLocal()
    try:   # an answer that arrived before the crash must survive (responses are reset only when the event is created)
        db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == mid).update({"response_status": "ACCEPTED"})
        db.commit()
    finally:
        db.close()

    draft_calls = len(spy_calendar)                                 # the draft stage already created its event
    assert mid in job.recover_stuck()
    assert len(spy_calendar) == draft_calls                         # the event was not created again
    assert len(_sent_to(email)) == 1                                # the invitation was sent once
    m = _meeting(mid)
    assert m.status != "EXECUTING" and m.calendar_event_id == "EVT-LAB6"
    steps = _steps(mid)
    assert steps["calendar"]["event_id"] == "EVT-LAB6" and steps["invitation"]["done"] and steps["invitation"]["message_id"]
    db = SessionLocal()
    try:
        assert {p.response_status for p in db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == mid)} == {"ACCEPTED"}
    finally:
        db.close()
    recovered_event = _events(mid, agent="Worker", action="RUN_RECOVERED")
    assert len(recovered_event) == 1 and _details(recovered_event[0])["completed_steps"] == ["calendar"]

    assert mid not in job.recover_stuck()                           # second recovery: nothing left to do
    assert len(_sent_to(email)) == 1


def test_recovery_skips_both_steps_when_everything_was_done():
    done = {"calendar": {"done": True, "event_id": "EVT-2", "at": "t"}, "invitation": {"done": True, "message_id": "m-1", "at": "t"}}
    mid, email = _stuck(steps=done)
    assert mid in job.recover_stuck()
    assert _sent_to(email) == []
    assert _meeting(mid).status == "WAITING_FOR_PARTICIPANTS"


def test_recent_and_unapproved_meetings_are_ignored(spy_calendar):
    recent, recent_email = _stuck(minutes_old=1)
    unapproved, unapproved_email = _stuck(minutes_old=60, approval="PENDING")
    recovered = job.recover_stuck()
    assert recent not in recovered and unapproved not in recovered
    assert _meeting(recent).status == "EXECUTING" and _meeting(unapproved).status == "EXECUTING"
    assert _sent_to(recent_email) == [] and _sent_to(unapproved_email) == []


def test_only_one_worker_wins_the_recovery_lease(monkeypatch):
    mid, email = _stuck()
    import backend.graph.workflow_graph as graph
    calls = []
    monkeypatch.setattr(graph, "resume_meeting_workflow", lambda *a, **k: calls.append(a))
    first = MeetingJob().recover_stuck()
    assert mid in first and calls == [(mid, "APPROVAL")]
    assert mid not in MeetingJob().recover_stuck()                  # the lease refreshed updated_at: no longer stale


def test_fresh_approval_clears_old_markers(spy_calendar):
    mid, email = _plan()
    _set(mid, approval_status="APPROVED", status="APPROVED")
    _put_steps(mid, {"calendar": {"done": True, "event_id": "OLD-EVT", "at": "t"},
                     "invitation": {"done": True, "message_id": "old-msg", "at": "t"}})
    draft_calls = len(spy_calendar)                                 # the draft stage already created its event
    assert job.execute(mid)["success"] is True
    assert len(spy_calendar) == draft_calls + 1 and len(_sent_to(email)) == 1   # nothing skipped: this is a new approval
    steps = _steps(mid)
    assert steps["calendar"]["event_id"] != "OLD-EVT" and steps["invitation"]["message_id"] != "old-msg"


def test_clear_execution_steps_only_touches_the_markers():
    mid, _ = _plan()
    _put_steps(mid, {"calendar": {"done": True, "event_id": "E", "at": "t"}})
    before = json.loads(_meeting(mid).parsed_details)
    clear_execution_steps(mid)
    after = json.loads(_meeting(mid).parsed_details)
    assert after["workflow"]["execution_steps"] == {}
    assert {k: v for k, v in after["workflow"].items() if k != "execution_steps"} == \
           {k: v for k, v in before["workflow"].items() if k != "execution_steps"}
