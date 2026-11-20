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

def test_agenda_skill_formats_only_the_organizers_points():
    skill = skill_registry.get("Agenda Preparation Skill")
    res = skill.run({
        "purpose": "Discuss AI project progress",
        "agenda_points": ["  review model results ", "", "1) deployment plan;"],
    })
    assert res["has_purpose"] is True
    assert len(res["agenda_points"]) == 2
    assert all(":" not in p for p in res["agenda_points"])
    assert "model results" in res["agenda_points"][0].lower()
    assert "deployment" in res["agenda_points"][1].lower()

def test_agenda_skill_never_generates_an_agenda():
    skill = skill_registry.get("Agenda Preparation Skill")
    res = skill.run({"purpose": "Discuss AI project progress", "duration_minutes": 30})
    assert res["agenda_points"] == [] and res["agenda_text"] == ""

def test_validation_skill():
    skill = skill_registry.get("Validation Skill")
    res = skill.run({
        "meeting_plan": {"scheduled_start": "2026-10-02T15:00:00Z"},
        "participants": [{"name": "Alice Chen", "status": "AVAILABLE"}],
        "mode": "ONLINE"
    })
    assert res["status"] == "PASS"
    assert res["can_proceed_to_approval"] is True
