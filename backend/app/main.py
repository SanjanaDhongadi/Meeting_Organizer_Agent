import os
import json
import logging
import uuid
import re
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

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
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func
from sqlalchemy.orm import Session
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from backend.app.db import (
    get_db, User, Employee, Meeting, MeetingParticipant, RoomBooking, AuditLog, MeetingMinutes,
    compute_simple_embedding, cosine_similarity
)
from backend.app.auth import (
    hash_password, verify_password, create_access_token,
    get_current_user, get_current_user_optional
)
from backend.app.init_db import init_and_seed_db
from backend.runtime.runtime_loop import agent_runtime
from backend.connectors.mcp_gateway import mcp_gateway
from backend.connectors.calendar_service import get_calendar_service
from backend.tools.registry import tool_registry
from backend.skills.registry import skill_registry
from backend.memory.audit_memory import audit_memory
from backend.connectors.google_auth import consume_oauth_pkce, save_oauth_pkce, google_error_detail
from backend.connectors import groq_client
from backend.graph.workflow_graph import WorkflowError, resume_meeting_workflow, workflow_view, clarification_view
from worker.worker import background_worker
from backend.app.routes_minutes import router as minutes_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("api")
GOOGLE_SCOPES = settings.google_scopes
OAUTH_STATE_SALT = "google-calendar-oauth"

GOOGLE_LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

GOOGLE_CALENDAR_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/meetings.space.readonly",
]


def google_oauth_flow(
    state: Optional[str] = None,
    code_verifier: Optional[str] = None,
    scopes: Optional[List[str]] = None
):
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
        scopes=scopes or GOOGLE_CALENDAR_SCOPES,
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

app.include_router(minutes_router)

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
        "ai_provider": settings.AI_PROVIDER,
        "ai_configured": groq_client.is_configured(),
        "ai_model": settings.GROQ_LLM_MODEL,
        "transcription_model": settings.GROQ_TRANSCRIPTION_MODEL,
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
        # Typed times are read in this zone; "configured" is False when .env does not set it (UTC fallback).
        "meeting_timezone": settings.MEETING_TIMEZONE,
        "meeting_timezone_configured": "MEETING_TIMEZONE" in settings.model_fields_set,
        "connected_google_accounts": len(_connected_google_emails()),
        "workflow": "langgraph",
    }


def _connected_google_emails() -> List[str]:
    if settings.DEMO_MODE:
        return []
    from backend.connectors.google_auth import connected_emails
    return connected_emails()

# ----------------- EMPLOYEE DIRECTORY -----------------

def normalize_clock_time(value: str) -> str:
    """Accepts 'HH:MM', 'HH:MM:SS' or 'h:mm AM/PM' and stores 24-hour 'HH:MM:SS'."""
    text = str(value or "").strip().upper()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?", text)
    if not match:
        raise ValueError(f"Invalid time '{value}'. Use a time such as 09:00 or 9:00 AM.")
    hour, minute, meridiem = int(match.group(1)), int(match.group(2)), match.group(4)
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError(f"Invalid 12-hour time '{value}'.")
        hour = hour % 12 + (12 if meridiem == "PM" else 0)
    if hour > 23 or minute > 59:
        raise ValueError(f"Invalid time '{value}'.")
    return f"{hour:02d}:{minute:02d}:00"


def validate_timezone_name(value: str) -> str:
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(value)
    except Exception:
        raise ValueError(f"Unknown time zone '{value}'.")
    return value


class EmployeeCreateSchema(BaseModel):
    employee_id: str
    name: str
    email: str
    designation: str
    department: str
    working_days: str = "Monday,Tuesday,Wednesday,Thursday,Friday"
    working_hours_start: str = "09:00:00"
    working_hours_end: str = "17:00:00"
    # Not entered in the form: defaults to the organization's MEETING_TIMEZONE and is replaced by the
    # employee's own Google Calendar time zone when they connect Google.
    timezone: Optional[str] = None
    # No invented preferences/locations: empty unless the employee provides them.
    meeting_preferences: str = ""
    # Kept for API/DB compatibility; no longer shown in the profile form.
    preferred_duration: int = 30
    mode_preference: str = "Online"
    location: str = ""
    other_info: str = ""
    google_calendar_connected: bool = False

    @field_validator("working_hours_start", "working_hours_end")
    @classmethod
    def normalize_hours(cls, value: str) -> str:
        return normalize_clock_time(value)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: Optional[str]) -> Optional[str]:
        return validate_timezone_name(value) if value else value

    @model_validator(mode="after")
    def check_hours(self):
        if self.working_hours_start == self.working_hours_end:
            raise ValueError("Working hours start and end cannot be the same time.")
        return self

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
        timezone=payload.timezone or settings.MEETING_TIMEZONE,
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
    try:
        for key in ("working_hours_start", "working_hours_end"):
            if payload.get(key):
                payload[key] = normalize_clock_time(payload[key])
        if payload.get("timezone"):
            validate_timezone_name(payload["timezone"])
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    start = payload.get("working_hours_start", employee.working_hours_start)
    end = payload.get("working_hours_end", employee.working_hours_end)
    if start == end:
        raise HTTPException(status_code=422, detail="Working hours start and end cannot be the same time.")
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

# ----------------- USER AUTHENTICATION -----------------

class UserRegisterSchema(BaseModel):
    email: str
    password: str
    name: str

class UserLoginSchema(BaseModel):
    email: str
    password: str

class MarkAttendanceSchema(BaseModel):
    participant_id: Optional[int] = None
    email: Optional[str] = None
    attended: bool = True

