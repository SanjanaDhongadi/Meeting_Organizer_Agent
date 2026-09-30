import pytest
from backend.skills.registry import skill_registry

def test_registry_contains_all_skills():
    skills = skill_registry.list_skills()
    names = [s["name"] for s in skills]
    assert "Meeting Scheduling Skill" in names
    assert "Agenda Preparation Skill" in names
    assert "Validation Skill" in names

def test_scheduling_skill():
    skill = skill_registry.get("Meeting Scheduling Skill")
    res = skill.run({
        "participant_emails": ["alice.chen@example.com"],
        "target_date": "2026-10-02",
        "target_start_time": "2026-10-02T15:00:00Z",
        "target_end_time": "2026-10-02T15:30:00Z"
    })
    assert res["is_slot_available"] is False
    assert res["participant_statuses"]["alice.chen@example.com"]["status"] == "UNVERIFIED"
    assert res["selected_slot"] is not None

def test_agenda_skill_with_purpose():
    skill = skill_registry.get("Agenda Preparation Skill")
    res = skill.run({
        "purpose": "Discuss AI project progress",
        "duration_minutes": 30,
        "participants": ["Alice Chen"]
    })
    assert res["has_purpose"] is True
    assert "Agenda" in res["agenda_text"]
    assert res["action_prompt"] is None

def test_agenda_skill_without_purpose():
    skill = skill_registry.get("Agenda Preparation Skill")
    res = skill.run({"purpose": "", "duration_minutes": 30})
    assert res["has_purpose"] is False
    assert res["action_prompt"] is not None
    assert "What is the purpose" in res["action_prompt"]

def test_validation_skill():
    skill = skill_registry.get("Validation Skill")
    res = skill.run({
        "meeting_plan": {"scheduled_start": "2026-10-02T15:00:00Z"},
        "participants": [{"name": "Alice Chen", "status": "AVAILABLE"}],
        "mode": "ONLINE"
    })
    assert res["status"] == "PASS"
    assert res["can_proceed_to_approval"] is True
