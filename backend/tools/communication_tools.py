from typing import List, Optional
from pydantic import BaseModel, Field
from backend.tools.base import BaseTool, ToolResult
from backend.connectors.mcp_gateway import mcp_call
import logging

logger = logging.getLogger("communication_tools")

# 7. send_email
class SendEmailInput(BaseModel):
    to_emails: List[str] = Field(..., description="Recipients of the email")
    subject: str = Field(..., description="Subject line of the email")
    body: str = Field(..., description="Content body of the email invitation")
    meeting_id: str = Field("unknown", description="ID of the associated meeting")

class SendEmailTool(BaseTool):
    name: str = "send_email"
    description: str = "Send meeting invitations or agenda updates to participants via Gmail or Mock Email."
    args_schema = SendEmailInput

    def _run(self, args: SendEmailInput) -> ToolResult:
        if not args.to_emails:
            return ToolResult(
                success=False,
                error="Must specify at least one recipient email address",
                tool_name=self.name
            )
        res = mcp_call("gmail.send", {
            "to_emails": args.to_emails,
            "subject": args.subject,
            "body": args.body,
            "meeting_id": args.meeting_id,
        })
        return ToolResult(
            success=res.get("success", False),
            data=res,
            error=None if res.get("success") else res.get("error"),
            tool_name=self.name
        )
