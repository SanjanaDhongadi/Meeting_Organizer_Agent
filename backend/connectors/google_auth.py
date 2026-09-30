import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from backend.app.config import settings

logger = logging.getLogger("google_auth")


def save_oauth_pkce(state: str, employee_id: int, code_verifier: str) -> None:
    from backend.app.db import SessionLocal, OAuthPendingState

    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(minutes=10)
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
        if row.created_at and row.created_at < datetime.utcnow() - timedelta(minutes=10):
            db.delete(row)
            db.commit()
            return None
        return {"employee_id": row.employee_id, "code_verifier": row.code_verifier}
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


def load_google_credentials(email: Optional[str] = None):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
    from backend.app.db import SessionLocal, Employee

    db = SessionLocal()
    try:
        query = db.query(Employee).filter(Employee.google_calendar_connected.is_(True))
        owner_email = email or settings.GOOGLE_CALENDAR_OWNER_EMAIL
        if owner_email:
            employee = query.filter(Employee.email.ilike(owner_email)).first()
            if not employee and email:
                employee = db.query(Employee).filter(Employee.email.ilike(email)).first()
        else:
            employee = query.order_by(Employee.id.asc()).first()
        if not employee or not employee.google_tokens or employee.google_tokens == "{}":
            return None

        credentials = Credentials.from_authorized_user_info(json.loads(employee.google_tokens))
        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
                employee.google_tokens = credentials.to_json()
                employee.google_calendar_connected = True
                db.commit()
            except RefreshError as error:
                logger.warning("Google refresh token revoked or expired for %s: %s", employee.email, error)
                mark_calendar_disconnected(employee)
                db.commit()
                return None
        elif credentials.expired and not credentials.refresh_token:
            logger.warning("Google credentials expired with no refresh token for %s", employee.email)
            mark_calendar_disconnected(employee)
            db.commit()
            return None
        return credentials
    except Exception:
        logger.exception("Unable to load stored Google OAuth credentials")
        return None
    finally:
        db.close()