from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
import logging
import uuid
from backend.app.config import settings
from backend.connectors.google_auth import google_error_detail

logger = logging.getLogger("email_service")

def _google_error_detail(error: Exception) -> Dict[str, Any]:
    # Kept for backwards compatibility; the shared helper lives in google_auth.
    return google_error_detail(error)

class EmailService(ABC):
    @abstractmethod
    def send_invitation(self, to_emails: List[str], subject: str, body: str, meeting_id: str,
                        sender_email: Optional[str] = None) -> Dict[str, Any]:
        """Send meeting invitation or status email to participants."""
        pass

    @abstractmethod
    def get_messages(self, query: str = "", account_email: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch incoming responses or confirmation emails."""
        pass

class MockEmailService(EmailService):
    """Offline/test adapter (DEMO_MODE=true only). Nothing is delivered; messages are kept in memory."""
    def __init__(self):
        self.outbox: List[Dict[str, Any]] = []
        self.simulated_inbox: List[Dict[str, Any]] = []

    def send_invitation(self, to_emails: List[str], subject: str, body: str, meeting_id: str,
                        sender_email: Optional[str] = None) -> Dict[str, Any]:
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

    def get_messages(self, query: str = "", account_email: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.simulated_inbox

class GmailService(EmailService):
    """Real Gmail adapter: sends/reads through the authenticated Google account of a registered employee."""
    def __init__(self, credentials: Optional[Any] = None):
        self.credentials = credentials

    def _credentials_for(self, email: Optional[str]):
        from backend.connectors.google_auth import load_google_credentials_with_error, resolve_organizer_email
        if self.credentials:
            return self.credentials, None, email
        account = email or resolve_organizer_email()
        if not account:
            return None, {
                "error_code": "NO_CREDENTIALS",
                "error": "Gmail is not connected. Connect the organizer's Google account from the Employees view.",
            }, None
        credentials, error = load_google_credentials_with_error(account)
        return credentials, error, account

    def send_invitation(self, to_emails: List[str], subject: str, body: str, meeting_id: str,
                        sender_email: Optional[str] = None) -> Dict[str, Any]:
        credentials, cred_error, account = self._credentials_for(sender_email)
        if not credentials:
            return {"success": False, "error": cred_error["error"], "error_code": cred_error["error_code"], "sender_email": account}
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
                "sender_email": account,
                "provider": "GmailService"
            }
        except Exception as e:
            details = google_error_detail(e)
            logger.error("GmailService send failed — HTTP %s | reason: %s | message: %s",
                         details["http_status"], details["error_reason"], details["error_message"] or details["error_detail"])
            return {
                "success": False,
                "error": f"Gmail API error: {details['error_detail']}",
                "error_code": "GOOGLE_API_FAILURE",
                "http_status": details["http_status"],
                "error_reason": details["error_reason"],
                "error_message": details["error_message"],
                "sender_email": account,
            }

    def get_messages(self, query: str = "", account_email: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns messages with id, sender, subject and snippet. Raises RuntimeError with Google's error on failure."""
        credentials, cred_error, account = self._credentials_for(account_email)
        if not credentials:
            raise RuntimeError(cred_error["error"])
        try:
            from googleapiclient.discovery import build
            service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
            listing = service.users().messages().list(userId="me", q=query, maxResults=20).execute()
            messages = []
            for item in listing.get("messages", []):
                msg = service.users().messages().get(
                    userId="me", id=item["id"], format="metadata", metadataHeaders=["From", "Subject", "Date"]
                ).execute()
                headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
                messages.append({
                    "id": msg.get("id"),
                    "thread_id": msg.get("threadId"),
                    "from": headers.get("from", ""),
                    "subject": headers.get("subject", ""),
                    "date": headers.get("date", ""),
                    "snippet": msg.get("snippet", ""),
                    "account": account,
                })
            return messages
        except Exception as e:
            details = google_error_detail(e)
            logger.error("Gmail read failed: %s", details["error_detail"])
            raise RuntimeError(f"Gmail API error: {details['error_detail']}") from e

_mock_email_instance = MockEmailService()

def get_email_service() -> EmailService:
    if settings.DEMO_MODE:
        return _mock_email_instance
    return GmailService()
