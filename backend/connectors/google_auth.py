import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings

logger = logging.getLogger("google_auth")


def _state_ttl() -> timedelta:
    return timedelta(seconds=settings.GOOGLE_OAUTH_STATE_TTL_SECONDS)


# Fail fast when Google cannot be reached instead of waiting minutes per call (library defaults are 60-120 s).
GOOGLE_HTTP_TIMEOUT_SECONDS = 20
try:
    import googleapiclient.http as _gapi_http
    _gapi_http.DEFAULT_HTTP_TIMEOUT_SEC = GOOGLE_HTTP_TIMEOUT_SECONDS
except ImportError:  # pragma: no cover
    pass


def _token_request():
    """google-auth transport for token refresh, with the same short timeout."""
    from google.auth.transport.requests import Request

    class _TimeoutRequest(Request):
        def __call__(self, *args, **kwargs):
            kwargs.setdefault("timeout", GOOGLE_HTTP_TIMEOUT_SECONDS)
            return super().__call__(*args, **kwargs)
    return _TimeoutRequest()


NETWORK_ERROR_HINTS = ("unable to find the server", "10060", "10061", "10065", "timed out", "name or service not known",
                       "getaddrinfo", "failed to establish a new connection", "connection refused", "network is unreachable",
                       "temporary failure in name resolution", "max retries exceeded")


def is_network_error(error: Exception) -> bool:
    """True when Google could not be reached at all (no internet / DNS / timeout), as opposed to an API error."""
    import socket
    if hasattr(error, "resp") and hasattr(error, "content"):
        return False  # Google answered with an HTTP error
    try:
        import httplib2
        if isinstance(error, httplib2.ServerNotFoundError):
            return True
    except ImportError:
        pass
    try:
        from google.auth.exceptions import TransportError
        if isinstance(error, TransportError):
            return True
    except ImportError:
        pass
    if isinstance(error, (socket.timeout, TimeoutError, ConnectionError)):
        return True
    return any(hint in str(error).lower() for hint in NETWORK_ERROR_HINTS)


NETWORK_ERROR_MESSAGE = ("Could not reach Google — check the internet connection on the computer running the backend. "
                         "Nothing was sent; try again once the connection is back.")


def google_error_detail(error: Exception) -> Dict[str, Any]:
    """Extract HTTP status / Google reason / Google message from a googleapiclient HttpError."""
    if is_network_error(error):
        return {"error_detail": f"{NETWORK_ERROR_MESSAGE} ({error})", "http_status": None, "error_reason": "network_unreachable",
                "error_message": NETWORK_ERROR_MESSAGE, "network_error": True}
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

def save_oauth_pkce(
    state: str,
    employee_id: Optional[int] = None,
    code_verifier: str = "",
    user_id: Optional[int] = None,
    flow_type: str = "calendar"
) -> None:
    from app.db import SessionLocal, OAuthPendingState

    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - _state_ttl()
        db.query(OAuthPendingState).filter(OAuthPendingState.created_at < cutoff).delete()
        existing = db.query(OAuthPendingState).filter(OAuthPendingState.state == state).first()
        if existing:
            existing.employee_id = employee_id
            existing.user_id = user_id
            existing.flow_type = flow_type
            existing.code_verifier = code_verifier
            existing.created_at = datetime.utcnow()
        else:
            db.add(OAuthPendingState(
                state=state,
                employee_id=employee_id,
                user_id=user_id,
                flow_type=flow_type,
                code_verifier=code_verifier
            ))
        db.commit()
    finally:
        db.close()


def load_oauth_pkce(state: str) -> Optional[dict]:
    from app.db import SessionLocal, OAuthPendingState

    db = SessionLocal()
    try:
        row = db.query(OAuthPendingState).filter(OAuthPendingState.state == state).first()
        if not row:
            return None
        if row.created_at and row.created_at < datetime.utcnow() - _state_ttl():
            db.delete(row)
            db.commit()
            return None
        return {
            "employee_id": row.employee_id,
            "user_id": row.user_id,
            "flow_type": row.flow_type or "calendar",
            "code_verifier": row.code_verifier
        }
    finally:
        db.close()


