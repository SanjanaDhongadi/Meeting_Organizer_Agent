"""The agenda an organizer writes in the natural-language request is extracted as an ordered list, offered pre-filled
in the clarification step, and stored unchanged in order in the final meeting record."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.agents.coordinator_agent import MeetingCoordinator
from backend.app.db import Meeting, SessionLocal
from backend.app.main import app
from backend.skills.agenda_skill import extract_agenda_points

client = TestClient(app)
POINTS = ["Budget review", "Vendor selection", "Timeline risks", "Hiring plan", "Next steps"]
BASE = "Schedule an online meeting with Nagveni tomorrow at 2pm for 30 minutes to review the Q3 launch."

REQUESTS = {
    "numbered inline": BASE + " Agenda: 1. Budget review 2. Vendor selection 3. Timeline risks 4. Hiring plan 5. Next steps",
    "numbered lines": BASE + "\nAgenda:\n1. Budget review\n2) Vendor selection\n3. Timeline risks\n4. Hiring plan\n5. Next steps",
    "bullets": BASE + "\nAgenda items:\n- Budget review\n- Vendor selection\n- Timeline risks\n- Hiring plan\n- Next steps\n\nPlease invite Bob.",
    "bullets inline": BASE + " Agenda: - Budget review - Vendor selection - Timeline risks - Hiring plan - Next steps",
    "commas": BASE + " Agenda is budget review, vendor selection, timeline risks, hiring plan and next steps",
    "semicolons": BASE + " Topics to cover: budget review; vendor selection; timeline risks; hiring plan; next steps. Please send the invite to Bob.",
}


@pytest.mark.parametrize("name", REQUESTS)
def test_extraction_formats(name):
    assert extract_agenda_points(REQUESTS[name]) == POINTS


def test_no_agenda_means_no_points_and_purpose_is_not_an_agenda():
    assert extract_agenda_points(BASE) == []
    parsed = MeetingCoordinator().parse_natural_language_request(BASE)
    assert parsed["agenda_points"] == [] and parsed["purpose"]


def test_parser_returns_points_separately_from_purpose():
    parsed = MeetingCoordinator().parse_natural_language_request(REQUESTS["numbered inline"])
    assert parsed["agenda_points"] == POINTS
    assert "Agenda" not in (parsed["purpose"] or "") and "Budget review" not in (parsed["purpose"] or "")


def test_llm_result_never_loses_or_renumbers_points():
    coordinator = MeetingCoordinator()
    fallback = coordinator.parse_natural_language_request(REQUESTS["commas"])
    merged = coordinator._normalize_llm_parse({"agenda_points": ["budget review, vendor selection"], "purpose": "Review launch"}, fallback)
    assert merged["agenda_points"] == POINTS                       # the model merged items: rule-based list wins
    numbered = coordinator._normalize_llm_parse({"agenda_points": [f"{i}. {p.lower()}" for i, p in enumerate(POINTS, 1)]}, fallback)
    assert numbered["agenda_points"] == POINTS                     # numbering stripped, order kept
    empty = coordinator._normalize_llm_parse({"agenda_points": []}, fallback)
    assert empty["agenda_points"] == POINTS


def _login():
    email = f"agenda_{uuid.uuid4().hex[:6]}@example.com"
    token = client.post("/api/auth/register", json={"email": email, "password": "secret123", "name": "Pat"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("name", ["numbered inline", "numbered lines", "commas"])
def test_agenda_flows_through_clarification_to_the_final_record(name):
    headers = _login()
    first = client.post("/api/meetings/orchestrate", json={"request": REQUESTS[name]}, headers=headers).json()
    mid = first["meeting_id"]
    # what the "A few details are needed" form is pre-filled from
    assert first["agenda_points"] == POINTS and first["understood"]["agenda_points"] == POINTS
    assert first["understood"]["purpose"] and "Budget review" not in first["understood"]["purpose"]

    # the form submits the (unchanged) pre-filled points together with the answers it asked for
    res = client.post(f"/api/meetings/{mid}/clarify", headers=headers, json={
        "participant_emails": {"Nagveni": "nagveni@example.com"}, "title": "Q3 launch review", "agenda_points": first["agenda_points"]})
    assert res.status_code == 200, res.text
    plan = res.json()
    assert plan["status"] == "WAITING_FOR_HUMAN_APPROVAL" and plan["agenda_points"] == POINTS

    lines = [ln.split(". ", 1)[1] for ln in plan["agenda"].splitlines() if ln.strip() and ln.split(". ", 1)[0].isdigit()]
    assert len(lines) == 5                                          # nothing generated, nothing dropped
    for original, final in zip(POINTS, lines):                      # same topics, same order
        assert original.split()[0].lower() in final.lower()
    assert plan["purpose"] and plan["purpose"] not in plan["agenda"]       # purpose did not replace the agenda
    db = SessionLocal()
    try:
        row = db.query(Meeting).filter(Meeting.id == mid).first()
        assert json.loads(row.parsed_details)["agenda_points"] == POINTS
        assert row.agenda == plan["agenda"]
    finally:
        db.close()


def test_organizer_can_edit_add_and_remove_points():
    headers = _login()
    first = client.post("/api/meetings/orchestrate", json={"request": REQUESTS["numbered inline"]}, headers=headers).json()
    mid = first["meeting_id"]
    edited = ["Budget review", "Hiring plan", "Security audit"]       # removed two, kept order, added one
    plan = client.post(f"/api/meetings/{mid}/clarify", headers=headers, json={
        "participant_emails": {"Nagveni": "nagveni@example.com"}, "title": "Q3 launch review", "agenda_points": edited}).json()
    assert plan["agenda_points"] == edited
    assert "Security audit" in plan["agenda"] and "Vendor" not in plan["agenda"]


def test_no_agenda_stays_optional_and_nothing_is_generated():
    headers = _login()
    first = client.post("/api/meetings/orchestrate", json={"request": BASE}, headers=headers).json()
    assert first["agenda_points"] == []
    plan = client.post(f"/api/meetings/{first['meeting_id']}/clarify", headers=headers, json={
        "participant_emails": {"Nagveni": "nagveni@example.com"}, "title": "Q3 launch review", "agenda_points": []}).json()
    assert plan["status"] == "WAITING_FOR_HUMAN_APPROVAL" and plan["agenda_points"] == []
