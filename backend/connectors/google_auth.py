import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from backend.app.config import settings

logger = logging.getLogger("google_auth")


def _state_ttl() -> timedelta:
    return timedelta(seconds=settings.GOOGLE_OAUTH_STATE_TTL_SECONDS)


def google_error_detail(error: Exception) -> Dict[str, Any]:
    """Extract HTTP status / Google reason / Google message from a googleapiclient HttpError."""
    error_detail = str(error)
    http_status = None
    error_reason = None
    error_message = None
    try:
        if hasattr(error, "resp") and hasattr(error, "content"):
            http_status = error.resp.status
            raw_content = error.content.decode("utf-8") if isinstance(error.content, bytes) else str(error.content)
            content = json.loads(raw_content)
            api_error = content.get("error", {})
            if isinstance(api_error, dict):
                errors = api_error.get("errors") or []
                error_reason = errors[0].get("reason", "") if errors else api_error.get("status", "")
                error_message = api_error.get("message", "")
            else:
                # OAuth token endpoint style: {"error": "invalid_grant", "error_description": "..."}
                error_reason = str(api_error)
                error_message = content.get("error_description", "")
            error_detail = (
                f"HTTP {http_status}: {error_message}"
                + (f" (reason: {error_reason})" if error_reason else "")
            )
    except Exception:
        pass
    return {
        "error_detail": error_detail,
        "http_status": http_status,
        "error_reason": error_reason,
        "error_message": error_message,
    }


# ----------------- PKCE / OAuth state storage (server side, short-lived, single use) -----------------

def save_oauth_pkce(state: str, employee_id: int, code_verifier: str) -> None:
    from backend.app.db import SessionLocal, OAuthPendingState

    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - _state_ttl()
        db.query(OAuthPendingState).filter(OAuthPendingState.created_at < cutoff).delete()
        existing = db.query(OAuthPendingState).filter(OAuthPendingState.state == state).first()
        if existing:
            existing.employee_id = employee_id
            existing.code_verifier = code_verifier
            existing.created_at = datetime.utcnow()
        else:
            db.add(OAuthPendingState(state=state, employee_id=employee_id, code_verifier=code_verifier))
        db.commit()
    finally:
        db.close()


def load_oauth_pkce(state: str) -> Optional[dict]:
    from backend.app.db import SessionLocal, OAuthPendingState

    db = SessionLocal()
    try:
        row = db.query(OAuthPendingState).filter(OAuthPendingState.state == state).first()
        if not row:
            return None
        if row.created_at and row.created_at < datetime.utcnow() - _state_ttl():
            db.delete(row)
            db.commit()
            return None
        return {"employee_id": row.employee_id, "code_verifier": row.code_verifier}
    finally:
        db.close()


def consume_oauth_pkce(state: str) -> Optional[dict]:
    """Load and delete the pending PKCE row in one step so a state/verifier can only be used once."""
    from backend.app.db import SessionLocal, OAuthPendingState

    db = SessionLocal()
    try:
        row = db.query(OAuthPendingState).filter(OAuthPendingState.state == state).first()
        if not row:
            return None
        data = {"employee_id": row.employee_id, "code_verifier": row.code_verifier, "created_at": row.created_at}
        db.delete(row)
        db.commit()
        if data["created_at"] and data["created_at"] < datetime.utcnow() - _state_ttl():
            return None
        return data
    finally:
        db.close()


def delete_oauth_pkce(state: str) -> None:
    from backend.app.db import SessionLocal, OAuthPendingState

    db = SessionLocal()
    try:
        db.query(OAuthPendingState).filter(OAuthPendingState.state == state).delete()
        db.commit()
    finally:
        db.close()


def mark_calendar_disconnected(employee) -> None:
    employee.google_calendar_connected = False
    employee.google_tokens = "{}"


# ----------------- Stored credential loading / refresh -----------------

