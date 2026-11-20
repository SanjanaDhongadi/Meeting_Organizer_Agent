import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List
from worker.state.state_manager import state_manager

logger = logging.getLogger("meeting_job")

class MeetingJob:
    """
    Executes an APPROVED meeting by resuming its LangGraph workflow at the human-approval gate
    (gate -> execute -> wait/finalize). The API and the background worker share this path; an atomic
    APPROVED -> EXECUTING claim guarantees each approval is executed exactly once.
    """
    def execute(self, meeting_id: str) -> Dict[str, Any]:
        from backend.graph.workflow_graph import WorkflowError, clear_execution_steps, resume_meeting_workflow

        meeting = state_manager.load_meeting(meeting_id)
        if not meeting:
            return {"success": False, "error": f"Meeting {meeting_id} not found"}

        current_status = meeting.get("status")
        logger.info(f"[MeetingJob] Evaluating meeting {meeting_id} in status: {current_status}")

        if current_status != "APPROVED":
            return {"success": True, "status": current_status}
        if meeting.get("approval_status") != "APPROVED":
            return {"success": False, "error": "Meeting status is APPROVED but no human approval is recorded; refusing to execute."}
        # Atomic double-booking check + claim: no other meeting may hold an attendee's slot.
        emails = [p["email"] for p in meeting.get("participants", []) if p.get("response_status") != "REJECTED"]
        reservation = state_manager.reserve_and_claim(
            meeting_id, emails, meeting.get("scheduled_start") or "", meeting.get("scheduled_end") or "")
        if reservation["conflicts"]:
            from backend.skills.scheduling_skill import MeetingSchedulingSkill
            names = {p["email"].lower(): p["name"] for p in meeting.get("participants", [])}
            reasons = sorted({
                f"{names.get(c['email'], c['email'])} is not available during the selected time slot "
                f"(already in '{c['title']}', {MeetingSchedulingSkill.local_range(c['scheduled_start'], c['scheduled_end'])})."
                for c in reservation["conflicts"]
            })
            message = " ".join(reasons)
            state_manager.update_meeting_status(meeting_id, "RESCHEDULING_REQUIRED", "DOUBLE_BOOKING_PREVENTED", message)
            return {"success": False, "error": message,
                    "error_data": {"error_code": "ATTENDEE_UNAVAILABLE", "conflicts": reservation["conflicts"]},
                    "new_status": "RESCHEDULING_REQUIRED"}
        if not reservation["claimed"]:
            latest = state_manager.load_meeting(meeting_id) or {}
            return {"success": True, "status": latest.get("status"), "message": "Execution already claimed by another process."}

        # A fresh approval (APPROVED -> EXECUTING) starts without completed execution steps, and its "stuck" clock
        # starts now (the claim itself is a raw UPDATE that does not touch updated_at).
        clear_execution_steps(meeting_id)
        self._touch(meeting_id)
        try:
            final_state = resume_meeting_workflow(meeting_id, "APPROVAL")
        except WorkflowError as error:
            state_manager.update_meeting_status(meeting_id, "ACTION_FAILED", "WORKFLOW_FAILED", str(error))
            return {"success": False, "error": str(error), "error_data": {"node": error.node}}

        if final_state.get("execution_error"):
            err = final_state["execution_error"]
            return {"success": False, "error": err.get("error"), "error_data": err, "new_status": final_state.get("status")}
        return {
            "success": True,
            "new_status": final_state.get("status"),
            "execution": final_state.get("execution_result", {}),
            "warnings": final_state.get("warnings", []),
            "workflow_trace": [t["node"] for t in final_state.get("trace", [])],
        }

    @staticmethod
    def _touch(meeting_id: str) -> None:
        from backend.app.db import Meeting, SessionLocal
        db = SessionLocal()
        try:
            db.query(Meeting).filter(Meeting.id == meeting_id).update({Meeting.updated_at: datetime.utcnow()}, synchronize_session=False)
            db.commit()
        finally:
            db.close()

    def recover_stuck(self) -> List[str]:
        """Resume approved meetings whose execution crashed: still EXECUTING after EXECUTION_STUCK_MINUTES.

        Only approved meetings qualify. The lease is an atomic conditional update of updated_at (it only succeeds
        for the worker that still sees the stale value), so two workers never resume the same meeting. The resumed
        run skips the external steps already checkpointed in parsed_details["workflow"]["execution_steps"].
        """
        from app.config import settings
        from app.db import Meeting, SessionLocal
        from graph.workflow_graph import WorkflowError, resume_meeting_workflow
        from memory.audit_memory import audit_memory

        cutoff = datetime.utcnow() - timedelta(minutes=settings.EXECUTION_STUCK_MINUTES)
        db = SessionLocal()
        try:
            stale = [(m.id, m.updated_at) for m in db.query(Meeting).filter(
                Meeting.status == "EXECUTING", Meeting.approval_status == "APPROVED", Meeting.updated_at < cutoff).all()]
        finally:
            db.close()

        recovered: List[str] = []
        for meeting_id, seen_updated_at in stale:
            db = SessionLocal()
            try:
                won = db.query(Meeting).filter(
                    Meeting.id == meeting_id, Meeting.status == "EXECUTING", Meeting.approval_status == "APPROVED",
                    Meeting.updated_at == seen_updated_at,
                ).update({Meeting.updated_at: datetime.utcnow()}, synchronize_session=False) == 1
                db.commit()
            finally:
                db.close()
            if not won:
                continue   # another worker took the lease, or the run progressed in the meantime
            steps = sorted((((state_manager.load_meeting(meeting_id) or {}).get("parsed_details") or {})
                            .get("workflow") or {}).get("execution_steps") or {})
            audit_memory.record_event("Worker", "RUN_RECOVERED", meeting_id=meeting_id,
                                      details={"stuck_minutes": settings.EXECUTION_STUCK_MINUTES, "completed_steps": steps})
            try:
                resume_meeting_workflow(meeting_id, "APPROVAL")
            except WorkflowError as error:
                state_manager.update_meeting_status(meeting_id, "ACTION_FAILED", "WORKFLOW_FAILED", str(error))
            recovered.append(meeting_id)
        return recovered