def consume_oauth_pkce(state: str) -> Optional[dict]:
    """Load and delete the pending PKCE row in one step so a state/verifier can only be used once."""
    from app.db import SessionLocal, OAuthPendingState

    db = SessionLocal()
    try:
        row = db.query(OAuthPendingState).filter(OAuthPendingState.state == state).first()
        if not row:
            return None
        data = {
            "employee_id": row.employee_id,
            "user_id": row.user_id,
            "flow_type": row.flow_type or "calendar",
            "code_verifier": row.code_verifier,
            "created_at": row.created_at
        }
        db.delete(row)
        db.commit()
        if data["created_at"] and data["created_at"] < datetime.utcnow() - _state_ttl():
            return None
        return data
    finally:
        db.close()


def delete_oauth_pkce(state: str) -> None:
    from app.db import SessionLocal, OAuthPendingState

    db = SessionLocal()
    try:
        db.query(OAuthPendingState).filter(OAuthPendingState.state == state).delete()
        db.commit()
    finally:
        db.close()


def mark_calendar_disconnected(employee) -> None:
    employee.google_calendar_connected = False
    employee.google_tokens = "{}"


def _audit_auto_disconnect(email: str, error_code: str, reason: str) -> None:
    """Google refused the stored token, so the account was disconnected: record why (no token data)."""
    try:
        from memory.audit_memory import audit_memory
        audit_memory.record_event("Google Auth", "GOOGLE_ACCOUNT_DISCONNECTED", status="ERROR", error=reason,
                                  details={"employee_email": email, "error_code": error_code})
    except Exception:
        logger.exception("Could not record the disconnection of %s in the audit log", email)


# ----------------- Stored credential loading / refresh -----------------

