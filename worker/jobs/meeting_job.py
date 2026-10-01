import logging
from typing import Dict, Any
from worker.state.state_manager import state_manager

logger = logging.getLogger("meeting_job")

class MeetingJob:
    """
    Executes an APPROVED meeting by resuming its LangGraph workflow at the human-approval gate
    (gate -> execute -> wait/finalize). The API and the background worker share this path; an atomic
    APPROVED -> EXECUTING claim guarantees each approval is executed exactly once.
    """
    def execute(self, meeting_id: str) -> Dict[str, Any]:
        from backend.graph.workflow_graph import WorkflowError, resume_meeting_workflow

        meeting = state_manager.load_meeting(meeting_id)
        if not meeting:
            return {"success": False, "error": f"Meeting {meeting_id} not found"}

        current_status = meeting.get("status")
        logger.info(f"[MeetingJob] Evaluating meeting {meeting_id} in status: {current_status}")

        if current_status != "APPROVED":
            return {"success": True, "status": current_status}
        if meeting.get("approval_status") != "APPROVED":
            return {"success": False, "error": "Meeting status is APPROVED but no human approval is recorded; refusing to execute."}
        if not state_manager.claim_status(meeting_id, "APPROVED", "EXECUTING"):
            latest = state_manager.load_meeting(meeting_id) or {}
            return {"success": True, "status": latest.get("status"), "message": "Execution already claimed by another process."}

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
