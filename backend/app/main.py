import os
import json
import logging
import uuid
import re
from typing import Dict, Any, List, Optional
from datetime import datetime

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

from fastapi import FastAPI, HTTPException, Depends, Query, Body
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from backend.app.config import settings
from backend.app.db import get_db, Employee, Meeting, MeetingParticipant, RoomBooking, AuditLog, compute_simple_embedding, cosine_similarity
from backend.app.init_db import init_and_seed_db
from backend.runtime.runtime_loop import agent_runtime
from backend.connectors.mcp_gateway import mcp_gateway
from backend.connectors.calendar_service import get_calendar_service
from backend.tools.registry import tool_registry
from backend.skills.registry import skill_registry
from backend.memory.audit_memory import audit_memory
from backend.connectors.google_auth import delete_oauth_pkce, load_oauth_pkce, save_oauth_pkce
from worker.worker import background_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("api")
GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def google_oauth_flow(state: Optional[str] = None):
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="Google OAuth credentials are not configured.")
    if not settings.SECRET_KEY:
        raise HTTPException(status_code=503, detail="SECRET_KEY must be configured before OAuth can be used.")
    from google_auth_oauthlib.flow import Flow

    return Flow.from_client_config(
        {"web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
        }},
        scopes=GOOGLE_SCOPES,
        state=state,
        redirect_uri=settings.GOOGLE_REDIRECT_URI,
    )

