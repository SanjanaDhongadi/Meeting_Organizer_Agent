import os
import json
import logging
import uuid
import re
from typing import Dict, Any, List, Optional
from datetime import datetime

from backend.app.config import settings

# Plain-HTTP OAuth redirects are only allowed for local development redirect URIs.
if settings.GOOGLE_REDIRECT_URI.startswith("http://localhost") or settings.GOOGLE_REDIRECT_URI.startswith("http://127.0.0.1"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
# With include_granted_scopes=true Google may return previously granted scopes too; do not treat that as an error.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Depends, Query, Body
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from backend.app.db import get_db, Employee, Meeting, MeetingParticipant, RoomBooking, AuditLog, compute_simple_embedding, cosine_similarity
from backend.app.init_db import init_and_seed_db
from backend.runtime.runtime_loop import agent_runtime
from backend.connectors.mcp_gateway import mcp_gateway
from backend.connectors.calendar_service import get_calendar_service
from backend.tools.registry import tool_registry
from backend.skills.registry import skill_registry
from backend.memory.audit_memory import audit_memory
from backend.connectors.google_auth import consume_oauth_pkce, save_oauth_pkce, google_error_detail
from backend.graph.workflow_graph import WorkflowError, resume_meeting_workflow, workflow_view
from worker.worker import background_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("api")
GOOGLE_SCOPES = settings.google_scopes
OAUTH_STATE_SALT = "google-calendar-oauth"


def google_oauth_flow(state: Optional[str] = None, code_verifier: Optional[str] = None):
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="Google OAuth credentials are not configured (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).")
    if not settings.SECRET_KEY:
        raise HTTPException(status_code=503, detail="SECRET_KEY must be configured before OAuth can be used.")
    from google_auth_oauthlib.flow import Flow

    return Flow.from_client_config(
        {"web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": settings.GOOGLE_AUTH_URI,
            "token_uri": settings.GOOGLE_TOKEN_URI,
            "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
        }},
        scopes=GOOGLE_SCOPES,
        state=state,
        redirect_uri=settings.GOOGLE_REDIRECT_URI,
        # Callback flows must reuse the verifier created for the authorization request — never a new one.
        code_verifier=code_verifier,
        autogenerate_code_verifier=code_verifier is None,
    )


def _frontend_redirect(**params) -> RedirectResponse:
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return RedirectResponse(f"{settings.FRONTEND_URL.rstrip('/')}/?{query}")

