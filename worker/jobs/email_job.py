import logging
from typing import Dict, Any, List
from backend.connectors.email_service import get_email_service

logger = logging.getLogger("email_job")

class EmailJob:
    def send_notifications(self, meeting_id: str, to_emails: List[str], subject: str, body: str) -> Dict[str, Any]:
        logger.info(f"[EmailJob] Sending emails for meeting {meeting_id} to {to_emails}")
        emailer = get_email_service()
        return emailer.send_invitation(to_emails, subject, body, meeting_id)