def load_google_credentials_with_error(email: Optional[str] = None) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """
    Returns (credentials, error). `email` selects the employee whose tokens are used; without it the
    configured GOOGLE_CALENDAR_OWNER_EMAIL is used. Expired access tokens are refreshed and persisted.
    Revoked/expired refresh tokens mark the employee as disconnected and return a clear error.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
    from backend.app.db import SessionLocal, Employee

    db = SessionLocal()
    try:
        target_email = (email or settings.GOOGLE_CALENDAR_OWNER_EMAIL or "").strip().lower()
        if not target_email:
            return None, {
                "error_code": "NO_ACCOUNT_SELECTED",
                "error": "No Google account selected. Set GOOGLE_CALENDAR_OWNER_EMAIL or connect a participant's Google account.",
            }
        employee = db.query(Employee).filter(Employee.email.ilike(target_email)).first()
        if not employee:
            return None, {
                "error_code": "NOT_REGISTERED",
                "error": f"{target_email} is not a registered employee, so no Google authorization exists for it.",
            }
        if not employee.google_calendar_connected or not employee.google_tokens or employee.google_tokens == "{}":
            return None, {
                "error_code": "NO_CREDENTIALS",
                "error": f"Google account for {employee.email} is not connected. Connect it from the Employees view.",
            }

        credentials = Credentials.from_authorized_user_info(json.loads(employee.google_tokens))
        if credentials.expired or not credentials.valid:
            if not credentials.refresh_token:
                logger.warning("Google credentials expired with no refresh token for %s", employee.email)
                mark_calendar_disconnected(employee)
                db.commit()
                return None, {
                    "error_code": "TOKEN_EXPIRED_NO_REFRESH",
                    "error": f"Google access for {employee.email} expired and no refresh token was issued. Reconnect the account.",
                }
            try:
                credentials.refresh(Request())
                employee.google_tokens = credentials.to_json()
                employee.google_calendar_connected = True
                db.commit()
                logger.info("Refreshed Google access token for %s", employee.email)
            except RefreshError as error:
                logger.warning("Google refresh token revoked or expired for %s: %s", employee.email, error)
                mark_calendar_disconnected(employee)
                db.commit()
                return None, {
                    "error_code": "REFRESH_TOKEN_REVOKED",
                    "error": (
                        f"Google refresh token for {employee.email} was revoked or has expired ({error}). "
                        "The account was marked as disconnected; reconnect it from the Employees view."
                    ),
                }
        return credentials, None
    except Exception as error:
        logger.exception("Unable to load stored Google OAuth credentials for %s", email)
        return None, {"error_code": "CREDENTIAL_LOAD_FAILED", "error": f"Stored Google credentials could not be loaded: {error}"}
    finally:
        db.close()


def load_google_credentials(email: Optional[str] = None):
    credentials, _ = load_google_credentials_with_error(email)
    return credentials


def connected_emails() -> List[str]:
    from backend.app.db import SessionLocal, Employee

    db = SessionLocal()
    try:
        rows = db.query(Employee).filter(Employee.google_calendar_connected.is_(True)).order_by(Employee.id.asc()).all()
        return [r.email.lower() for r in rows if r.google_tokens and r.google_tokens != "{}"]
    finally:
        db.close()


def resolve_organizer_email(participant_emails: Optional[List[str]] = None) -> Optional[str]:
    """
    Pick the Google account that organizes the event and sends mail, in this order:
    1. GOOGLE_CALENDAR_OWNER_EMAIL (configuration), if that employee is connected
    2. the first meeting participant who has connected Google
    3. any connected employee (the previous default behaviour)
    """
    connected = connected_emails()
    owner = (settings.GOOGLE_CALENDAR_OWNER_EMAIL or "").strip().lower()
    if owner and owner in connected:
        return owner
    for email in participant_emails or []:
        if email and email.lower() in connected:
            return email.lower()
    return connected[0] if connected else None