app = FastAPI(
    title="MEETING ORGANIZER AGENT",
    description="Meeting planning, human approval, and asynchronous response workflow.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
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
        "mcp_capabilities": list(mcp_gateway.CAPABILITIES.keys()),
        # Configuration status only — secrets are never exposed to the frontend.
        "google": {
            "oauth_configured": bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET and settings.SECRET_KEY),
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "scopes": GOOGLE_SCOPES,
            "project_id_configured": bool(settings.GOOGLE_CLOUD_PROJECT_ID),
            "calendar_owner_configured": bool(settings.GOOGLE_CALENDAR_OWNER_EMAIL),
            "hosted_domain": settings.GOOGLE_HOSTED_DOMAIN,
        },
        "auditorium_booking_email": settings.AUDITORIUM_BOOKING_EMAIL,
        "workflow": "langgraph",
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
    timezone: str = "UTC"
    # No invented preferences/locations: empty unless the employee provides them.
    meeting_preferences: str = ""
    preferred_duration: int = 30
    mode_preference: str = "Online"
    location: str = ""
    other_info: str = ""
    google_calendar_connected: bool = False

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("A valid email address is required (it is the employee's unique identity).")
        return value

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
                if value != employee.email.strip().lower() and employee.google_calendar_connected:
                    # Tokens belong to the old Google account; never keep them under a different identity.
                    employee.google_tokens = "{}"
                    employee.google_calendar_connected = False
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
    # Same rules as the by-email update (email normalization, uniqueness, token safety).
    return update_employee_by_email(emp.email, payload, db)

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
    state = URLSafeTimedSerializer(settings.SECRET_KEY, salt=OAUTH_STATE_SALT).dumps({
        "employee_id": employee.id,
        "email": employee.email.strip().lower(),
        "nonce": uuid.uuid4().hex,
    })
    flow = google_oauth_flow(state)
    extra = {"login_hint": employee.email}
    if settings.GOOGLE_HOSTED_DOMAIN:
        extra["hd"] = settings.GOOGLE_HOSTED_DOMAIN
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        **extra,
    )
    if not flow.code_verifier:
        raise HTTPException(status_code=500, detail="OAuth PKCE code verifier was not created.")
    # The verifier never leaves the server: it is stored against this single-use state value.
    save_oauth_pkce(state, employee.id, flow.code_verifier)
    audit_memory.record_event("API", "GOOGLE_OAUTH_STARTED", details={"employee_email": employee.email})
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
    # 1. The state must be one we signed, and not older than the configured TTL.
    try:
        state_data = URLSafeTimedSerializer(settings.SECRET_KEY, salt=OAUTH_STATE_SALT).loads(
            state, max_age=settings.GOOGLE_OAUTH_STATE_TTL_SECONDS
        )
    except SignatureExpired:
        logger.warning("Google OAuth callback rejected: state expired")
        return _frontend_redirect(calendar="error", reason="The Google sign-in link expired. Start the connection again.")
    except BadSignature:
        logger.warning("Google OAuth callback rejected: state signature invalid")
        raise HTTPException(status_code=400, detail="Google OAuth state is invalid.")

    # 2. The state must still have its server-side PKCE record (single use), bound to the same employee.
    pkce = consume_oauth_pkce(state)
    state_email = (state_data.get("email") or "").strip().lower()
    if not pkce or not pkce.get("code_verifier"):
        logger.warning("Google OAuth callback rejected: no pending PKCE verifier for this state (already used or expired)")
        return _frontend_redirect(calendar="error", email=state_email,
                                  reason="This Google sign-in was already used or has expired. Start the connection again.")
    if pkce["employee_id"] != state_data.get("employee_id"):
        logger.error("Google OAuth callback rejected: state/employee mismatch (state=%s, stored=%s)",
                     state_data.get("employee_id"), pkce["employee_id"])
        raise HTTPException(status_code=400, detail="Google OAuth state does not match the pending authorization.")

    employee = db.query(Employee).filter(Employee.id == pkce["employee_id"]).first()
    if not employee or employee.email.strip().lower() != state_email:
        return _frontend_redirect(calendar="error", email=state_email,
                                  reason="The employee who started this connection no longer exists or changed email.")

    if request.query_params.get("error"):
        logger.info("Google OAuth consent denied for %s: %s", employee.email, request.query_params.get("error"))
        return _frontend_redirect(calendar="denied", email=employee.email, reason=request.query_params.get("error"))

    code = request.query_params.get("code")
    if not code:
        return _frontend_redirect(calendar="error", email=employee.email, reason="Google did not return an authorization code.")

    try:
        from googleapiclient.discovery import build
        # 3. Exchange the code with the SAME verifier that produced the code_challenge.
        flow = google_oauth_flow(state, code_verifier=pkce["code_verifier"])
        flow.fetch_token(code=code, code_verifier=pkce["code_verifier"])
        credentials = flow.credentials
        account = build("oauth2", "v2", credentials=credentials, cache_discovery=False).userinfo().get().execute()
        authenticated_email = (account.get("email") or "").strip().lower()
    except Exception as error:
        details = google_error_detail(error)
        description = getattr(error, "description", None)
        oauth_code = getattr(error, "error", None)  # e.g. invalid_grant, redirect_uri_mismatch
        reason = f"{oauth_code or type(error).__name__}: {description or details['error_detail']}"
        logger.error("Google OAuth token exchange failed for %s: %s", employee.email, reason)
        audit_memory.record_event("API", "GOOGLE_OAUTH_FAILED", status="ERROR", error=reason,
                                  details={"employee_email": employee.email, **details})
        return _frontend_redirect(calendar="error", email=employee.email, reason=reason)

    # 4. The Google account must be the employee's own registered address (email is the identity).
    if authenticated_email != employee.email.strip().lower():
        logger.warning("Google account %s does not match employee %s; tokens not stored", authenticated_email, employee.email)
        audit_memory.record_event("API", "GOOGLE_OAUTH_ACCOUNT_MISMATCH", status="ERROR",
                                  details={"employee_email": employee.email, "google_account": authenticated_email})
        return _frontend_redirect(
            calendar="account_mismatch", email=employee.email, google_email=authenticated_email,
            reason=f"You signed in to Google as {authenticated_email}, but this employee is registered as {employee.email}.",
        )

    # 5. Persist tokens on the employee who started the flow.
    employee.google_tokens = credentials.to_json()
    employee.google_calendar_connected = True
    db.commit()
    db.refresh(employee)
    logger.info("Connected Google account for employee %s (%s); refresh token issued: %s",
                employee.name, employee.email, bool(credentials.refresh_token))
    audit_memory.record_event("API", "GOOGLE_OAUTH_CONNECTED", details={
        "employee_email": employee.email, "refresh_token_issued": bool(credentials.refresh_token),
    })
    return _frontend_redirect(calendar="connected", email=employee.email)