def load_google_credentials_with_error(email: Optional[str] = None) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """
    Returns (credentials, error). `email` selects the user or employee whose tokens are used; without it the
    configured GOOGLE_CALENDAR_OWNER_EMAIL is used. Expired access tokens are refreshed and persisted.
    Revoked/expired refresh tokens mark the account as disconnected and return a clear error.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
    from app.db import SessionLocal, User, Employee

    db = SessionLocal()
    try:
        target_email = (email or settings.GOOGLE_CALENDAR_OWNER_EMAIL or "").strip().lower()
        if not target_email:
            return None, {
                "error_code": "NO_ACCOUNT_SELECTED",
                "error": "No Google account selected. Connect your Google account in Settings or Dashboard.",
            }
        
        # Check User table first, then Employee
        account = db.query(User).filter(User.email.ilike(target_email)).first()
        is_user = True
        if not account:
            account = db.query(Employee).filter(Employee.email.ilike(target_email)).first()
            is_user = False

        if not account:
            return None, {
                "error_code": "NOT_REGISTERED",
                "error": f"{target_email} is not a registered user, so no Google authorization exists for it.",
            }
        if not account.google_calendar_connected or not account.google_tokens or account.google_tokens == "{}":
            return None, {
                "error_code": "NO_CREDENTIALS",
                "error": f"Google account for {account.email} is not connected. Connect it from the Dashboard.",
            }

        credentials = Credentials.from_authorized_user_info(json.loads(account.google_tokens))
        if credentials.expired or not credentials.valid:
            if not credentials.refresh_token:
                logger.warning("Google credentials expired with no refresh token for %s", account.email)
                account.google_calendar_connected = False
                account.google_tokens = "{}"
                db.commit()
                _audit_auto_disconnect(account.email, "TOKEN_EXPIRED_NO_REFRESH", "Access token expired and no refresh token was stored.")
                return None, {
                    "error_code": "TOKEN_EXPIRED_NO_REFRESH",
                    "error": f"Google access for {account.email} expired and no refresh token was issued. Reconnect the account.",
                }
            try:
                credentials.refresh(_token_request())
                account.google_tokens = credentials.to_json()
                account.google_calendar_connected = True
                db.commit()
                logger.info("Refreshed Google access token for %s", account.email)
            except RefreshError as error:
                logger.warning("Google refresh token revoked or expired for %s: %s", account.email, error)
                account.google_calendar_connected = False
                account.google_tokens = "{}"
                db.commit()
                _audit_auto_disconnect(account.email, "REFRESH_TOKEN_REVOKED", f"Google refused the refresh token: {error}")
                return None, {
                    "error_code": "REFRESH_TOKEN_REVOKED",
                    "error": (
                        f"Google refresh token for {account.email} was revoked or has expired ({error}). "
                        "The account was marked as disconnected; reconnect it from the Dashboard."
                    ),
                }
        return credentials, None
    except Exception as error:
        if is_network_error(error):
            logger.warning("Google unreachable while refreshing credentials for %s: %s", email, error)
            return None, {"error_code": "NETWORK_UNREACHABLE", "error": NETWORK_ERROR_MESSAGE}
        logger.exception("Unable to load stored Google OAuth credentials for %s", email)
        return None, {"error_code": "CREDENTIAL_LOAD_FAILED", "error": f"Stored Google credentials could not be loaded: {error}"}
    finally:
        db.close()


def load_google_credentials(email: Optional[str] = None):
    credentials, _ = load_google_credentials_with_error(email)
    return credentials


def connected_emails() -> List[str]:
    from app.db import SessionLocal, User, Employee

    db = SessionLocal()
    try:
        user_rows = db.query(User).filter(User.google_calendar_connected.is_(True)).order_by(User.id.asc()).all()
        emp_rows = db.query(Employee).filter(Employee.google_calendar_connected.is_(True)).order_by(Employee.id.asc()).all()
        emails = [u.email.lower() for u in user_rows if u.google_tokens and u.google_tokens != "{}"]
        for r in emp_rows:
            if r.google_tokens and r.google_tokens != "{}" and r.email.lower() not in emails:
                emails.append(r.email.lower())
        return emails
    finally:
        db.close()


def resolve_organizer_email(participant_emails: Optional[List[str]] = None, organizer_hint: Optional[str] = None) -> Optional[str]:
    """
    Pick the Google account that organizes the event and sends mail, in this order:
    1. organizer_hint (the authenticated user creating or owning the meeting), if connected
    2. GOOGLE_CALENDAR_OWNER_EMAIL (configuration), if connected
    3. the first meeting participant who has connected Google
    4. any connected account
    """
    connected = connected_emails()
    if organizer_hint and organizer_hint.strip().lower() in connected:
        return organizer_hint.strip().lower()
    owner = (settings.GOOGLE_CALENDAR_OWNER_EMAIL or "").strip().lower()
    if owner and owner in connected:
        return owner
    for email in participant_emails or []:
        if email and email.lower() in connected:
            return email.lower()
    return connected[0] if connected else None


NO_ORGANIZER_MESSAGE = (
    "No connected Google account is available to send the calendar invitation and emails. "
    "Connect your Google Account in the Dashboard or Settings to enable Google synchronization."
)


def organizer_status(participant_emails: Optional[List[str]] = None, organizer_hint: Optional[str] = None, verify: bool = False) -> Dict[str, Any]:
    """
    Whether a Google account is available to organize the event and send mail (live mode).
    verify=False only checks the stored connection (no network). verify=True also loads/refreshes the
    credentials; an account whose refresh token was revoked is marked disconnected and the next one is tried.
    """
    tried = set()
    while True:
        organizer = resolve_organizer_email(participant_emails, organizer_hint=organizer_hint)
        if not organizer:
            return {"ready": False, "organizer_email": None, "error_code": "NO_CREDENTIALS", "error": NO_ORGANIZER_MESSAGE}
        if not verify:
            return {"ready": True, "organizer_email": organizer}
        _, error = load_google_credentials_with_error(organizer)
        if not error:
            return {"ready": True, "organizer_email": organizer}
        if error.get("error_code") not in {"TOKEN_EXPIRED_NO_REFRESH", "REFRESH_TOKEN_REVOKED"} or organizer in tried:
            return {"ready": False, "organizer_email": organizer, **error}
        tried.add(organizer)