@app.post("/api/auth/register")
def register_user(payload: UserRegisterSchema, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(status_code=400, detail="A valid email address is required.")
    if len(payload.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters long.")
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")

    existing = db.query(User).filter(func.lower(User.email) == email).first()
    if existing:
        if existing.password_hash:
            raise HTTPException(status_code=409, detail="An account with this email already exists.")
        else:
            existing.password_hash = hash_password(payload.password)
            if not existing.name:
                existing.name = payload.name.strip()
            db.commit()
            db.refresh(existing)
            token = create_access_token({"sub": str(existing.id), "email": existing.email})
            return {"token": token, "user": existing.to_dict()}

    user = User(
        email=email,
        name=payload.name.strip(),
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token({"sub": str(user.id), "email": user.email})
    audit_memory.record_event("Auth", "USER_REGISTER", details={"user_id": user.id, "email": user.email})
    return {"token": token, "user": user.to_dict()}


@app.post("/api/auth/login")
def login_user(payload: UserLoginSchema, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    user = db.query(User).filter(func.lower(User.email) == email).first()
    if not user or not user.password_hash or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    token = create_access_token({"sub": str(user.id), "email": user.email})
    audit_memory.record_event("Auth", "USER_LOGIN", details={"user_id": user.id, "email": user.email})
    return {"token": token, "user": user.to_dict()}


@app.get("/api/auth/me")
def get_current_user_profile(user: User = Depends(get_current_user)):
    return user.to_dict()


@app.get("/api/auth/google/login")
def google_auth_login():
    """Continue with Google login: requests only openid and profile/email."""
    if not settings.SECRET_KEY:
        raise HTTPException(status_code=503, detail="SECRET_KEY must be configured before OAuth can be used.")
    state = URLSafeTimedSerializer(settings.SECRET_KEY, salt=OAUTH_STATE_SALT).dumps({
        "flow": "login",
        "nonce": uuid.uuid4().hex,
    })
    flow = google_oauth_flow(state, scopes=GOOGLE_LOGIN_SCOPES)
    extra = {}
    if settings.GOOGLE_HOSTED_DOMAIN:
        extra["hd"] = settings.GOOGLE_HOSTED_DOMAIN
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="select_account",
        **extra,
    )
    if not flow.code_verifier:
        raise HTTPException(status_code=500, detail="OAuth PKCE code verifier was not created.")
    save_oauth_pkce(state, code_verifier=flow.code_verifier, flow_type="login")
    return {"authorization_url": authorization_url}


@app.get("/api/auth/google/connect")
def google_auth_connect(user: User = Depends(get_current_user)):
    """Connect Google Calendar/Gmail to the currently authenticated user."""
    if not settings.SECRET_KEY:
        raise HTTPException(status_code=503, detail="SECRET_KEY must be configured before OAuth can be used.")
    state = URLSafeTimedSerializer(settings.SECRET_KEY, salt=OAUTH_STATE_SALT).dumps({
        "flow": "calendar",
        "user_id": user.id,
        "email": user.email.strip().lower(),
        "nonce": uuid.uuid4().hex,
    })
    flow = google_oauth_flow(state, scopes=GOOGLE_CALENDAR_SCOPES)
    extra = {"login_hint": user.email}
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
    save_oauth_pkce(state, user_id=user.id, code_verifier=flow.code_verifier, flow_type="calendar")
    return {"authorization_url": authorization_url}


@app.post("/api/auth/google/disconnect")
def google_auth_disconnect(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    user_db = db.query(User).filter(User.id == user.id).first()
    if user_db:
        user_db.google_calendar_connected = False
        user_db.google_tokens = "{}"
        db.commit()
        db.refresh(user_db)
        audit_memory.record_event("Auth", "GOOGLE_DISCONNECTED", details={"user_id": user_db.id})
        return {"success": True, "user": user_db.to_dict()}
    return {"success": False, "error": "User not found"}

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
        state_data = URLSafeTimedSerializer(settings.SECRET_KEY, salt=OAUTH_STATE_SALT).loads(
            state, max_age=settings.GOOGLE_OAUTH_STATE_TTL_SECONDS
        )
    except SignatureExpired:
        logger.warning("Google OAuth callback rejected: state expired")
        return _frontend_redirect(auth_error="The Google sign-in link expired. Start again.")
    except BadSignature:
        logger.warning("Google OAuth callback rejected: state signature invalid")
        raise HTTPException(status_code=400, detail="Google OAuth state is invalid.")

    pkce = consume_oauth_pkce(state)
    if not pkce or not pkce.get("code_verifier"):
        logger.warning("Google OAuth callback rejected: no pending PKCE verifier for this state")
        return _frontend_redirect(auth_error="This Google sign-in was already used or has expired. Start again.")

    flow_type = state_data.get("flow") or pkce.get("flow_type") or "calendar"

    if request.query_params.get("error"):
        err = request.query_params.get("error")
        logger.info("Google OAuth consent denied: %s", err)
        return _frontend_redirect(auth_error=f"Google sign-in was denied: {err}")

    code = request.query_params.get("code")
    if not code:
        return _frontend_redirect(auth_error="Google did not return an authorization code.")

    try:
        from googleapiclient.discovery import build
        flow = google_oauth_flow(
            state,
            code_verifier=pkce["code_verifier"],
            scopes=GOOGLE_LOGIN_SCOPES if flow_type == "login" else GOOGLE_CALENDAR_SCOPES
        )
        flow.fetch_token(code=code, code_verifier=pkce["code_verifier"])
        credentials = flow.credentials
        userinfo = build("oauth2", "v2", credentials=credentials, cache_discovery=False).userinfo().get().execute()
        authenticated_email = (userinfo.get("email") or "").strip().lower()
        google_id = userinfo.get("id")
        user_name = userinfo.get("name") or authenticated_email.split("@")[0]
    except Exception as error:
        details = google_error_detail(error)
        description = getattr(error, "description", None)
        oauth_code = getattr(error, "error", None)
        reason = f"{oauth_code or type(error).__name__}: {description or details.get('error_detail') or error}"
        logger.error("Google OAuth token exchange failed: %s", reason)
        audit_memory.record_event("API", "GOOGLE_OAUTH_FAILED", status="ERROR", error=reason, details=details)
        return _frontend_redirect(auth_error=f"Google token exchange failed: {reason}")

    # Case 1: Continue with Google (Login flow)
    if flow_type == "login":
        user = db.query(User).filter(
            (func.lower(User.email) == authenticated_email) | (User.google_id == google_id)
        ).first()
        if not user:
            user = User(
                email=authenticated_email,
                name=user_name,
                google_id=google_id,
                google_calendar_connected=False,
                google_tokens="{}"
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            if not user.google_id:
                user.google_id = google_id
                db.commit()
        token = create_access_token({"sub": str(user.id), "email": user.email})
        audit_memory.record_event("Auth", "GOOGLE_LOGIN_SUCCESS", details={"user_id": user.id, "email": user.email})
        return RedirectResponse(f"{settings.FRONTEND_URL.rstrip('/')}/#auth_token={token}&email={user.email}")

    # Case 2: Connect Google Calendar/Gmail to existing User
    if flow_type == "calendar" and (state_data.get("user_id") or pkce.get("user_id")):
        user_id = state_data.get("user_id") or pkce.get("user_id")
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return _frontend_redirect(auth_error="User account not found.")
        user.google_tokens = credentials.to_json()
        user.google_calendar_connected = True
        if not user.google_id and google_id:
            user.google_id = google_id
        db.commit()
        db.refresh(user)
        audit_memory.record_event("Auth", "GOOGLE_CALENDAR_CONNECTED", details={"user_id": user.id, "email": user.email})
        return RedirectResponse(f"{settings.FRONTEND_URL.rstrip('/')}/#calendar_connected=true")

    # Case 3: Legacy employee flow (for backwards compatibility)
    if pkce.get("employee_id"):
        employee = db.query(Employee).filter(Employee.id == pkce["employee_id"]).first()
        if employee:
            employee.google_tokens = credentials.to_json()
            employee.google_calendar_connected = True
            db.commit()
            return _frontend_redirect(calendar="connected", email=employee.email)

    return _frontend_redirect(calendar="connected")

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
def orchestrate_meeting(
    payload: OrchestrateRequestSchema,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db)
):
    if not payload.request.strip():
        raise HTTPException(status_code=400, detail="Meeting request cannot be empty.")
    try:
        organizer_id = current_user.id if current_user else None
        organizer_email = current_user.email if current_user else None
        res = agent_runtime.run_meeting_request(
            payload.request,
            payload.session_id,
            skip_agenda=payload.skip_agenda,
            organizer_id=organizer_id,
            organizer_email=organizer_email
        )
        meeting_id = res.get("id") or res.get("meeting_id")
        if meeting_id and organizer_id:
            m = db.query(Meeting).filter(Meeting.id == meeting_id).first()
            if m:
                m.organizer_id = organizer_id
                db.commit()
                db.refresh(m)
                res["organizer_id"] = organizer_id
        return res
    except WorkflowError as e:
        raise _workflow_http_error(e)
    except Exception as e:
        logger.exception("Error orchestrating meeting")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


def _meeting_payload(m: Meeting, db: Session) -> Dict[str, Any]:
    data = m.to_dict()
    data["conflicts"] = [c for c in data.get("conflicts", []) if not _obsolete_venue_verification_conflict(c)]
    data["participants"] = [p.to_dict() for p in db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == m.id).all()]
    data["room_bookings"] = [rb.to_dict() for rb in db.query(RoomBooking).filter(RoomBooking.meeting_id == m.id).order_by(RoomBooking.id.asc()).all()]
    workflow = (data.get("parsed_details") or {}).get("workflow") or {}
    data["meeting_id"] = m.id
    data["unknown_participants"] = workflow.get("unknown_participants", [])
    data["ambiguous_participants"] = workflow.get("ambiguous_participants", [])
    data["validation"] = (data.get("parsed_details") or {}).get("validation", {})
    data["rag_context"] = workflow.get("rag_context", {})
    data["organizer_availability"] = workflow.get("organizer_availability")
    data["execution"] = workflow.get("execution", {})
    data["execution_error"] = workflow.get("execution_error")
    data["warnings"] = workflow.get("warnings", [])
    data["workflow_trace"] = workflow.get("trace", [])
    data.update(clarification_view(data.get("parsed_details") or {}))
    data["venue_coordinator_email"] = (data.get("parsed_details") or {}).get("venue_coordinator_email", "")
    if m.status in APPROVABLE_STATUSES:
        # Lets the review screen say up front that approval cannot send anything yet (no network call).
        data["execution_readiness"] = _execution_readiness(m, db, verify=False)
    if data["execution_error"] and m.status == "ACTION_FAILED":
        # When the failure happened, so an old failure is not mistaken for a new one.
        data["execution_error"] = {**data["execution_error"], "failed_at": m.updated_at.isoformat() + "Z" if m.updated_at else None}
    data["google_sync"] = (data.get("parsed_details") or {}).get("google_sync")
    minutes = db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == m.id).first()
    if minutes:
        data["minutes"] = {"status": minutes.status, "last_error": minutes.last_error or "",
                           "next_attempt_at": minutes.next_attempt_at.isoformat() + "Z" if minutes.next_attempt_at else None}
    if (m.mode or "").upper() == "OFFLINE":
        from backend.agents.venue_coordinator import venue_view
        data["venue_coordination"] = venue_view(data, workflow, workflow.get("organizer_email"))
    return data


def _obsolete_venue_verification_conflict(conflict: Dict[str, Any]) -> bool:
    details = conflict.get("conflicting_values") or {}
    return details.get("field") == "venue_availability" and conflict.get("status") == "FLAGGED_FOR_HUMAN"


# A plan can be approved at the gate, or approved again after its execution failed (e.g. Google was not connected).
APPROVABLE_STATUSES = {"WAITING_FOR_HUMAN_APPROVAL", "ACTION_FAILED"}


def _execution_readiness(m: Meeting, db: Session, verify: bool) -> Dict[str, Any]:
    """What approval needs: in person, a venue that is not busy (re-checked on approval).
    Google Calendar connection enables external sync; if not connected, meeting is scheduled internally."""
    workflow = (json.loads(m.parsed_details or "{}").get("workflow") or {})
    venue_check = workflow.get("venue_check") or {}
    if (m.mode or "").upper() == "OFFLINE" and verify and m.room_name and m.scheduled_start and m.scheduled_end:
        from backend.agents.venue_coordinator import check_venue_availability
        venue_check = check_venue_availability(m.room_name, m.scheduled_start, m.scheduled_end, m.id)
    if (m.mode or "").upper() == "OFFLINE" and venue_check.get("status") == "BUSY":
        return {"ready": False, "error_code": "VENUE_NOT_AVAILABLE", "venue_check": venue_check,
                "error": f"Venue not available: {venue_check.get('reason')} Change the time or the venue before approving."}
    if settings.DEMO_MODE:
        return {"ready": True, "demo_mode": True, "google_connected": True}
    from backend.connectors.google_auth import organizer_status
    attendees = [p.email for p in db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == m.id).all() if p.email]
    organizer_hint = None
    if m.organizer_id:
        from backend.app.db import User
        user = db.query(User).filter(User.id == m.organizer_id).first()
        if user:
            organizer_hint = user.email
    st = organizer_status(attendees, organizer_hint=organizer_hint, verify=verify)
    if not st.get("ready"):
        return {"ready": True, "google_connected": False, "note": st.get("error")}
    return {**st, "google_connected": True}


@app.get("/api/meetings")
def list_meetings(
    section: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    # If no user logged in, return all in DEMO_MODE or empty
    if not current_user:
        if settings.DEMO_MODE:
            meetings = db.query(Meeting).filter(Meeting.status != "DELETED").order_by(Meeting.created_at.desc()).all()
            return [_meeting_payload(m, db) for m in meetings]
        return []

    user_email = current_user.email.strip().lower()

    # 1. Organized Meetings: created by current_user
    organized_query = db.query(Meeting).filter(
        Meeting.status != "DELETED",
        Meeting.organizer_id == current_user.id
    ).order_by(Meeting.created_at.desc())

    # 2. Meetings where user is a participant
    participating_meeting_ids = [
        p.meeting_id for p in db.query(MeetingParticipant.meeting_id).filter(
            func.lower(MeetingParticipant.email) == user_email
        ).all()
    ]

    # Past Meetings: user participated AND attended or attendance recorded
    past_participating_ids = [
        p.meeting_id for p in db.query(MeetingParticipant.meeting_id).filter(
            func.lower(MeetingParticipant.email) == user_email,
            (MeetingParticipant.attended.is_(True) | MeetingParticipant.attendance_recorded.is_(True))
        ).all()
    ]

    if section == "organized":
        return [_meeting_payload(m, db) for m in organized_query.all()]

    if section == "past":
        if not past_participating_ids:
            return []
        meetings = db.query(Meeting).filter(
            Meeting.status != "DELETED",
            Meeting.id.in_(past_participating_ids)
        ).order_by(Meeting.created_at.desc()).all()
        return [_meeting_payload(m, db) for m in meetings]

    if section == "requests":
        if not participating_meeting_ids:
            return []
        meetings = db.query(Meeting).filter(
            Meeting.status != "DELETED",
            Meeting.id.in_(participating_meeting_ids),
            (Meeting.organizer_id != current_user.id) | (Meeting.organizer_id.is_(None))
        ).order_by(Meeting.created_at.desc()).all()
        return [_meeting_payload(m, db) for m in meetings]

    if section == "sections":
        organized = [_meeting_payload(m, db) for m in organized_query.all()]
        past = [_meeting_payload(m, db) for m in db.query(Meeting).filter(
            Meeting.status != "DELETED",
            Meeting.id.in_(past_participating_ids)
        ).order_by(Meeting.created_at.desc()).all()] if past_participating_ids else []
        requests = [_meeting_payload(m, db) for m in db.query(Meeting).filter(
            Meeting.status != "DELETED",
            Meeting.id.in_(participating_meeting_ids),
            (Meeting.organizer_id != current_user.id) | (Meeting.organizer_id.is_(None))
        ).order_by(Meeting.created_at.desc()).all()] if participating_meeting_ids else []
        return {
            "organized": organized,
            "past": past,
            "requests": requests
        }

    # Default: user is organizer OR invited participant
    accessible_ids = list(set([m.id for m in organized_query.all()] + participating_meeting_ids))
    if not accessible_ids:
        return []
    meetings = db.query(Meeting).filter(
        Meeting.status != "DELETED",
        Meeting.id.in_(accessible_ids)
    ).order_by(Meeting.created_at.desc()).all()
    return [_meeting_payload(m, db) for m in meetings]

@app.get("/api/meetings/{id}")
def get_meeting(
    id: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    m = db.query(Meeting).filter(Meeting.id == id).first()
    if not m or m.status == "DELETED":
        raise HTTPException(status_code=404, detail="Meeting not found")

    if current_user and m.organizer_id is not None:
        user_email = current_user.email.strip().lower()
        if m.organizer_id != current_user.id:
            is_participant = db.query(MeetingParticipant).filter(
                MeetingParticipant.meeting_id == id,
                func.lower(MeetingParticipant.email) == user_email
            ).first() is not None
            if not is_participant:
                raise HTTPException(status_code=403, detail="Access denied: You do not have permission to view this meeting.")

    return _meeting_payload(m, db)

@app.post("/api/meetings/{id}/attendance")
def record_meeting_attendance(
    id: str,
    payload: MarkAttendanceSchema,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    m = db.query(Meeting).filter(Meeting.id == id).first()
    if not m or m.status == "DELETED":
        raise HTTPException(status_code=404, detail="Meeting not found")

    participant = None
    if payload.participant_id:
        participant = db.query(MeetingParticipant).filter(
            MeetingParticipant.id == payload.participant_id,
            MeetingParticipant.meeting_id == id
        ).first()
    elif payload.email:
        participant = db.query(MeetingParticipant).filter(
            func.lower(MeetingParticipant.email) == payload.email.strip().lower(),
            MeetingParticipant.meeting_id == id
        ).first()
    else:
        participant = db.query(MeetingParticipant).filter(
            func.lower(MeetingParticipant.email) == current_user.email.strip().lower(),
            MeetingParticipant.meeting_id == id
        ).first()

    if not participant:
        part_email = (payload.email or current_user.email).strip().lower()
        participant = MeetingParticipant(
            meeting_id=id,
            email=part_email,
            name=payload.email or current_user.name or part_email,
            attendance_recorded=True,
            attended=bool(payload.attended)
        )
        db.add(participant)
    else:
        participant.attendance_recorded = True
        participant.attended = bool(payload.attended)
    db.commit()
    db.refresh(participant)
    audit_memory.record_event("Human Operator", "RECORD_ATTENDANCE", meeting_id=id,
                              details={"participant_email": participant.email, "attended": participant.attended})
    return {"success": True, "participant": participant.to_dict()}


def _cancel_external_actions(m: Meeting, db: Session, reason: str) -> Dict[str, Any]:
    """Cancel what was already sent for this meeting: the Google Calendar event (Google notifies attendees)
    and any pending/confirmed venue request. Returns {"ok", "error", "done": [...]}."""
    from backend.connectors.mcp_gateway import mcp_call
    workflow = (json.loads(m.parsed_details or "{}").get("workflow") or {})
    organizer = workflow.get("organizer_email")
    done = []
    if m.calendar_event_id:
        res = mcp_call("calendar.cancel_event", {"event_id": m.calendar_event_id, "organizer_email": organizer})
        if not res.get("success"):
            return {"ok": False, "error": f"Google Calendar event could not be cancelled: {res.get('error')}", "details": res}
        done.append("calendar_event_cancelled" + (" (already removed in Google)" if res.get("already_removed") else ""))
    from backend.agents.venue_coordinator import request_subject, valid_coordinator_email
    meeting_email = ((json.loads(m.parsed_details or "{}")).get("venue_coordinator_email") or "").strip()
    for rb in db.query(RoomBooking).filter(RoomBooking.meeting_id == m.id, RoomBooking.status.in_(["PENDING", "CONFIRMED"])).all():
        # The venue coordinator received a request for this meeting: tell them it is cancelled.
        if not valid_coordinator_email(meeting_email):
            return {"ok": False, "error": "A venue request was sent, but this meeting has no valid venue coordinator email for the cancellation.", "done": done}
        res = mcp_call("gmail.send", {
            "to_emails": [meeting_email],
            "subject": "Cancelled: " + request_subject(m.id, m.title),
            "body": f"The venue request for {rb.room_name} ({m.scheduled_start} - {m.scheduled_end}) is cancelled. Reason: {reason}",
            "meeting_id": m.id, "sender_email": organizer,
        })
        if not res.get("success"):
            return {"ok": False, "error": f"Venue cancellation e-mail could not be sent: {res.get('error')}", "details": res, "done": done}
        rb.status = "CANCELLED"
        rb.notes = f"{rb.notes}\nCancellation sent to {meeting_email}.".strip()
        done.append(f"venue_request_cancelled:{rb.room_name}")
    return {"ok": True, "done": done}


@app.delete("/api/meetings/{id}")
def delete_meeting(
    id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    """
    Delete one meeting. Anything already sent is cancelled first (Google event with attendee notifications,
    venue request). The record is soft-deleted (status DELETED, hidden) so the audit history stays intact.
    """
    m = db.query(Meeting).filter(Meeting.id == id).first()
    if not m or m.status == "DELETED":
        raise HTTPException(status_code=404, detail="Meeting not found")
    if current_user and m.organizer_id is not None and m.organizer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied: Only the meeting organizer can delete this meeting.")
    if m.status in {"APPROVED", "EXECUTING"}:
        raise HTTPException(status_code=409, detail="Approved actions are being executed right now; try again in a moment.")
    result = _cancel_external_actions(m, db, reason="Meeting deleted by the organizer.")
    if not result["ok"]:
        db.rollback()
        raise HTTPException(status_code=502, detail=result["error"])
    previous = m.status
    m.status = "DELETED"
    m.approval_notes = f"{m.approval_notes or ''}\nDeleted by organizer (was {previous}). {'; '.join(result['done'])}".strip()
    db.commit()
    audit_memory.record_event("Human Operator", "MEETING_DELETED", meeting_id=id,
                              details={"previous_status": previous, "external_actions": result["done"]})
    return {"success": True, "meeting_id": id, "previous_status": previous, "cancelled": result["done"]}

# ----------------- VENUE COORDINATOR (in-person meetings) -----------------

class VenueRequestSchema(BaseModel):
    to: str
    subject: str
    body: str

VENUE_REQUEST_BLOCKED_STATUSES = {"DELETED", "REJECTED", "CONFIRMED", "APPROVED", "EXECUTING"}


@app.post("/api/meetings/{id}/venue-request")
def send_venue_request(
    id: str,
    payload: VenueRequestSchema,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Send the venue booking request e-mail, exactly as reviewed/edited by the organizer, ONLY when they click Send.
    It never sends attendee invitations and never marks the venue as confirmed: the coordinator's reply does that.
    """
    from backend.agents.venue_coordinator import active_request, ensure_reference, valid_coordinator_email
    from backend.connectors.mcp_gateway import mcp_call
    m = db.query(Meeting).filter(Meeting.id == id).with_for_update().first()
    if not m or m.status == "DELETED":
        raise HTTPException(status_code=404, detail="Meeting not found")
    if current_user and m.organizer_id is not None and m.organizer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied: Only the meeting organizer can send its venue request.")
    if (m.mode or "").upper() != "OFFLINE":
        raise HTTPException(status_code=400, detail="Venue requests are only for in-person (offline) meetings.")
    if not m.room_name:
        raise HTTPException(status_code=400, detail="This meeting has no venue yet. Add a venue before requesting it.")
    if m.status in VENUE_REQUEST_BLOCKED_STATUSES:
        raise HTTPException(status_code=409, detail=f"A venue request cannot be sent while the meeting is {m.status}.")
    parsed = json.loads(m.parsed_details or "{}")
    to = (parsed.get("venue_coordinator_email") or "").strip()
    if not valid_coordinator_email(to):
        raise HTTPException(status_code=422, detail="Enter a valid venue coordinator email in meeting details before sending.")
    if m.approval_status != "APPROVED":
        raise HTTPException(status_code=409, detail="Approve the meeting before manually sending its venue booking request.")
    if (payload.to or "").strip().lower() != to.lower():
        raise HTTPException(status_code=422, detail="The recipient must match this meeting's saved venue coordinator email.")
    subject, body = (payload.subject or "").strip(), (payload.body or "").strip()
    if not subject or not body:
        raise HTTPException(status_code=422, detail="Subject and message cannot be empty.")
    rooms = [rb.to_dict() for rb in db.query(RoomBooking).filter(RoomBooking.meeting_id == id).order_by(RoomBooking.id.asc()).all()]
    current = active_request(rooms)
    if current and current.get("status") in {"SENDING", "PENDING", "CONFIRMED"}:
        raise HTTPException(status_code=409, detail="A venue request for this meeting is already "
                            + ({"SENDING": "being sent. Check its status before retrying.",
                                "PENDING": "waiting for a reply.", "CONFIRMED": "confirmed."}[current["status"]]))

    sender = None
    if not settings.DEMO_MODE:
        from backend.connectors.google_auth import organizer_status
        attendees = [p.email for p in db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == id).all() if p.email]
        status = organizer_status(attendees, verify=True)
        if not status["ready"]:
            raise HTTPException(status_code=409, detail=status["error"])
        sender = status["organizer_email"]
    # The meeting reference must stay in the subject so the coordinator's reply can be matched to this meeting.
    subject = ensure_reference(subject, id)
    # Persist a send claim before calling Gmail so concurrent or repeated clicks cannot duplicate the message.
    claim = RoomBooking(meeting_id=id, room_name=m.room_name, capacity=None, status="SENDING",
                        notes=f"Venue email send initiated to {to} on {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC. Subject: {subject}")
    db.add(claim)
    db.commit()
    res = mcp_call("gmail.send", {"to_emails": [to], "subject": subject, "body": body, "meeting_id": id, "sender_email": sender})
    if not res.get("success"):
        uncertain = res.get("error_code") == "NETWORK_UNREACHABLE" or bool(res.get("mcp_error"))
        claim.status = "SENDING" if uncertain else "FAILED"
        claim.notes = (f"Could not confirm whether the venue email was sent to {to}; do not retry until checked. "
                       if uncertain else f"Venue email was not sent to {to}. ") + (res.get("error") or "Email provider reported failure.")
        db.commit()
        audit_memory.record_event("Human Operator", "SEND_VENUE_REQUEST", meeting_id=id, status="ERROR",
                                  error=res.get("error") or "", details={"to": to, "subject": subject})
        message = ("The venue email sending result could not be confirmed; it is held to prevent duplicates. "
                   if uncertain else "The venue request was NOT sent: ")
        raise HTTPException(status_code=502, detail=message + (res.get("error") or "Email provider reported failure."))
    # Recorded as a request waiting for the coordinator's answer (never as booked).
    claim.status = "PENDING"
    claim.notes = (f"Request sent to {to} on {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC "
                   f"(message {res.get('message_id') or 'n/a'}, provider {res.get('provider') or 'n/a'}). Subject: {subject}")
    db.commit()
    audit_memory.record_event("Human Operator", "SEND_VENUE_REQUEST", meeting_id=id, details={
        "to": to, "subject": subject, "message_id": res.get("message_id"), "provider": res.get("provider"), "sender": sender})
    return {"success": True, "meeting_id": id, "to": to, "subject": subject, "message_id": res.get("message_id"),
            "provider": res.get("provider"), "meeting": _meeting_payload(m, db)}


# ----------------- HUMAN-IN-THE-LOOP APPROVAL GATE -----------------

class ApprovalGateSchema(BaseModel):
    action: str = Field(..., description="APPROVE, REJECT, or EDIT")
    notes: Optional[str] = ""
    edits: Optional[Dict[str, Any]] = None

EDITABLE_FIELDS = {"title", "scheduled_start", "scheduled_end", "duration_minutes", "mode", "room_name", "agenda", "purpose"}


def _supersede_venue_request(m: Meeting, db: Session, before) -> None:
    """A venue request is for one venue at one time: after the time/venue changes, or after it was declined, it no
    longer applies and the organizer sends a new one from the Venue Coordinator tab."""
    after = (m.scheduled_start, m.scheduled_end, (m.room_name or "").strip().lower(), (m.mode or "").upper())
    for rb in db.query(RoomBooking).filter(RoomBooking.meeting_id == m.id,
                                           RoomBooking.status.in_(["PENDING", "CONFIRMED", "REJECTED"])).all():
        if after != before or rb.status == "REJECTED":
            rb.status = "SUPERSEDED"
            rb.notes = f"{rb.notes}\nSuperseded by an edit of the meeting ({datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC)."
            audit_memory.record_event("Human Operator", "VENUE_REQUEST_SUPERSEDED", meeting_id=m.id,
                                      details={"booking_id": rb.id, "venue": rb.room_name})


def _store_agenda_points(m: Meeting, points: List[str]) -> None:
    """The organizer's agenda points are the single source; the agenda text is just their numbered form."""
    from backend.skills.agenda_skill import normalize_agenda_points, format_agenda
    parsed = json.loads(m.parsed_details or "{}")
    from sqlalchemy.orm import object_session
    from backend.app.action_items import compose_agenda
    parsed["agenda_points"] = normalize_agenda_points(points)
    m.parsed_details = json.dumps(parsed)
    # Follow-up tasks the organizer selected are appended after the organizer's own agenda, never replacing it.
    m.agenda = compose_agenda(object_session(m), m, format_agenda(parsed["agenda_points"]))

@app.post("/api/meetings/{id}/approval")
def handle_human_approval(
    id: str,
    payload: ApprovalGateSchema,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    m = db.query(Meeting).filter(Meeting.id == id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Meeting not found")

    if current_user and m.organizer_id and m.organizer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied: Only the meeting organizer can approve or edit this meeting.")

    action = payload.action.upper()
    if action not in {"APPROVE", "REJECT", "EDIT"}:
        raise HTTPException(status_code=400, detail=f"Invalid approval action: {action}")
    if action == "APPROVE" and m.status not in APPROVABLE_STATUSES:
        raise HTTPException(status_code=409, detail=f"Meeting is not waiting for approval (status: {m.status}).")
    if action == "APPROVE":
        # Check what sending needs BEFORE approving, so a missing Google connection leaves the plan at the gate
        # (with a clear message) instead of failing halfway through execution.
        readiness = _execution_readiness(m, db, verify=True)
        if not readiness["ready"]:
            audit_memory.record_event("Human Operator", "HUMAN_APPROVE_BLOCKED", meeting_id=id, status="ERROR",
                                      error=readiness["error"], details={"error_code": readiness.get("error_code")})
            raise HTTPException(status_code=409, detail=readiness["error"])
    if action in {"EDIT", "REJECT"} and m.status not in {"WAITING_FOR_HUMAN_APPROVAL", "RESCHEDULING_REQUIRED", "DRAFT", "ACTION_FAILED", "NEEDS_INFORMATION"}:
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
        # A meeting that already has a Google event (e.g. after an attendee declined) must not leave it behind.
        cancel = _cancel_external_actions(m, db, reason=payload.notes or "Rejected by the organizer.")
        if not cancel["ok"]:
            db.rollback()
            raise HTTPException(status_code=502, detail=cancel["error"])
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
    from backend.skills.agenda_skill import agenda_points_from_text
    before = (m.scheduled_start, m.scheduled_end, (m.room_name or "").strip().lower(), (m.mode or "").upper())
    m.approval_status = "EDITED"
    edits = dict(payload.edits or {})
    points = edits.pop("agenda_points", None)
    if points is None and edits.get("agenda") is not None:
        points = agenda_points_from_text(edits["agenda"])
    edits.pop("agenda", None)
    for key in ("scheduled_start", "scheduled_end"):
        if edits.get(key):
            try:
                edits[key] = datetime.fromisoformat(str(edits[key]).replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                raise HTTPException(status_code=422, detail=f"Invalid {key}: {edits[key]}")
    for k, v in edits.items():
        if k in EDITABLE_FIELDS and v is not None:
            setattr(m, k, v.upper() if k == "mode" and isinstance(v, str) else v)
    if points is not None:
        _store_agenda_points(m, points)
    _supersede_venue_request(m, db, before)
    db.commit()
    try:
        state = resume_meeting_workflow(id, "EDIT", slot_locked=True)
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
    if m.status not in {"RESCHEDULING_REQUIRED", "WAITING_FOR_HUMAN_APPROVAL", "DRAFT", "NEEDS_INFORMATION"}:
        raise HTTPException(status_code=409, detail="Participants can only be changed before approval.")

    if payload.new_email and not payload.selected_email:
        raise HTTPException(status_code=400, detail=(
            "Attendees must be registered employees. Register the employee in the Employees view, then re-check."))
    email = (payload.selected_email or "").strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(status_code=400, detail="Select one of the matching registered employees.")

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
    if not employee:
        raise HTTPException(status_code=400, detail=f"{email} is not a registered employee.")
    name = employee.name
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

class ClarifySchema(BaseModel):
    """Answers to the agent's clarifying questions. Only provided fields change; nothing else is touched."""
    title: Optional[str] = None
    purpose: Optional[str] = None
    date: Optional[str] = Field(None, description="YYYY-MM-DD (in MEETING_TIMEZONE)")
    start_time: Optional[str] = Field(None, description="HH:MM 24h (in MEETING_TIMEZONE)")
    duration_minutes: Optional[int] = None
    mode: Optional[str] = Field(None, description="ONLINE or OFFLINE")
    venue: Optional[str] = None
    venue_coordinator_email: Optional[str] = None
    attendees: Optional[List[str]] = Field(None, description="Names/emails of attendees to add")
    participant_emails: Optional[Dict[str, str]] = Field(None, description="Map of participant names to email addresses")
    agenda_points: Optional[List[str]] = None

@app.post("/api/meetings/{id}/clarify")
def clarify_meeting(id: str, payload: ClarifySchema, db: Session = Depends(get_db)):
    """Merge the organizer's answers into the SAME meeting and re-run the workflow from participant/context checks."""
    from backend.graph.workflow_graph import coordinator
    from backend.agents.participant_agent import normalize_participant

    m = db.query(Meeting).filter(Meeting.id == id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if m.status not in {"NEEDS_INFORMATION", "DRAFT", "WAITING_FOR_HUMAN_APPROVAL", "RESCHEDULING_REQUIRED"}:
        raise HTTPException(status_code=409, detail="Details can only be changed before approval.")

    parsed = json.loads(m.parsed_details or "{}")
    ambiguities = parsed.setdefault("ambiguities", {})
    errors = []
    if payload.title is not None and payload.title.strip():
        parsed["title"] = payload.title.strip()
    if payload.purpose is not None and payload.purpose.strip():
        parsed["purpose"] = payload.purpose.strip()
    if payload.date:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", payload.date):
            errors.append("Date must be YYYY-MM-DD.")
        else:
            parsed["date"] = payload.date
            ambiguities.pop("date", None)
    if payload.start_time:
        try:
            parsed["start_clock"] = normalize_clock_time(payload.start_time)[:5]
            parsed["end_clock"] = None
            ambiguities.pop("start_time", None)
        except ValueError as error:
            errors.append(str(error))
    if payload.duration_minutes is not None:
        if not 5 <= payload.duration_minutes <= 24 * 60:
            errors.append("Duration must be between 5 minutes and 24 hours.")
        else:
            parsed["duration_minutes"] = payload.duration_minutes
            parsed["end_clock"] = None
    if payload.mode:
        mode = payload.mode.strip().upper()
        if mode not in {"ONLINE", "OFFLINE"}:
            errors.append("Mode must be ONLINE or OFFLINE.")
        else:
            parsed["mode"] = mode
            ambiguities.pop("mode", None)
    if payload.venue is not None and payload.venue.strip():
        parsed["room_name"] = payload.venue.strip()
    if payload.venue_coordinator_email is not None:
        coordinator_email = payload.venue_coordinator_email.strip().lower()
        if (parsed.get("mode") or "").upper() == "OFFLINE":
            from backend.agents.venue_coordinator import valid_coordinator_email
            if not valid_coordinator_email(coordinator_email):
                errors.append("Please enter a valid venue coordinator email address.")
            else:
                parsed["venue_coordinator_email"] = coordinator_email
    if payload.agenda_points is not None:
        from backend.skills.agenda_skill import normalize_agenda_points
        parsed["agenda_points"] = normalize_agenda_points(payload.agenda_points)

    if payload.attendees:
        existing = [normalize_participant(p) for p in parsed.get("participants", [])]
        for a in payload.attendees:
            if a and a.strip():
                norm = normalize_participant(a.strip())
                if not any(ep["name"].lower() == norm["name"].lower() or (norm["email"] and ep["email"].lower() == norm["email"].lower()) for ep in existing):
                    existing.append(norm)
        parsed["participants"] = existing

    if payload.participant_emails:
        email_regex = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
        matched_names = set()
        updated = []
        source_participants = []
        for p in parsed.get("participants", []):
            if isinstance(p, str) and "@" not in p:
                source_participants.extend(part.strip() for part in re.split(r",|\band\b|&", p, flags=re.IGNORECASE) if part.strip())
            else:
                source_participants.append(p)
        for p in source_participants:
            norm = normalize_participant(p)
            p_name = norm["name"]
            email = norm["email"]
            if p_name in payload.participant_emails:
                val = str(payload.participant_emails[p_name] or "").strip()
                matched_names.add(p_name)
                if val:
                    if not email_regex.match(val):
                        errors.append(f"Please enter a valid email address for {p_name}.")
                    else:
                        email = val
            else:
                for k, v in payload.participant_emails.items():
                    if k.lower() == p_name.lower():
                        val = str(v or "").strip()
                        matched_names.add(k)
                        if val:
                            if not email_regex.match(val):
                                errors.append(f"Please enter a valid email address for {p_name}.")
                            else:
                                email = val
                        break
            norm["email"] = email
            updated.append(norm)

        for k, v in payload.participant_emails.items():
            if k not in matched_names and not any(k.lower() == p["name"].lower() for p in updated):
                val = str(v or "").strip()
                if val:
                    if not email_regex.match(val):
                        errors.append(f"Please enter a valid email address for {k}.")
                    else:
                        updated.append(normalize_participant({"name": k, "email": val}))

        parsed["participants"] = updated

    parsed["details_confirmed"] = True  # the organizer has seen the details/agenda step
    if errors:
        raise HTTPException(status_code=422, detail=" ".join(errors))

    # If the organizer changes the time, the previously stored slot no longer applies.
    if payload.date or payload.start_time or payload.duration_minutes is not None:
        m.scheduled_start = ""
        m.scheduled_end = ""
    parsed = coordinator.complete_details(parsed)
    m.title = parsed.get("title") or ""
    m.purpose = parsed.get("purpose") or ""
    m.mode = parsed.get("mode") or ""
    m.room_name = parsed.get("room_name") or ""
    m.duration_minutes = parsed.get("duration_minutes") or 0
    m.scheduled_start = parsed.get("start_time") or ""
    m.scheduled_end = parsed.get("end_time") or ""
    m.parsed_details = json.dumps(parsed)
    db.commit()
    from backend.tools.registry import tool_registry
    tool_registry.get("save_meeting_state").execute(meeting_id=id, participants=parsed.get("participants", []))
    audit_memory.record_event("Human Operator", "CLARIFY_DETAILS", meeting_id=id,
                              details={k: v for k, v in payload.model_dump().items() if v not in (None, "", [])})

    try:
        state = resume_meeting_workflow(id, "REPLAN")
    except WorkflowError as e:
        raise _workflow_http_error(e)
    db.refresh(m)
    return workflow_view(state)

class UnresolvedAttendeeSchema(BaseModel):
    queried_name: str

def _unresolved_lists(m: Meeting):
    workflow = (json.loads(m.parsed_details or "{}").get("workflow") or {})
    return workflow.get("unknown_participants", []), workflow.get("ambiguous_participants", [])

@app.post("/api/meetings/{id}/recheck-participant")
def recheck_participant(id: str, payload: UnresolvedAttendeeSchema, db: Session = Depends(get_db)):
    """After the organizer registers the employee, look the name up again in the directory (same meeting)."""
    m = db.query(Meeting).filter(Meeting.id == id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if m.status not in {"NEEDS_INFORMATION", "RESCHEDULING_REQUIRED", "WAITING_FOR_HUMAN_APPROVAL", "DRAFT"}:
        raise HTTPException(status_code=409, detail="Participants can only be changed before approval.")
    unknown, ambiguous = _unresolved_lists(m)
    if not any(u.get("queried_name") == payload.queried_name for u in unknown):
        raise HTTPException(status_code=404, detail=f"'{payload.queried_name}' is not an unresolved attendee of this meeting.")
    try:
        state = resume_meeting_workflow(
            id, "REPLAN", pending_participant_names=[payload.queried_name],
            unknown_participants=[u for u in unknown if u.get("queried_name") != payload.queried_name],
            ambiguous_participants=ambiguous,
        )
    except WorkflowError as e:
        raise _workflow_http_error(e)
    return workflow_view(state)

@app.post("/api/meetings/{id}/remove-participant")
def remove_unresolved_participant(id: str, payload: UnresolvedAttendeeSchema, db: Session = Depends(get_db)):
    """Drop an unregistered/ambiguous attendee from the request and re-plan the same meeting without them."""
    m = db.query(Meeting).filter(Meeting.id == id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if m.status not in {"NEEDS_INFORMATION", "RESCHEDULING_REQUIRED", "WAITING_FOR_HUMAN_APPROVAL", "DRAFT"}:
        raise HTTPException(status_code=409, detail="Participants can only be changed before approval.")
    unknown, ambiguous = _unresolved_lists(m)
    parsed = json.loads(m.parsed_details or "{}")
    parsed["participants"] = [p for p in parsed.get("participants", []) if p != payload.queried_name]
    from backend.graph.workflow_graph import coordinator
    m.parsed_details = json.dumps(coordinator.complete_details(parsed))
    db.commit()
    audit_memory.record_event("Human Operator", "REMOVE_UNRESOLVED_ATTENDEE", meeting_id=id, details={"queried_name": payload.queried_name})
    try:
        state = resume_meeting_workflow(
            id, "REPLAN",
            unknown_participants=[u for u in unknown if u.get("queried_name") != payload.queried_name],
            ambiguous_participants=[a for a in ambiguous if a.get("queried_name") != payload.queried_name],
        )
    except WorkflowError as e:
        raise _workflow_http_error(e)
    return workflow_view(state)

class SetAgendaSchema(BaseModel):
    purpose: Optional[str] = None
    agenda: Optional[str] = None
    skip: bool = False

@app.post("/api/meetings/{id}/agenda")
def update_meeting_agenda(id: str, payload: SetAgendaSchema, db: Session = Depends(get_db)):
    m = db.query(Meeting).filter(Meeting.id == id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if m.status not in {"DRAFT", "WAITING_FOR_HUMAN_APPROVAL", "RESCHEDULING_REQUIRED", "NEEDS_INFORMATION"}:
        raise HTTPException(status_code=409, detail="The agenda can only be changed before approval.")

    from backend.skills.agenda_skill import agenda_points_from_text
    if payload.purpose:
        m.purpose = payload.purpose
    if payload.skip:
        _store_agenda_points(m, [])          # no agenda (optional) — nothing is generated
    elif payload.agenda is not None:
        _store_agenda_points(m, agenda_points_from_text(payload.agenda))
    db.commit()

    try:
        resume_meeting_workflow(id, "EDIT", slot_locked=True)
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
    if not settings.DEMO_MODE:
        raise HTTPException(status_code=403, detail="Simulated responses are only available in DEMO_MODE. "
                                                    "Real responses are read from Google with Refresh.")
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
    if not settings.DEMO_MODE:
        raise HTTPException(status_code=403, detail="Simulated responses are only available in DEMO_MODE. "
                                                    "Real responses are read from Google with Refresh.")
    res = background_worker.simulate_room_approval(
        meeting_id=id,
        room_name=payload.room_name,
        confirmed=payload.confirmed,
        notes=payload.notes or ""
    )
    if res.get("status") == "ERROR":
        raise HTTPException(status_code=409, detail=res.get("message"))
    return res

@app.post("/api/meetings/sync-responses")
def sync_all_external_responses():
    """Refresh: read REAL responses (Google Calendar RSVPs / Gmail auditorium replies) for every waiting meeting."""
    results = background_worker.sync_external_responses(None)
    errors = [r for r in results if r.get("status") == "ERROR"]
    return {"results": results, "errors": errors, "synced_at": datetime.utcnow().isoformat() + "Z"}

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
def get_mcp_capabilities(user: User = Depends(get_current_user)):
    return mcp_gateway.list_capabilities()

@app.post("/api/mcp")
def execute_mcp(req: MCPRequestSchema, user: User = Depends(get_current_user)):
    # Authenticated callers only; the gateway scopes them to their own Google account and meetings.
    res = mcp_gateway.execute(method=req.method, params=req.params, req_id=req.id,
                              auth_context={"external": True, "user_id": user.id, "user_email": user.email.lower()})
    return res.model_dump()