def _calendar_status_payload(employee: Employee, db: Session, verify: bool) -> Dict[str, Any]:
    error = None
    if verify and employee.google_calendar_connected:
        from backend.connectors.google_auth import load_google_credentials_with_error
        _, error = load_google_credentials_with_error(employee.email)
        db.refresh(employee)  # a revoked refresh token marks the employee disconnected
    connected = bool(employee.google_calendar_connected)
    return {
        "id": employee.id,
        "employee_id": employee.employee_id,
        "email": employee.email,
        "google_calendar_connected": connected,
        "status": "connected" if connected else "not_connected",
        "verified": verify,
        "error": error,
    }

@app.get("/api/employees/{id}/calendar/status")
def get_employee_calendar_status(id: int, verify: bool = False, db: Session = Depends(get_db)):
    employee = db.query(Employee).filter(Employee.id == id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    return _calendar_status_payload(employee, db, verify)

@app.get("/api/employees/by-email/{email}/calendar/status")
def get_employee_calendar_status_by_email(email: str, verify: bool = False, db: Session = Depends(get_db)):
    employee = db.query(Employee).filter(func.lower(Employee.email) == email.strip().lower()).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    return _calendar_status_payload(employee, db, verify)

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

# ----------------- MEETING WORKFLOW (LangGraph) -----------------

class OrchestrateRequestSchema(BaseModel):
    request: str = Field(..., description="Natural-language meeting scheduling command")
    session_id: Optional[str] = None
    skip_agenda: bool = False


def _workflow_http_error(error: WorkflowError) -> HTTPException:
    logger.error("Workflow failure: %s", error)
    return HTTPException(status_code=500, detail={
        "message": str(error), "node": error.node, "meeting_id": error.meeting_id,
    })


@app.post("/api/meetings/orchestrate")
def orchestrate_meeting(payload: OrchestrateRequestSchema):
    if not payload.request.strip():
        raise HTTPException(status_code=400, detail="Meeting request cannot be empty.")
    try:
        return agent_runtime.run_meeting_request(payload.request, payload.session_id, skip_agenda=payload.skip_agenda)
    except WorkflowError as e:
        raise _workflow_http_error(e)
    except Exception as e:
        logger.exception("Error orchestrating meeting")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


def _meeting_payload(m: Meeting, db: Session) -> Dict[str, Any]:
    data = m.to_dict()
    data["participants"] = [p.to_dict() for p in db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == m.id).all()]
    data["room_bookings"] = [rb.to_dict() for rb in db.query(RoomBooking).filter(RoomBooking.meeting_id == m.id).all()]
    workflow = (data.get("parsed_details") or {}).get("workflow") or {}
    data["meeting_id"] = m.id
    data["unknown_participants"] = workflow.get("unknown_participants", [])
    data["ambiguous_participants"] = workflow.get("ambiguous_participants", [])
    data["validation"] = (data.get("parsed_details") or {}).get("validation", {})
    data["rag_context"] = workflow.get("rag_context", {})
    data["execution"] = workflow.get("execution", {})
    data["execution_error"] = workflow.get("execution_error")
    data["warnings"] = workflow.get("warnings", [])
    data["workflow_trace"] = workflow.get("trace", [])
    return data


