import json

import pytest

from backend.app.db import Employee, SessionLocal, compute_simple_embedding

TEST_EMPLOYEES = [
    {
        "employee_id": "TEST001", "name": "Alice Chen", "email": "alice.chen@example.com",
        "designation": "Research Engineer", "department": "Research",
        "working_days": "Monday,Tuesday,Wednesday,Thursday,Friday", "working_hours_start": "09:00:00",
        "working_hours_end": "17:00:00", "timezone": "UTC", "meeting_preferences": "Online preferred",
        "preferred_duration": 30, "mode_preference": "Online", "location": "Remote", "other_info": "",
    },
    {
        "employee_id": "TEST002", "name": "Bob Smith", "email": "bob.smith@example.com",
        "designation": "Product Manager", "department": "Product",
        "working_days": "Monday,Tuesday,Wednesday,Thursday,Friday", "working_hours_start": "09:00:00",
        "working_hours_end": "17:00:00", "timezone": "UTC", "meeting_preferences": "Available during work hours",
        "preferred_duration": 30, "mode_preference": "Online", "location": "Remote", "other_info": "",
    },
    {
        "employee_id": "TEST003", "name": "Carol Davis", "email": "carol.davis@example.com",
        "designation": "Engineering Manager", "department": "Engineering",
        "working_days": "Monday,Tuesday,Wednesday,Thursday", "working_hours_start": "10:00:00",
        "working_hours_end": "16:00:00", "timezone": "UTC", "meeting_preferences": "No Friday meetings",
        "preferred_duration": 30, "mode_preference": "Offline", "location": "Remote", "other_info": "",
    },
    {
        "employee_id": "TEST004", "name": "David Miller", "email": "david.miller@example.com",
        "designation": "Infrastructure Architect", "department": "Infrastructure",
        "working_days": "Monday,Tuesday,Wednesday,Thursday,Friday", "working_hours_start": "08:00:00",
        "working_hours_end": "16:00:00", "timezone": "UTC", "meeting_preferences": "Online preferred",
        "preferred_duration": 30, "mode_preference": "Online", "location": "Remote", "other_info": "",
    },
    {
        "employee_id": "TEST005", "name": "David Martinez", "email": "david.martinez@example.com",
        "designation": "UX Researcher", "department": "Design",
        "working_days": "Monday,Tuesday,Wednesday,Thursday,Friday", "working_hours_start": "09:00:00",
        "working_hours_end": "17:00:00", "timezone": "UTC", "meeting_preferences": "Workshop preferred",
        "preferred_duration": 30, "mode_preference": "Hybrid", "location": "Remote", "other_info": "",
    },
]


@pytest.fixture(scope="session", autouse=True)
def test_employee_directory():
    db = SessionLocal()
    inserted_emails = []
    try:
        existing = {employee.email.lower() for employee in db.query(Employee).all()}
        for record in TEST_EMPLOYEES:
            if record["email"] in existing:
                continue
            profile = " ".join(record[key] for key in ("name", "designation", "department", "meeting_preferences"))
            db.add(Employee(**record, embedding=json.dumps(compute_simple_embedding(profile))))
            inserted_emails.append(record["email"])
        db.commit()
        yield
    finally:
        if inserted_emails:
            db.query(Employee).filter(Employee.email.in_(inserted_emails)).delete(synchronize_session=False)
            db.commit()
        db.close()