from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
import logging
import uuid
from backend.app.config import settings

logger = logging.getLogger("email_service")

def _google_error_detail(error: Exception) -> Dict[str, Any]:
    error_detail = str(error)
    http_status = None
    error_reason = None
    error_message = None
    try:
        if hasattr(error, "resp") and hasattr(error, "content"):
            import json as _json
            http_status = error.resp.status
            raw_content = error.content.decode("utf-8") if isinstance(error.content, bytes) else str(error.content)
            content = _json.loads(raw_content)
            api_error = content.get("error", {})
            error_reason = api_error.get("errors", [{}])[0].get("reason", "") if api_error.get("errors") else ""
            error_message = api_error.get("message", "")
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

class EmailService(ABC):
    @abstractmethod
    def send_invitation(self, to_emails: List[str], subject: str, body: str, meeting_id: str) -> Dict[str, Any]:
        """Send meeting invitation or status email to participants."""
        pass

    @abstractmethod
    def get_messages(self, query: str = "") -> List[Dict[str, Any]]:
        """Fetch incoming responses or confirmation emails."""
        pass

class MockEmailService(EmailService):
    def __init__(self):
        self.outbox: List[Dict[str, Any]] = []
        self.simulated_inbox: List[Dict[str, Any]] = []

    def send_invitation(self, to_emails: List[str], subject: str, body: str, meeting_id: str) -> Dict[str, Any]:
        msg_id = f"mock-msg-{uuid.uuid4().hex[:10]}"
        record = {
            "id": msg_id,
            "to": to_emails,
            "subject": subject,
            "body": body,
            "meeting_id": meeting_id,
            "status": "SENT",
            "provider": "MockEmailService"
        }
        self.outbox.append(record)
        logger.info(f"[MockEmail] Sent message {msg_id} to {to_emails} for meeting {meeting_id}")
        return {
            "success": True,
            "message_id": msg_id,
            "recipients": to_emails,
            "subject": subject,
            "provider": "MockEmailService"
        }

    def get_messages(self, query: str = "") -> List[Dict[str, Any]]:
        return self.simulated_inbox

class GmailService(EmailService):
    def __init__(self, credentials: Optional[Any] = None):
        self.credentials = credentials
        self._service = None

    def _get_service(self):
        if not self._service:
            from googleapiclient.discovery import build
            self._service = build("gmail", "v1", credentials=self.credentials)
        return self._service

    def send_invitation(self, to_emails: List[str], subject: str, body: str, meeting_id: str) -> Dict[str, Any]:
        from backend.connectors.google_auth import load_google_credentials
        credentials = self.credentials or load_google_credentials()
        if not credentials:
            return {
                "success": False,
                "error": "Gmail OAuth is not connected. Connect Google Calendar/Gmail from the Employee Dashboard.",
                "error_code": "NO_CREDENTIALS",
            }
        try:
            import base64
            from email.mime.text import MIMEText
            from googleapiclient.discovery import build

            message = MIMEText(body)
            message["to"] = ", ".join(to_emails)
            message["subject"] = subject
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

            service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
            sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
            return {
                "success": True,
                "message_id": sent.get("id"),
                "recipients": to_emails,
                "subject": subject,
                "provider": "GmailService"
            }
        except Exception as e:
            details = _google_error_detail(e)
            logger.error("GmailService error: %s", details["error_detail"])
            return {
                "success": False,
                "error": f"Gmail API error: {details['error_detail']}",
                "error_code": "GOOGLE_API_FAILURE",
                "http_status": details["http_status"],
                "error_reason": details["error_reason"],
                "error_message": details["error_message"],
            }

    def get_messages(self, query: str = "") -> List[Dict[str, Any]]:
        try:
            service = self._get_service()
            res = service.users().messages().list(userId="me", q=query).execute()
            return res.get("messages", [])
        except Exception as e:
            logger.error(f"Gmail get_messages error: {e}")
            return []

_mock_email_instance = MockEmailService()

def get_email_service() -> EmailService:
    if settings.DEMO_MODE:
        return _mock_email_instance
    from backend.connectors.google_auth import load_google_credentials
    return GmailService(load_google_credentials())