app = FastAPI(
    title="MEETING ORGANIZER AGENT",
    description="Meeting planning, human approval, and asynchronous response workflow.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    init_and_seed_db()
    logger.info(f"MEETING ORGANIZER AGENT backend ready (DEMO_MODE={settings.DEMO_MODE})")

# ----------------- SYSTEM & CAPABILITIES -----------------

@app.get("/api/health")
def health():
    return {"status": "healthy", "demo_mode": settings.DEMO_MODE, "timestamp": datetime.utcnow().isoformat()}

@app.get("/api/system-info")
def system_info():
    return {
        "demo_mode": settings.DEMO_MODE,
        "openai_configured": bool(settings.OPENAI_API_KEY),
        "openai_model": settings.OPENAI_MODEL,
        "database_url": settings.DATABASE_URL.split("@")[-1],
        "registered_tools": [t["name"] for t in tool_registry.list_tools()],
        "registered_skills": [s["name"] for s in skill_registry.list_skills()],
        "mcp_capabilities": list(mcp_gateway.CAPABILITIES.keys())
    }

# ----------------- EMPLOYEE DIRECTORY -----------------

class EmployeeCreateSchema(BaseModel):
    employee_id: str
    name: str
    email: str
    designation: str
    department: str
    working_days: str = "Monday,Tuesday,Wednesday,Thursday,Friday"
    working_hours_start: str = "09:00:00"
    working_hours_end: str = "17:00:00"
    timezone: str = "America/New_York"
    meeting_preferences: str = "Prefers 30-min meetings, online preferred"
    preferred_duration: int = 30
    mode_preference: str = "Online"
    location: str = "HQ Tech Park, Building A"
    other_info: str = ""
    google_calendar_connected: bool = False

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()

@app.get("/api/employees")
def list_employees(db: Session = Depends(get_db)):
    employees = db.query(Employee).order_by(Employee.id.asc()).all()
    return [e.to_dict() for e in employees]

@app.post("/api/employees")
def create_employee(payload: EmployeeCreateSchema, db: Session = Depends(get_db)):
    existing = db.query(Employee).filter(
        (func.lower(Employee.email) == payload.email) | (Employee.employee_id == payload.employee_id)
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Employee with this email or ID already exists.")

    embed_text = f"{payload.name} {payload.designation} {payload.department} {payload.meeting_preferences} {payload.other_info}"
    embedding = compute_simple_embedding(embed_text)

    emp = Employee(
        employee_id=payload.employee_id,
        name=payload.name,
        email=payload.email,
        designation=payload.designation,
        department=payload.department,
        working_days=payload.working_days,
        working_hours_start=payload.working_hours_start,
        working_hours_end=payload.working_hours_end,
        timezone=payload.timezone,
        meeting_preferences=payload.meeting_preferences,
        preferred_duration=payload.preferred_duration,
        mode_preference=payload.mode_preference,
        location=payload.location,
        other_info=payload.other_info,
        google_calendar_connected=False,
        embedding=json.dumps(embedding)
    )
    db.add(emp)
    db.commit()
    db.refresh(emp)

    audit_memory.record_event("API", "CREATE_EMPLOYEE", details={"employee_id": emp.employee_id, "name": emp.name})
    return emp.to_dict()

@app.get("/api/employees/by-email/{email}")
def get_employee_by_email(email: str, db: Session = Depends(get_db)):
    employee = db.query(Employee).filter(func.lower(Employee.email) == email.strip().lower()).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    return employee.to_dict()

@app.put("/api/employees/by-email/{email}")
def update_employee_by_email(email: str, payload: Dict[str, Any], db: Session = Depends(get_db)):
    employee = db.query(Employee).filter(func.lower(Employee.email) == email.strip().lower()).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    for key, value in payload.items():
        if key in {"id", "created_at", "google_tokens", "google_calendar_connected"}:
            continue
        if hasattr(employee, key):
            if key == "email":
                value = str(value).strip().lower()
                duplicate = db.query(Employee).filter(
                    func.lower(Employee.email) == value,
                    Employee.id != employee.id,
                ).first()
                if duplicate:
                    raise HTTPException(status_code=409, detail="An employee with this email already exists.")
            setattr(employee, key, value)
    profile_text = f"{employee.name} {employee.designation} {employee.department} {employee.meeting_preferences} {employee.other_info}"
    employee.embedding = json.dumps(compute_simple_embedding(profile_text))
    db.commit()
    db.refresh(employee)
    return employee.to_dict()

@app.put("/api/employees/{id}")
def update_employee(id: int, payload: Dict[str, Any], db: Session = Depends(get_db)):
    emp = db.query(Employee).filter(Employee.id == id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    for k, v in payload.items():
        if hasattr(emp, k) and k not in ["id", "created_at", "google_tokens", "google_calendar_connected"]:
            setattr(emp, k, v)

    # Re-compute embedding
    embed_text = f"{emp.name} {emp.designation} {emp.department} {emp.meeting_preferences} {emp.other_info}"
    emp.embedding = json.dumps(compute_simple_embedding(embed_text))

    db.commit()
    db.refresh(emp)
    return emp.to_dict()

@app.delete("/api/employees/{id}")
def delete_employee(id: int, db: Session = Depends(get_db)):
    emp = db.query(Employee).filter(Employee.id == id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    db.delete(emp)
    db.commit()
    return {"success": True, "message": f"Employee {id} removed."}

@app.post("/api/employees/{id}/toggle-calendar")
def toggle_calendar(id: int, db: Session = Depends(get_db)):
    emp = db.query(Employee).filter(Employee.id == id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    raise HTTPException(status_code=410, detail="Use Google OAuth to connect this calendar.")

def _start_google_oauth(employee: Employee):
    if not settings.SECRET_KEY:
        raise HTTPException(status_code=503, detail="SECRET_KEY must be configured before OAuth can be used.")
    state = URLSafeTimedSerializer(settings.SECRET_KEY, salt="google-calendar-oauth").dumps({
        "employee_id": employee.id,
        "email": employee.email.strip().lower()
    })
    flow = google_oauth_flow(state)
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        code_challenge_method="S256",
    )
    if not flow.code_verifier:
        raise HTTPException(status_code=500, detail="OAuth PKCE code verifier was not created.")
    save_oauth_pkce(state, employee.id, flow.code_verifier)
    return RedirectResponse(authorization_url)


@app.get("/api/employees/{id}/calendar/connect")
def connect_employee_calendar(id: int, db: Session = Depends(get_db)):
    employee = db.query(Employee).filter(Employee.id == id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    return _start_google_oauth(employee)

@app.get("/api/employees/by-email/{email}/calendar/connect")
def connect_employee_calendar_by_email(email: str, db: Session = Depends(get_db)):
    employee = db.query(Employee).filter(func.lower(Employee.email) == email.strip().lower()).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    return _start_google_oauth(employee)

@app.get("/api/auth/google/callback")
def google_calendar_callback(request: Request, db: Session = Depends(get_db)):
    if not settings.SECRET_KEY:
        raise HTTPException(status_code=503, detail="SECRET_KEY must be configured before OAuth can be used.")
    state = request.query_params.get("state", "")
    try:
        state_data = URLSafeTimedSerializer(settings.SECRET_KEY, salt="google-calendar-oauth").loads(state, max_age=600)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=400, detail="Google OAuth state is invalid or expired.")

    frontend_url = f"http://localhost:{settings.FRONTEND_EMPLOYEE_PORT}"

    if request.query_params.get("error"):
        return RedirectResponse(f"{frontend_url}/?calendar=denied")

    try:
        from googleapiclient.discovery import build
        pkce = load_oauth_pkce(state)
        if not pkce or not pkce.get("code_verifier"):
            logger.warning("OAuth callback missing stored PKCE verifier for state")
            return RedirectResponse(f"{frontend_url}/?calendar=error")
        flow = google_oauth_flow(state)
        flow.code_verifier = pkce["code_verifier"]
        flow.fetch_token(authorization_response=str(request.url))
        account = build("oauth2", "v2", credentials=flow.credentials, cache_discovery=False).userinfo().get().execute()
        authenticated_email = account.get("email", "").strip().lower()

        # Match OAuth result using the registered EMAIL ADDRESS as unique identifier
        employee = None
        if authenticated_email:
            employee = db.query(Employee).filter(func.lower(Employee.email) == authenticated_email).first()

        # Fallback to state email if authenticated email didn't match directly
        if not employee and state_data.get("email"):
            employee = db.query(Employee).filter(func.lower(Employee.email) == state_data.get("email").strip().lower()).first()

        # Fallback to state employee_id
        if not employee and state_data.get("employee_id"):
            employee = db.query(Employee).filter(Employee.id == state_data.get("employee_id")).first()

        if not employee:
            logger.warning("No registered employee matching Google account: %s", authenticated_email)
            return RedirectResponse(f"{frontend_url}/?calendar=unregistered_email&email={authenticated_email}")

        # Persist Google tokens securely and mark calendar as connected
        employee.google_tokens = flow.credentials.to_json()
        employee.google_calendar_connected = True
        db.commit()
        db.refresh(employee)
        delete_oauth_pkce(state)
        logger.info("Successfully connected Google Calendar for employee %s (%s)", employee.name, employee.email)
        return RedirectResponse(f"{frontend_url}/?calendar=connected&email={employee.email}")
    except HTTPException:
        raise
    except Exception as error:
        logger.warning("Google OAuth callback failed: %s", error)
        return RedirectResponse(f"{frontend_url}/?calendar=error")

@app.get("/api/employees/{id}/calendar/status")
def get_employee_calendar_status(id: int, db: Session = Depends(get_db)):
    employee = db.query(Employee).filter(Employee.id == id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    return {
        "id": employee.id,
        "employee_id": employee.employee_id,
        "email": employee.email,
        "google_calendar_connected": bool(employee.google_calendar_connected),
        "status": "connected" if employee.google_calendar_connected else "not_connected",
    }

@app.get("/api/employees/by-email/{email}/calendar/status")
def get_employee_calendar_status_by_email(email: str, db: Session = Depends(get_db)):
    employee = db.query(Employee).filter(func.lower(Employee.email) == email.strip().lower()).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    return {
        "id": employee.id,
        "employee_id": employee.employee_id,
        "email": employee.email,
        "google_calendar_connected": bool(employee.google_calendar_connected),
        "status": "connected" if employee.google_calendar_connected else "not_connected",
    }

@app.delete("/api/employees/{id}/calendar")
def disconnect_employee_calendar(id: int, db: Session = Depends(get_db)):
    employee = db.query(Employee).filter(Employee.id == id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    employee.google_tokens = "{}"
    employee.google_calendar_connected = False
    db.commit()
    db.refresh(employee)
    return {
        "success": True,
        "id": employee.id,
        "employee_id": employee.employee_id,
        "email": employee.email,
        "google_calendar_connected": False,
        "status": "not_connected"
    }

@app.delete("/api/employees/by-email/{email}/calendar")
def disconnect_employee_calendar_by_email(email: str, db: Session = Depends(get_db)):
    employee = db.query(Employee).filter(func.lower(Employee.email) == email.strip().lower()).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    employee.google_tokens = "{}"
    employee.google_calendar_connected = False
    db.commit()
    db.refresh(employee)
    return {
        "success": True,
        "id": employee.id,
        "employee_id": employee.employee_id,
        "email": employee.email,
        "google_calendar_connected": False,
        "status": "not_connected"
    }

@app.get("/api/employees/search")
def search_employees(q: str = Query(..., description="Semantic or text query"), db: Session = Depends(get_db)):
    q_vec = compute_simple_embedding(q)
    employees = db.query(Employee).all()
    scored = []
    for emp in employees:
        score = 0.0
        if emp.embedding and emp.embedding != "[]":
            try:
                emb = json.loads(emp.embedding)
                score = cosine_similarity(q_vec, emb)
            except Exception:
                pass
        # Text match boost
        if q.lower() in emp.name.lower() or q.lower() in emp.designation.lower():
            score = max(score, 0.85)
        if score > 0.2:
            scored.append((score, emp))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "score": round(s[0], 3),
            "employee": s[1].to_dict()
        }
        for s in scored[:5]
    ]

# ----------------- MEETING WORKFLOW -----------------

class OrchestrateRequestSchema(BaseModel):
    request: str = Field(..., description="Natural-language meeting scheduling command")
    session_id: Optional[str] = None

@app.post("/api/meetings/orchestrate")
def orchestrate_meeting(payload: OrchestrateRequestSchema):
    if not payload.request.strip():
        raise HTTPException(status_code=400, detail="Meeting request cannot be empty.")
    try:
        plan = agent_runtime.run_meeting_request(payload.request, payload.session_id)
        return plan
    except Exception as e:
        logger.error(f"Error orchestrating meeting: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/meetings")
def list_meetings(db: Session = Depends(get_db)):
    meetings = db.query(Meeting).order_by(Meeting.created_at.desc()).all()
    result = []
    for m in meetings:
        item = m.to_dict()
        participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == m.id).all()
        item["participants"] = [p.to_dict() for p in participants]
        result.append(item)
    return result

@app.get("/api/meetings/{id}")
def get_meeting(id: str, db: Session = Depends(get_db)):
    m = db.query(Meeting).filter(Meeting.id == id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Meeting not found")
    data = m.to_dict()
    participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == id).all()
    room_bookings = db.query(RoomBooking).filter(RoomBooking.meeting_id == id).all()
    data["participants"] = [p.to_dict() for p in participants]
    data["room_bookings"] = [rb.to_dict() for rb in room_bookings]
    return data

# ----------------- HUMAN-IN-THE-LOOP APPROVAL GATE -----------------

class ApprovalGateSchema(BaseModel):
    action: str = Field(..., description="APPROVE, REJECT, or EDIT")
    notes: Optional[str] = ""
    edits: Optional[Dict[str, Any]] = None

@app.post("/api/meetings/{id}/approval")
def handle_human_approval(id: str, payload: ApprovalGateSchema, db: Session = Depends(get_db)):
    m = db.query(Meeting).filter(Meeting.id == id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Meeting not found")

    action = payload.action.upper()
    if action == "APPROVE" and m.status != "WAITING_FOR_HUMAN_APPROVAL":
        raise HTTPException(status_code=409, detail="Meeting is not waiting for approval.")
    if action in {"EDIT", "REJECT"} and m.status not in {"WAITING_FOR_HUMAN_APPROVAL", "RESCHEDULING_REQUIRED"}:
        raise HTTPException(status_code=409, detail="Meeting can no longer be edited or rejected.")

    audit_memory.record_event(
        agent="Human Operator",
        action=f"HUMAN_{action}",
        meeting_id=id,
        approval_status=action,
        details={"notes": payload.notes, "edits": payload.edits}
    )

    if action == "APPROVE":
        m.approval_status = "APPROVED"
        m.status = "APPROVED"
        m.approval_notes = payload.notes or "Approved by organizer."
        db.commit()

        # Trigger worker execution job
        exec_result = background_worker.meeting_job.execute(id)
        db.refresh(m)
        if not exec_result.get("success"):
            return {
                "success": False,
                "action": "APPROVED",
                "meeting_id": id,
                "status": m.status,
                "error": exec_result.get("error", "Approved meeting action failed."),
                "error_data": exec_result.get("error_data"),
            }
        return {
            "success": True,
            "action": "APPROVED",
            "meeting_id": id,
            "status": m.status,
            "execution": exec_result
        }

    elif action == "REJECT":
        m.approval_status = "REJECTED"
        m.status = "REJECTED"
        m.approval_notes = payload.notes or "Rejected by human operator."
        db.commit()
        return {
            "success": True,
            "action": "REJECTED",
            "meeting_id": id,
            "status": "REJECTED"
        }

    elif action == "EDIT":
        m.approval_status = "EDITED"
        if payload.edits:
            for k, v in payload.edits.items():
                if hasattr(m, k) and v is not None:
                    setattr(m, k, v)
        m.status = "WAITING_FOR_HUMAN_APPROVAL"
        db.commit()
        db.refresh(m)
        return {
            "success": True,
            "action": "EDITED",
            "meeting_id": id,
            "status": m.status,
            "meeting": m.to_dict()
        }

    else:
        raise HTTPException(status_code=400, detail=f"Invalid approval action: {action}")

# ----------------- UNKNOWN PARTICIPANT / AGENDA RESOLUTION -----------------

class ResolveParticipantSchema(BaseModel):
    queried_name: str
    selected_email: Optional[str] = None
    new_email: Optional[str] = None
    new_name: Optional[str] = None

@app.post("/api/meetings/{id}/resolve-participant")
def resolve_participant(id: str, payload: ResolveParticipantSchema, db: Session = Depends(get_db)):
    m = db.query(Meeting).filter(Meeting.id == id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Meeting not found")

    email = (payload.selected_email or payload.new_email or "").strip()

    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(status_code=400, detail="Must provide a valid participant email address.")

    employee = db.query(Employee).filter(Employee.email.ilike(email)).first()
    name = employee.name if employee else (payload.new_name or payload.queried_name).strip()
    availability = get_calendar_service().check_availability(
        [email], m.scheduled_start or "", m.scheduled_end or ""
    ).get("details", {}).get(email, {})
    existing = db.query(MeetingParticipant).filter(
        MeetingParticipant.meeting_id == id,
        MeetingParticipant.email.ilike(email),
    ).first()

    if existing:
        existing.name = name
        existing.employee_id = employee.employee_id if employee else None
        existing.is_external = employee is None
        existing.calendar_status = availability.get("status", "UNVERIFIED")
        existing.response_status = "PENDING"
    else:
        db.add(MeetingParticipant(
            meeting_id=id,
            employee_id=employee.employee_id if employee else None,
            name=name,
            email=email,
            is_external=employee is None,
            calendar_status=availability.get("status", "UNVERIFIED"),
            response_status="PENDING",
        ))

    # Re-evaluate meeting status
    m.status = "WAITING_FOR_HUMAN_APPROVAL"
    db.commit()

    audit_memory.record_event(
        agent="Meeting Coordinator",
        action="RESOLVE_PARTICIPANT",
        meeting_id=id,
        details={"name": name, "email": email}
    )

    return {"success": True, "resolved": {"name": name, "email": email}}

class SetAgendaSchema(BaseModel):
    purpose: Optional[str] = None
    agenda: Optional[str] = None
    skip: bool = False

@app.post("/api/meetings/{id}/agenda")
def update_meeting_agenda(id: str, payload: SetAgendaSchema, db: Session = Depends(get_db)):
    m = db.query(Meeting).filter(Meeting.id == id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Meeting not found")

    if payload.skip:
        m.agenda = "[Agenda skipped by organizer request]"
        m.status = "WAITING_FOR_HUMAN_APPROVAL"
    elif payload.agenda:
        m.agenda = payload.agenda
        if payload.purpose:
            m.purpose = payload.purpose
        m.status = "WAITING_FOR_HUMAN_APPROVAL"
    elif payload.purpose:
        m.purpose = payload.purpose
        # Re-draft agenda
        from backend.skills.agenda_skill import AgendaPreparationSkill
        skill = AgendaPreparationSkill()
        res = skill.run({"purpose": payload.purpose, "duration_minutes": m.duration_minutes})
        m.agenda = res.get("agenda_text", "")
        m.status = "WAITING_FOR_HUMAN_APPROVAL"

    db.commit()
    db.refresh(m)
    return {"success": True, "agenda": m.agenda, "purpose": m.purpose, "status": m.status}

# ----------------- SIMULATION & ASYNC RESUME HOOKS -----------------

class SimulateParticipantResponseSchema(BaseModel):
    email: str
    response: str = Field(..., description="ACCEPTED or REJECTED")
    notes: Optional[str] = ""

@app.post("/api/meetings/{id}/simulate-participant")
def simulate_participant(id: str, payload: SimulateParticipantResponseSchema):
    response = payload.response.upper()
    if response not in {"ACCEPTED", "REJECTED"}:
        raise HTTPException(status_code=400, detail="Response must be ACCEPTED or REJECTED.")
    res = background_worker.simulate_participant_response(
        meeting_id=id,
        email=payload.email,
        response=response,
        notes=payload.notes or ""
    )
    if res.get("status") == "ERROR":
        raise HTTPException(status_code=409, detail=res.get("message"))
    return res

class SimulateRoomApprovalSchema(BaseModel):
    room_name: str
    confirmed: bool
    notes: Optional[str] = ""

@app.post("/api/meetings/{id}/simulate-room")
def simulate_room(id: str, payload: SimulateRoomApprovalSchema):
    res = background_worker.simulate_room_approval(
        meeting_id=id,
        room_name=payload.room_name,
        confirmed=payload.confirmed,
        notes=payload.notes or ""
    )
    if res.get("status") == "ERROR":
        raise HTTPException(status_code=409, detail=res.get("message"))
    return res

# ----------------- INTERNAL AUDIT LOGS -----------------

@app.get("/api/audit-logs")
def get_audit_logs(limit: int = 50):
    return audit_memory.get_recent_logs(limit=limit)

@app.get("/api/audit-logs/{meeting_id}")
def get_meeting_trace(meeting_id: str):
    return audit_memory.get_meeting_traces(meeting_id)

# ----------------- MCP GATEWAY -----------------

class MCPRequestSchema(BaseModel):
    method: str
    params: Dict[str, Any] = {}
    id: Optional[str] = None

@app.get("/api/mcp/capabilities")
def get_mcp_capabilities():
    return mcp_gateway.list_capabilities()

@app.post("/api/mcp")
def execute_mcp(req: MCPRequestSchema):
    res = mcp_gateway.execute(method=req.method, params=req.params, req_id=req.id)
    return res.dict()
