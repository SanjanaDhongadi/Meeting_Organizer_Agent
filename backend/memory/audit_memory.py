import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime
from backend.app.db import SessionLocal, AuditLog

logger = logging.getLogger("audit_memory")

class AuditMemory:
    """
    Lab 4: Audit Memory
    Maintains an append-only, tamper-evident record of all agent steps,
    tool invocations, human approvals, conflicts, and external responses.
    """

    def record_event(
        self,
        agent: str,
        action: str,
        meeting_id: Optional[str] = None,
        tool: str = "",
        status: str = "SUCCESS",
        error: str = "",
        approval_status: str = "",
        details: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            entry = AuditLog(
                meeting_id=meeting_id,
                agent=agent,
                action=action,
                tool=tool or "",
                status=status,
                error=error or "",
                approval_status=approval_status or "",
                details=json.dumps(details or {})
            )
            db.add(entry)
            db.commit()
            db.refresh(entry)
            logger.info(f"[AUDIT] [{agent}] {action} | Status: {status} | Meeting: {meeting_id}")
            return entry.to_dict()
        except Exception as e:
            logger.error(f"Failed to write audit memory: {e}")
            db.rollback()
            return {
                "agent": agent,
                "action": action,
                "status": "ERROR",
                "error": str(e)
            }
        finally:
            db.close()

    def get_meeting_traces(self, meeting_id: str) -> List[Dict[str, Any]]:
        db = SessionLocal()
        try:
            # Only this meeting's trace; global (meeting-less) events such as employee creation are not part of it.
            logs = db.query(AuditLog).filter(
                AuditLog.meeting_id == meeting_id
            ).order_by(AuditLog.timestamp.asc(), AuditLog.id.asc()).all()
            return [l.to_dict() for l in logs]
        finally:
            db.close()

    def get_recent_logs(self, limit: int = 50) -> List[Dict[str, Any]]:
        db = SessionLocal()
        try:
            logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit).all()
            return [l.to_dict() for l in reversed(logs)]
        finally:
            db.close()

audit_memory = AuditMemory()
