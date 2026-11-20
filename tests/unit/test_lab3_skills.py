"""
Lab 3 - reusable, schema-typed skills: Meeting Plan (locked), Meeting Format (templates), Minutes of Meeting.
The expected e-mail strings in the format tests were produced by the code that existed BEFORE it was moved into
the Format skill (invitation text from node_execute, MinutesJob.render_email), so they prove nothing changed.
"""
import pytest
from pydantic import ValidationError

from backend.app.db import ActionItem, Meeting
from backend.skills.format_skill import FormatMeetingSkill
from backend.skills.minutes_skill import MinutesOfMeetingSkill
from backend.skills.plan_skill import MeetingPlan, PlanSkill, plan_differences
from backend.skills.registry import skill_registry
from worker.jobs.minutes_job import MinutesJob

# ----------------- Meeting Plan Skill -----------------

ONLINE = {
    "title": "Design Review", "purpose": "Review designs", "date": "2030-01-08",
    "start_time": "2030-01-08T09:00:00Z", "end_time": "2030-01-08T09:30:00Z", "duration_minutes": 30, "mode": "ONLINE",
    "participants": [{"name": "Ann", "email": "ann@example.test"}], "agenda_points": ["Models", "Plan"],
}
OFFLINE = {
    "title": "Workshop", "purpose": "Layouts", "date": "2030-01-08", "start_time": "2030-01-08T09:00:00Z",
    "end_time": "2030-01-08T10:00:00Z", "duration_minutes": 60, "mode": "offline", "room_name": "Conference Room B",
    "venue_coordinator_email": "Venue.Coord@College.edu",
    "participants": [{"name": "Bo", "email": "bo@example.test"}, {"name": "Ann", "email": "ann@example.test"}],
    "agenda_points": ["Layouts"],
}


@pytest.mark.parametrize("details,mode,venue", [(ONLINE, "ONLINE", ""), (OFFLINE, "OFFLINE", "Conference Room B")])
def test_plan_skill_handles_online_and_offline_without_code_changes(details, mode, venue):
    plan = PlanSkill().run(details)
    assert isinstance(plan, MeetingPlan)
    assert plan.mode == mode and plan.room_name == venue and plan.version == 1
    assert plan.locked_at.endswith("Z") and len(plan.plan_hash) == 64
    assert isinstance(plan.participants, tuple) and isinstance(plan.agenda_points, tuple)
    assert [p.email for p in plan.participants] == sorted(p["email"] for p in details["participants"])


def test_locked_plan_is_immutable():
    plan = PlanSkill().run(ONLINE)
    with pytest.raises(ValidationError):
        plan.title = "Something else"
    with pytest.raises(ValidationError):
        plan.version = 9


def test_same_input_keeps_hash_and_version_changed_input_bumps_version():
    skill = PlanSkill()
    v1 = skill.run(ONLINE)
    again = skill.run({**ONLINE, "previous_plan": v1.model_dump(mode="json")})
    assert (again.plan_hash, again.version, again.locked_at) == (v1.plan_hash, 1, v1.locked_at)
    edited = {**ONLINE, "start_time": "2030-01-08T10:00:00Z"}
    changed = skill.run({**edited, "previous_plan": v1.model_dump(mode="json")})
    assert changed.plan_hash != v1.plan_hash and changed.version == 2
    assert skill.run({**edited, "previous_plan": changed.model_dump(mode="json")}).version == 2      # unchanged again
    assert skill.run({**ONLINE, "previous_plan": changed.model_dump(mode="json")}).version == 3      # changed back = new lock


def test_plan_differences_name_each_changed_field():
    plan = PlanSkill().run(OFFLINE).model_dump(mode="json")
    emails = ["ann@example.test", "bo@example.test"]
    assert plan_differences(plan, plan["start_time"], plan["end_time"], "OFFLINE", "conference room b", emails) == []
    assert plan_differences(plan, "2030-01-08T11:00:00Z", plan["end_time"], "ONLINE", "Other room", emails[:1]) == [
        "start_time", "mode", "venue", "participants"]


