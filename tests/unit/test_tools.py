import pytest
from backend.tools.registry import tool_registry

def test_registry_contains_all_12_tools():
    expected_tools = [
        "find_participant",
        "retrieve_participant_profile",
        "check_calendar_availability",
        "find_common_slots",
        "create_calendar_event",
        "create_google_meet",
        "send_email",
        "request_room_booking",
        "check_room_status",
        "save_meeting_state",
        "load_meeting_state",
        "create_audit_log"
    ]
    registered = [t["name"] for t in tool_registry.list_tools()]
    for tool_name in expected_tools:
        assert tool_name in registered

def test_find_participant_tool():
    tool = tool_registry.get("find_participant")
    res = tool.execute(query="Alice Chen")
    assert res.success is True
    assert res.data["match_type"] == "EXACT_MATCH"

def test_find_participant_unknown():
    tool = tool_registry.get("find_participant")
    res = tool.execute(query="NonExistentPerson")
    assert res.success is True
    assert res.data["match_type"] == "UNKNOWN"

def test_check_calendar_availability_tool():
    tool = tool_registry.get("check_calendar_availability")
    res = tool.execute(
        emails=["alice.chen@example.com"],
        start_time="2026-10-02T15:00:00Z",
        end_time="2026-10-02T16:00:00Z"
    )
    assert res.success is True
    assert "details" in res.data

def test_create_google_meet_tool():
    tool = tool_registry.get("create_google_meet")
    res = tool.execute(
        title="Quick Sync",
        start_time="2026-10-02T15:00:00Z",
        end_time="2026-10-02T15:30:00Z"
    )
    assert res.success is True
    assert "meet_url" in res.data
    assert "meet.google.com" in res.data["meet_url"]