@app.get("/api/meetings")
def list_meetings(db: Session = Depends(get_db)):
    meetings = db.query(Meeting).order_by(Meeting.created_at.desc()).all()
    return [_meeting_payload(m, db) for m in meetings]

@app.get("/api/meetings/{id}")
def get_meeting(id: str, db: Session = Depends(get_db)):
    m = db.query(Meeting).filter(Meeting.id == id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return _meeting_payload(m, db)

# ----------------- HUMAN-IN-THE-LOOP APPROVAL GATE -----------------

class ApprovalGateSchema(BaseModel):
    action: str = Field(..., description="APPROVE, REJECT, or EDIT")
    notes: Optional[str] = ""
    edits: Optional[Dict[str, Any]] = None

EDITABLE_FIELDS = {"title", "scheduled_start", "scheduled_end", "duration_minutes", "mode", "room_name", "agenda", "purpose"}

@app.post("/api/meetings/{id}/approval")
def handle_human_approval(id: str, payload: ApprovalGateSchema, db: Session = Depends(get_db)):
    m = db.query(Meeting).filter(Meeting.id == id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Meeting not found")

    action = payload.action.upper()
    if action not in {"APPROVE", "REJECT", "EDIT"}:
        raise HTTPException(status_code=400, detail=f"Invalid approval action: {action}")
    if action == "APPROVE" and m.status != "WAITING_FOR_HUMAN_APPROVAL":
        raise HTTPException(status_code=409, detail=f"Meeting is not waiting for approval (status: {m.status}).")
    if action in {"EDIT", "REJECT"} and m.status not in {"WAITING_FOR_HUMAN_APPROVAL", "RESCHEDULING_REQUIRED", "DRAFT", "ACTION_FAILED"}:
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

        # Resume the LangGraph workflow at the approval gate (worker job shares this path).
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
            "execution": exec_result,
            "warnings": exec_result.get("warnings", []),
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

    # EDIT: apply the organizer's changes, then re-run availability/validation on the SAME meeting.
    m.approval_status = "EDITED"
    for k, v in (payload.edits or {}).items():
        if k in EDITABLE_FIELDS and v is not None:
            setattr(m, k, v.upper() if k == "mode" and isinstance(v, str) else v)
    db.commit()
    try:
        state = resume_meeting_workflow(id, "EDIT", slot_locked=True, agenda_locked=bool(m.agenda))
    except WorkflowError as e:
        raise _workflow_http_error(e)
    db.refresh(m)
    return {
        "success": True,
        "action": "EDITED",
        "meeting_id": id,
        "status": m.status,
        "meeting": _meeting_payload(m, db),
        "plan": workflow_view(state),
    }

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
    if m.status not in {"RESCHEDULING_REQUIRED", "WAITING_FOR_HUMAN_APPROVAL", "DRAFT"}:
        raise HTTPException(status_code=409, detail="Participants can only be changed before approval.")

    email = (payload.selected_email or payload.new_email or "").strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(status_code=400, detail="Must provide a valid participant email address.")

    workflow = (json.loads(m.parsed_details or "{}").get("workflow") or {})
    ambiguous = workflow.get("ambiguous_participants", [])
    unknown = workflow.get("unknown_participants", [])
    ambiguous_entry = next((a for a in ambiguous if a.get("queried_name") == payload.queried_name), None)
    if payload.selected_email and ambiguous_entry is not None:
        # The organizer must pick one of the registered candidates; nothing is chosen automatically.
        candidate_emails = {(c.get("email") or "").lower() for c in ambiguous_entry.get("candidates", [])}
        if email not in candidate_emails:
            raise HTTPException(status_code=400, detail="Selected email is not one of the matching registered employees.")

    employee = db.query(Employee).filter(func.lower(Employee.email) == email).first()
    name = employee.name if employee else (payload.new_name or payload.queried_name).strip()
    existing = db.query(MeetingParticipant).filter(
        MeetingParticipant.meeting_id == id, func.lower(MeetingParticipant.email) == email,
    ).first()
    if not existing:
        db.add(MeetingParticipant(
            meeting_id=id,
            employee_id=employee.employee_id if employee else None,
            name=name,
            email=employee.email if employee else email,
            is_external=employee is None,
            calendar_status="UNVERIFIED",
            response_status="PENDING",
        ))
    db.commit()

    audit_memory.record_event(
        agent="Meeting Coordinator",
        action="RESOLVE_PARTICIPANT",
        meeting_id=id,
        details={"queried_name": payload.queried_name, "name": name, "email": email, "registered": employee is not None}
    )

    remaining_unknown = [u for u in unknown if u.get("queried_name") != payload.queried_name]
    remaining_ambiguous = [a for a in ambiguous if a.get("queried_name") != payload.queried_name]
    try:
        # Re-plan the same meeting: still-unresolved names keep it blocked; otherwise availability + validation run.
        state = resume_meeting_workflow(
            id, "REPLAN", unknown_participants=remaining_unknown, ambiguous_participants=remaining_ambiguous,
        )
    except WorkflowError as e:
        raise _workflow_http_error(e)
    db.refresh(m)
    return {"success": True, "resolved": {"name": name, "email": email}, "status": m.status, "plan": workflow_view(state)}

class SetAgendaSchema(BaseModel):
    purpose: Optional[str] = None
    agenda: Optional[str] = None
    skip: bool = False

@app.post("/api/meetings/{id}/agenda")
def update_meeting_agenda(id: str, payload: SetAgendaSchema, db: Session = Depends(get_db)):
    m = db.query(Meeting).filter(Meeting.id == id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if m.status not in {"DRAFT", "WAITING_FOR_HUMAN_APPROVAL", "RESCHEDULING_REQUIRED"}:
        raise HTTPException(status_code=409, detail="The agenda can only be changed before approval.")

    agenda_locked = False
    if payload.skip:
        m.agenda = "[Agenda skipped by organizer request]"
        agenda_locked = True
    elif payload.agenda:
        m.agenda = payload.agenda
        if payload.purpose:
            m.purpose = payload.purpose
        agenda_locked = True
    elif payload.purpose:
        m.purpose = payload.purpose
        m.agenda = ""  # regenerated by the Agenda Agent in the workflow
    db.commit()

    try:
        resume_meeting_workflow(id, "EDIT", slot_locked=True, agenda_locked=agenda_locked, skip_agenda=payload.skip)
    except WorkflowError as e:
        raise _workflow_http_error(e)
    db.refresh(m)
    return {"success": True, "agenda": m.agenda, "purpose": m.purpose, "status": m.status}

# ----------------- ASYNC RESPONSES (simulated + real) -----------------

class SimulateParticipantResponseSchema(BaseModel):
    email: str
    response: str = Field(..., description="ACCEPTED or REJECTED")
    notes: Optional[str] = ""

@app.post("/api/meetings/{id}/simulate-participant")
def simulate_participant(id: str, payload: SimulateParticipantResponseSchema):
    """SIMULATED participant response (demo/testing). Recorded with source=SIMULATED, never as a real reply."""
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
    """SIMULATED auditorium response (demo/testing). Recorded with source=SIMULATED."""
    res = background_worker.simulate_room_approval(
        meeting_id=id,
        room_name=payload.room_name,
        confirmed=payload.confirmed,
        notes=payload.notes or ""
    )
    if res.get("status") == "ERROR":
        raise HTTPException(status_code=409, detail=res.get("message"))
    return res

@app.post("/api/meetings/{id}/sync-responses")
def sync_external_responses(id: str, db: Session = Depends(get_db)):
    """Check REAL responses now: Google Calendar attendee status and Gmail auditorium replies (via MCP)."""
    if not db.query(Meeting).filter(Meeting.id == id).first():
        raise HTTPException(status_code=404, detail="Meeting not found")
    results = background_worker.sync_external_responses(id)
    m = db.query(Meeting).filter(Meeting.id == id).first()
    db.refresh(m)
    return {"meeting_id": id, "status": m.status, "results": results}

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
    # External callers go through the approval gate for consequential capabilities.
    res = mcp_gateway.execute(method=req.method, params=req.params, req_id=req.id, auth_context={"external": True})
    return res.model_dump()