def test_plan_is_locked_into_parsed_details_by_the_workflow():
    from backend.app.db import SessionLocal
    from backend.graph.workflow_graph import start_meeting_workflow
    state = start_meeting_workflow("Schedule an online roadmap sync with alice.chen@example.com on 2030-01-10 at 11 AM "
                                   "for 30 minutes to discuss the product roadmap.")
    if state.get("status") == "NEEDS_INFORMATION":
        pytest.skip("the request parser asked for more details in this environment")
    db = SessionLocal()
    try:
        import json
        m = db.query(Meeting).filter(Meeting.id == state["meeting_id"]).first()
        plan = json.loads(m.parsed_details)["plan"]
    finally:
        db.close()
    assert plan["version"] == 1 and plan["plan_hash"] and plan["start_time"] == m.scheduled_start


# ----------------- Meeting Format Skill -----------------

INVITE_ONLINE = ("Invitation: Design Review",
                 "You are invited to: Design Review\nPurpose: Review designs\n"
                 "When: Tuesday, 08 January 2030, 9:00 AM – 9:30 AM (UTC)\nGoogle Meet: https://meet.google.com/abc-defg-hij\n\n"
                 "Agenda:\n1. Models\n2. Plan\n\nPlease accept or decline the Google Calendar invitation.")
INVITE_NO_LINK = ("Invitation: Sync",
                  "You are invited to: Sync\nPurpose: (not provided)\n"
                  "When: Tuesday, 08 January 2030, 9:00 AM – 9:30 AM (UTC)\n"
                  "Google Meet: not available (see calendar invitation)\n\nAgenda:\n(no agenda provided)\n\n"
                  "Please accept or decline the Google Calendar invitation.")
INVITE_OFFLINE = ("Invitation: Workshop",
                  "You are invited to: Workshop\nPurpose: Layouts\n"
                  "When: Tuesday, 08 January 2030, 9:00 AM – 10:00 AM (UTC)\nVenue: Conference Room B\n\n"
                  "Agenda:\n1. Layouts\n\nPlease accept or decline the Google Calendar invitation.")
MINUTES_FULL = {
    "subject": "Minutes of Meeting: Q3 Review (2030-01-08)",
    "body": "Minutes of Meeting: Q3 Review\n\nDate: 2030-01-08\nDuration: 30 minutes\nParticipants: Alice Chen, Bob Smith\n\n"
            "SUMMARY\nWe reviewed Q3.\n\nKEY DISCUSSION POINTS\n- Budget: Over by 5%\n- Hiring\n\nDECISIONS\n- Freeze hiring\n\n"
            "ACTION ITEMS\n1. Send report | Owner: Alice Chen | Due: 2030-01-15\n2. Book room | Owner: Not specified | "
            "Due: Not specified\n\nYou can track your action items in the Meeting Organizer app.",
}
MINUTES_EMPTY = {
    "subject": "Minutes of Meeting: Empty (n/a)",
    "body": "Minutes of Meeting: Empty\n\nDate: Not specified\nDuration: Not specified minutes\nParticipants: Not specified\n\n"
            "SUMMARY\nNot specified\n\nKEY DISCUSSION POINTS\nNone recorded\n\nDECISIONS\nNone recorded\n\nACTION ITEMS\n"
            "None recorded\n\nYou can track your action items in the Meeting Organizer app.",
}


def _invite(**kw):
    base = {"title": "Design Review", "purpose": "Review designs", "when_date": "Tuesday, 08 January 2030",
            "when_time": "9:00 AM – 9:30 AM (UTC)", "is_online": True, "venue": "",
            "meet_url": "https://meet.google.com/abc-defg-hij", "agenda": "1. Models\n2. Plan"}
    return FormatMeetingSkill().run({**base, **kw}, "invite")


def test_invite_template_matches_the_previous_invitation_text():
    doc = _invite()
    assert (doc["subject"], doc["body"]) == INVITE_ONLINE
    doc = _invite(title="Sync", purpose="", meet_url="", agenda="")
    assert (doc["subject"], doc["body"]) == INVITE_NO_LINK
    doc = _invite(title="Workshop", purpose="Layouts", is_online=False, meet_url="", venue="Conference Room B",
                  when_time="9:00 AM – 10:00 AM (UTC)", agenda="1. Layouts")
    assert (doc["subject"], doc["body"]) == INVITE_OFFLINE


