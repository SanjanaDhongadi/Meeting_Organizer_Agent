import pytest
from backend.memory.long_term_memory import long_term_memory
from backend.memory.session_memory import session_memory
from backend.memory.audit_memory import audit_memory

def test_long_term_memory_rag_search():
    res = long_term_memory.search_context("Alice Chen AI Research")
    assert "relevant_employees" in res
    assert len(res["relevant_employees"]) > 0
    matched_names = [r["profile"]["name"] for r in res["relevant_employees"]]
    assert any("Alice" in name for name in matched_names)

def test_session_memory_lifecycle():
    s = session_memory.init_session("test-sess-1", "Schedule meeting with Bob")
    assert s["status"] == "DRAFT"
    
    session_memory.update_session("test-sess-1", {"status": "WAITING_FOR_HUMAN_APPROVAL"})
    loaded = session_memory.get_session("test-sess-1")
    assert loaded["status"] == "WAITING_FOR_HUMAN_APPROVAL"

def test_audit_memory_trace():
    audit_memory.record_event(
        agent="Meeting Coordinator",
        action="TEST_ACTION",
        meeting_id="test-audit-sess",
        status="SUCCESS",
        details={"key": "value"}
    )
    traces = audit_memory.get_meeting_traces("test-audit-sess")
    assert len(traces) >= 1
    assert traces[0]["action"] == "TEST_ACTION"
