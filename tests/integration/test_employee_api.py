import pytest
from fastapi import HTTPException

from backend.app.db import SessionLocal, Employee
from backend.app.main import (
    EmployeeCreateSchema,
    create_employee,
    get_employee_by_email,
    update_employee_by_email,
)
from backend.runtime.runtime_loop import agent_runtime


def test_employee_profile_persists_and_agent_resolves_by_email():
    email = "integration.user@example.test"
    db = SessionLocal()
    employee = db.query(Employee).filter(Employee.email == email).first()
    if employee:
        db.delete(employee)
        db.commit()

    try:
        created = create_employee(EmployeeCreateSchema(
            employee_id="INTEGRATION-EMP-01",
            name="Integration User",
            email=email,
            designation="Quality Engineer",
            department="Quality",
            working_days="Monday,Tuesday,Wednesday,Thursday,Friday",
            working_hours_start="09:00:00",
            working_hours_end="17:00:00",
            timezone="UTC",
            meeting_preferences="Afternoons preferred",
            preferred_duration=30,
            mode_preference="Online",
            location="Remote",
        ), db)
        assert created["email"] == email
        assert get_employee_by_email(email.upper(), db)["name"] == "Integration User"

        updated = update_employee_by_email(email, {"department": "Platform"}, db)
        assert updated["department"] == "Platform"

        with pytest.raises(HTTPException) as duplicate:
            create_employee(EmployeeCreateSchema(
                employee_id="INTEGRATION-EMP-02",
                name="Another User",
                email=email.upper(),
                designation="Engineer",
                department="Platform",
            ), db)
        assert duplicate.value.status_code == 400

        plan = agent_runtime.run_meeting_request(
            "Schedule an online meeting with Integration User on Friday at 3 PM to discuss onboarding."
        )
        assert [person["email"] for person in plan["participants"]] == [email]
    finally:
        db.query(Employee).filter(Employee.email == email).delete(synchronize_session=False)
        db.commit()
        db.close()