import json
from typing import Dict, Any, Optional
from datetime import datetime
from pydantic import BaseModel, Field
from backend.tools.base import BaseTool, ToolResult
from backend.app.db import SessionLocal, Meeting, MeetingParticipant, AuditLog
import logging

logger = logging.getLogger("state_tools")

# 10. save_meeting_state
class SaveMeetingStateInput(BaseModel):
    meeting_id: str = Field(..., description="Unique meeting ID")
    title: Optional[str] = Field(None, description="Meeting title")
    status: Optional[str] = Field(None, description="Current workflow state")
    mode: Optional[str] = Field(None, description="ONLINE or OFFLINE")
    scheduled_start: Optional[str] = Field(None, description="Scheduled start ISO string")
    scheduled_end: Optional[str] = Field(None, description="Scheduled end ISO string")
    duration_minutes: Optional[int] = Field(None, description="Meeting duration in minutes")
    purpose: Optional[str] = Field(None, description="Meeting purpose")
    agenda: Optional[str] = Field(None, description="Meeting agenda text")
    room_name: Optional[str] = Field(None, description="Assigned room name")
    meet_url: Optional[str] = Field(None, description="Google Meet URL")
    calendar_event_id: Optional[str] = Field(None, description="Calendar event ID")
    approval_status: Optional[str] = Field(None, description="PENDING, APPROVED, REJECTED")
    parsed_details: Optional[Dict[str, Any]] = Field(None, description="Structured parsed details")
    conflicts: Optional[list] = Field(None, description="Detected conflicts")
    critique_notes: Optional[str] = Field(None, description="Critique remarks")
    participants: Optional[list] = Field(None, description="Resolved participants keyed by email")

class SaveMeetingStateTool(BaseTool):
    name: str = "save_meeting_state"
    description: str = "Persist or update the meeting state and workflow progress in PostgreSQL/SQLite."
    args_schema = SaveMeetingStateInput

    def _run(self, args: SaveMeetingStateInput) -> ToolResult:
        db = SessionLocal()
        try:
            meeting = db.query(Meeting).filter(Meeting.id == args.meeting_id).first()
            if not meeting:
                meeting = Meeting(
                    id=args.meeting_id,
                    title=args.title or "Untitled Meeting",
                    raw_request="Internal Agent Request",
                    status=args.status or "DRAFT"
                )
                db.add(meeting)

            if args.title is not None:
                meeting.title = args.title
            if args.status is not None:
                meeting.status = args.status
            if args.mode is not None:
                meeting.mode = args.mode
            if args.scheduled_start is not None:
                meeting.scheduled_start = args.scheduled_start
            if args.scheduled_end is not None:
                meeting.scheduled_end = args.scheduled_end
            if args.duration_minutes is not None:
                meeting.duration_minutes = args.duration_minutes
            if args.purpose is not None:
                meeting.purpose = args.purpose
            if args.agenda is not None:
                meeting.agenda = args.agenda
            if args.room_name is not None:
                meeting.room_name = args.room_name
            if args.meet_url is not None:
                meeting.meet_url = args.meet_url
            if args.calendar_event_id is not None:
                meeting.calendar_event_id = args.calendar_event_id
            if args.approval_status is not None:
                meeting.approval_status = args.approval_status
            if args.parsed_details is not None:
                meeting.parsed_details = json.dumps(args.parsed_details)
            if args.conflicts is not None:
                meeting.conflicts = json.dumps(args.conflicts)
            if args.critique_notes is not None:
                meeting.critique_notes = args.critique_notes

            if args.participants is not None:
                current = {
                    participant.email.lower(): participant
                    for participant in db.query(MeetingParticipant).filter(
                        MeetingParticipant.meeting_id == args.meeting_id
                    ).all()
                }
                requested_emails = set()
                for details in args.participants:
                    email = (details.get("email") or "").strip()
                    if not email:
                        continue
                    email_key = email.lower()
                    requested_emails.add(email_key)
                    participant = current.get(email_key)
                    if participant is None:
                        participant = MeetingParticipant(meeting_id=args.meeting_id, email=email, name=details.get("name") or email)
                        db.add(participant)
                    participant.name = details.get("name") or email
                    participant.employee_id = details.get("employee_id")
                    participant.is_external = bool(details.get("is_external", False))
                    participant.calendar_status = details.get("calendar_status", "UNVERIFIED")
                    participant.response_status = details.get("response_status", "PENDING")
                for email, participant in current.items():
                    if email not in requested_emails:
                        db.delete(participant)

            db.commit()
            db.refresh(meeting)
            return ToolResult(
                success=True,
                data=meeting.to_dict(),
                tool_name=self.name
            )
        except Exception as e:
            db.rollback()
            return ToolResult(
                success=False,
                error=f"Failed to persist meeting state: {e}",
                tool_name=self.name
            )
        finally:
            db.close()

# 11. load_meeting_state
class LoadMeetingStateInput(BaseModel):
    meeting_id: str = Field(..., description="ID of the meeting to load")

class LoadMeetingStateTool(BaseTool):
    name: str = "load_meeting_state"
    description: str = "Load current meeting state, participants, and workflow status for pause/resume."
    args_schema = LoadMeetingStateInput

    def _run(self, args: LoadMeetingStateInput) -> ToolResult:
        db = SessionLocal()
        try:
            meeting = db.query(Meeting).filter(Meeting.id == args.meeting_id).first()
            if not meeting:
                return ToolResult(
                    success=False,
                    error=f"Meeting ID '{args.meeting_id}' not found",
                    tool_name=self.name
                )
            
            participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == args.meeting_id).all()
            data = meeting.to_dict()
            data["participants"] = [p.to_dict() for p in participants]
            return ToolResult(
                success=True,
                data=data,
                tool_name=self.name
            )
        finally:
            db.close()

# 12. create_audit_log
class CreateAuditLogInput(BaseModel):
    agent: str = Field(..., description="Name of the agent logging the event")
    action: str = Field(..., description="Action name (e.g. INTAKE, RETRIEVE, CHECK_AVAILABILITY)")
    meeting_id: Optional[str] = Field(None, description="Associated meeting ID")
    tool: Optional[str] = Field("", description="Tool called, if applicable")
    status: str = Field("SUCCESS", description="SUCCESS, WARNING, ERROR, WAITING")
    error: Optional[str] = Field("", description="Error message if any")
    approval_status: Optional[str] = Field("", description="Approval gate status if relevant")
    details: Optional[Dict[str, Any]] = Field(default={}, description="Arbitrary execution metadata")

class CreateAuditLogTool(BaseTool):
    name: str = "create_audit_log"
    description: str = "Record an immutable audit log entry for agent decisions, tool invocations, and human approvals."
    args_schema = CreateAuditLogInput

    def _run(self, args: CreateAuditLogInput) -> ToolResult:
        db = SessionLocal()
        try:
            audit = AuditLog(
                meeting_id=args.meeting_id,
                agent=args.agent,
                action=args.action,
                tool=args.tool or "",
                status=args.status,
                error=args.error or "",
                approval_status=args.approval_status or "",
                details=json.dumps(args.details or {})
            )
            db.add(audit)
            db.commit()
            db.refresh(audit)
            return ToolResult(
                success=True,
                data=audit.to_dict(),
                tool_name=self.name
            )
        except Exception as e:
            db.rollback()
            return ToolResult(
                success=False,
                error=f"Failed to record audit log: {e}",
                tool_name=self.name
            )
        finally:
            db.close()