def test_minutes_template_matches_the_previous_minutes_email():
    draft = {"meeting": {"date": "2030-01-08", "duration_minutes": 30,
                         "participants": [{"name": "Alice Chen"}, {"name": "Bob Smith"}]},
             "summary": "We reviewed Q3.",
             "discussion_points": [{"topic": "Budget", "details": "Over by 5%"}, {"topic": "Hiring", "details": ""}],
             "decisions": [{"text": "Freeze hiring", "evidence": "x"}]}
    items = [ActionItem(description="Send report", owner_name="Alice Chen", due_date="2030-01-15"),
             ActionItem(description="Book room", owner_name="", due_date=None)]
    assert MinutesJob.render_email(Meeting(id="m1", title="Q3 Review"), draft, items) == MINUTES_FULL
    assert MinutesJob.render_email(Meeting(id="m2", title="Empty"), {}, []) == MINUTES_EMPTY


def test_unknown_template_raises():
    with pytest.raises(ValueError):
        FormatMeetingSkill().run({"title": "x"}, "newsletter")


# ----------------- Minutes of Meeting Skill -----------------

TRANSCRIPT = ("[00:01] Alice Chen: I will send the quarterly report by Friday 15 January.\n"
              "[00:02] Bob Smith: We decided to freeze hiring this quarter.\n"
              "[00:03] Alice Chen: Someone should also book the venue by the 20th.\n")
PEOPLE = [{"name": "Alice Chen", "email": "alice@example.test"}, {"name": "Bob Smith", "email": "bob@example.test"}]


def _run(raw):
    calls = []

    def complete(system, user):
        calls.append((system, user))
        return raw
    out = MinutesOfMeetingSkill().run({"transcript": TRANSCRIPT, "participants": PEOPLE,
                                       "meeting": {"title": "Q3", "date": "2030-01-08"}}, complete=complete)
    return out, calls


def test_minutes_skill_keeps_grounded_items_and_drops_ungrounded_ones():
    out, calls = _run({
        "summary": "Hiring was frozen.", "topic": "Q3",
        "decisions": [{"decision": "Freeze hiring", "evidence": "We decided to freeze hiring this quarter"},
                      {"decision": "Buy a new office", "evidence": "we will buy a new office downtown"}],
        "action_items": [
            {"description": "Send the quarterly report", "owner": "Alice Chen", "due_date": "2030-01-15",
             "due_date_text": "by Friday 15 January", "evidence": "I will send the quarterly report by Friday 15 January"},
            {"description": "Hire a designer", "owner": "Bob Smith", "evidence": "I will hire a designer next week"},
        ],
    })
    assert [d.text for d in out.decisions] == ["Freeze hiring"]
    assert [i.description for i in out.action_items] == ["Send the quarterly report"]
    item = out.action_items[0]
    assert item.owner_email == "alice@example.test" and item.due_date == "2030-01-15" and item.project_topic == "Q3"
    assert any("1 decision(s)" in w for w in out.warnings) and any("1 action item(s)" in w for w in out.warnings)
    assert calls and "Meeting: Q3" in calls[0][1] and "Alice Chen, Bob Smith" in calls[0][1]


def test_due_date_not_in_transcript_is_cleared_and_unknown_owner_gets_no_email():
    out, _ = _run({"action_items": [
        {"description": "Send the quarterly report", "owner": "Alice Chen", "due_date": "2030-02-01",
         "due_date_text": "by the first of February", "evidence": "I will send the quarterly report by Friday 15 January"},
        {"description": "Book the venue", "owner": "Carl Stranger", "evidence": "Someone should also book the venue by the 20th"},
    ]})
    by_desc = {i.description: i for i in out.action_items}
    assert by_desc["Send the quarterly report"].due_date is None
    stranger = by_desc["Book the venue"]
    assert stranger.owner_name == "Carl Stranger" and stranger.owner_email is None
    assert any("could not be matched" in w for w in out.warnings)


def test_minutes_skill_never_calls_a_model_itself():
    with pytest.raises(RuntimeError, match="boom"):
        MinutesOfMeetingSkill().run({"transcript": TRANSCRIPT}, complete=lambda s, u: (_ for _ in ()).throw(RuntimeError("boom")))


# ----------------- Registry -----------------

def test_registry_lists_the_new_skills_with_schemas():
    listed = {s["name"]: s for s in skill_registry.list_skills()}
    for name in ("Meeting Plan Skill", "Meeting Format Skill", "Minutes of Meeting Skill"):
        assert "properties" in listed[name]["input_schema"] and "properties" in listed[name]["output_schema"]
    assert "plan_hash" in listed["Meeting Plan Skill"]["output_schema"]["properties"]
    for name in ("Meeting Scheduling Skill", "Agenda Preparation Skill", "Validation Skill"):
        assert "input_schema" not in listed[name]          # existing skills unchanged
