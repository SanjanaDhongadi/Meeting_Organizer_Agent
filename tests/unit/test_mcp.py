import pytest
from backend.connectors.mcp_gateway import mcp_gateway

def test_mcp_capabilities():
    caps = mcp_gateway.list_capabilities()
    assert "capabilities" in caps
    c_keys = caps["capabilities"].keys()
    assert "calendar.check_availability" in c_keys
    assert "calendar.create_event" in c_keys
    assert "calendar.create_meet" in c_keys
    assert "gmail.send" in c_keys
    assert "room.request" in c_keys
    assert "room.status" in c_keys

def test_mcp_validation_error():
    # Calling calendar.create_event without required title
    res = mcp_gateway.execute(
        method="calendar.create_event",
        params={"start_time": "2026-10-02T15:00:00Z"}
    )
    assert res.error is not None
    assert res.error["code"] == -32602
    assert "missing required fields" in res.error["message"]

def test_mcp_unknown_method():
    res = mcp_gateway.execute(
        method="unregistered.unknown_method",
        params={}
    )
    assert res.error is not None
    assert res.error["code"] == -32601

def test_mcp_successful_execution():
    res = mcp_gateway.execute(
        method="calendar.check_availability",
        params={
            "emails": ["alice.chen@example.com"],
            "start_time": "2026-10-02T15:00:00Z",
            "end_time": "2026-10-02T16:00:00Z"
        }
    )
    assert res.error is None
    assert res.result is not None
    assert "details" in res.result
