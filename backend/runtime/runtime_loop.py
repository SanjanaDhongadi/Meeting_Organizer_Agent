import uuid
import logging
from typing import Dict, Any, Optional, List
from backend.runtime.agent_interface import BaseAgent, AgentOutput
from backend.skills.registry import skill_registry
from backend.tools.registry import tool_registry
from backend.memory.session_memory import session_memory
from backend.memory.long_term_memory import long_term_memory
from backend.memory.audit_memory import audit_memory
from backend.agents.coordinator_agent import MeetingCoordinator

logger = logging.getLogger("agent_runtime")

class AgentRuntime:
    """
    Lab 6: Reusable Agent Runtime
    Provides the central execution loop governing Agent/Skill selection,
    Tool invocation, Session & Long-Term Memory access, and Audit logging.
    """

    def __init__(self):
        self.coordinator = MeetingCoordinator()
        self.tool_registry = tool_registry
        self.skill_registry = skill_registry
        self.session_memory = session_memory
        self.long_term_memory = long_term_memory
        self.audit_memory = audit_memory

    def run_meeting_request(self, raw_request: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Executes complete runtime loop:
        1. Intake & Audit
        2. Natural Language Parsing
        3. Long-term RAG Context Enrichment
        4. Parallel Multi-Agent execution (Lab 8)
        5. Merge + Critique + Conflict Resolution
        6. Validation (Hard Gate Check)
        7. State Persistence & Audit Trace
        """
        meeting_id = session_id or f"meet-{uuid.uuid4().hex[:10]}"
        logger.info(f"[Runtime] Initiating meeting orchestration {meeting_id} for: '{raw_request}'")

        # 1. Audit Intake
        self.audit_memory.record_event(
            agent="Meeting Coordinator",
            action="INTAKE",
            meeting_id=meeting_id,
            status="SUCCESS",
            details={"raw_request": raw_request}
        )
        self.session_memory.init_session(meeting_id, raw_request)

        # 2. Parse request
        parsed = self.coordinator.parse_natural_language_request(raw_request)
        self.audit_memory.record_event(
            agent="Meeting Coordinator",
            action="PARSE_REQUEST",
            meeting_id=meeting_id,
            status="SUCCESS",
            details=parsed
        )

        # 3. Memory & RAG Context Enrichment
        # Retrieve previous context or participant profiles
        rag_context = self.long_term_memory.search_context(raw_request)

        # 4. Prepare state for agent swarm
        state = {
            "meeting_id": meeting_id,
            "raw_request": raw_request,
            "parsed_participants": parsed.get("participants", []),
            "target_date": parsed.get("date"),
            "target_start_time": parsed.get("start_time"),
            "target_end_time": parsed.get("end_time"),
            "duration_minutes": parsed.get("duration_minutes", 30),
            "mode": parsed.get("mode", "ONLINE"),
            "purpose": parsed.get("purpose", ""),
            "equipment": parsed.get("equipment", ""),
            "room_name": parsed.get("room_name", ""),
            "rag_context": rag_context
        }

        # 5. Parallel Execution (Lab 8)
        self.audit_memory.record_event(
            agent="Meeting Coordinator",
            action="PARALLEL_AGENT_DISPATCH",
            meeting_id=meeting_id,
            status="SUCCESS",
            details={"agents": ["Participant Agent", "Scheduling Agent", "Agenda Agent", "Resource Agent"]}
        )

        agent_outputs = self.coordinator.execute_parallel_agents(state)

        # Record individual agent audit entries
        for ag_key, ag_out in agent_outputs.items():
            self.audit_memory.record_event(
                agent=ag_out.agent_name,
                action=f"{ag_key.upper()}_COMPLETED",
                meeting_id=meeting_id,
                status="SUCCESS" if ag_out.status == "SUCCESS" else "WARNING",
                error="; ".join(ag_out.errors) if ag_out.errors else "",
                details={"data": ag_out.data, "tool_calls": ag_out.tool_calls}
            )

        # 6. Merge (Lab 8)
        merged = self.coordinator.merge_results(agent_outputs, state)
        self.audit_memory.record_event(
            agent="Meeting Coordinator",
            action="MERGE",
            meeting_id=meeting_id,
            status="SUCCESS",
            details={"merged_participants": len(merged.get("participants", []))}
        )

        # 7. Critique (Lab 8)
        critique_res = self.coordinator.critique(merged, state)
        self.audit_memory.record_event(
            agent="Meeting Coordinator",
            action="CRITIQUE",
            meeting_id=meeting_id,
            status="SUCCESS" if critique_res["is_consistent"] else "WARNING",
            details=critique_res
        )

        # 8. Conflict Detection & Resolution (Lab 8)
        conflicts = self.coordinator.detect_and_resolve_conflicts(merged, critique_res)
        if conflicts:
            for c in conflicts:
                self.audit_memory.record_event(
                    agent="Meeting Coordinator",
                    action="CONFLICT_DETECTED",
                    meeting_id=meeting_id,
                    status="WARNING",
                    details=c
                )

        # 9. Validation Agent
        val_output = self.coordinator.validation_agent.execute({
            "draft_plan": {
                "meeting_id": meeting_id,
                "scheduled_start": parsed.get("start_time"),
                "scheduled_end": parsed.get("end_time"),
                "mode": merged.get("mode", "ONLINE"),
                "room_name": merged.get("resource_info", {}).get("room_name")
            },
            "resolved_participants": merged.get("participants", []),
            "mode": merged.get("mode", "ONLINE"),
            "resource_data": merged.get("resource_info", {})
        })

        self.audit_memory.record_event(
            agent="Validation Agent",
            action="VALIDATION",
            meeting_id=meeting_id,
            status="SUCCESS" if val_output.data.get("can_proceed_to_approval") else "WARNING",
            details=val_output.data
        )

        # 10. Assemble Draft Plan & Determine Workflow State
        has_blockers = any(c.get("status") == "UNRESOLVED_BLOCKER" for c in conflicts) or not val_output.data.get("can_proceed_to_approval")
        
        if has_blockers:
            status = "RESCHEDULING_REQUIRED"
        elif not merged.get("has_purpose") and not state.get("skip_agenda"):
            status = "DRAFT" # Needs purpose prompt
        else:
            status = "WAITING_FOR_HUMAN_APPROVAL"

        slot = merged.get("scheduling_slot") or {"start": parsed.get("start_time"), "end": parsed.get("end_time")}
        start_time_final = slot.get("start") if isinstance(slot, dict) else parsed.get("start_time")
        end_time_final = slot.get("end") if isinstance(slot, dict) else parsed.get("end_time")

        draft_plan = {
            "meeting_id": meeting_id,
            "title": f"Meeting: {parsed.get('purpose') or 'Project Sync'}",
            "status": status,
            "mode": merged.get("mode", "ONLINE"),
            "scheduled_start": start_time_final,
            "scheduled_end": end_time_final,
            "duration_minutes": parsed.get("duration_minutes", 30),
            "purpose": parsed.get("purpose", ""),
            "agenda": merged.get("agenda_text", ""),
            "participants": merged.get("participants", []),
            "unknown_participants": merged.get("unknown_participants", []),
            "ambiguous_participants": merged.get("ambiguous_participants", []),
            "room_name": merged.get("resource_info", {}).get("room_name", ""),
            "meet_url": merged.get("resource_info", {}).get("provisional_meet_url", ""),
            "conflicts": conflicts,
            "critique_notes": critique_res.get("critique_notes", []),
            "validation": val_output.data,
            "approval_status": "PENDING"
        }

        # 11. Persist state via tool
        save_tool = self.tool_registry.get("save_meeting_state")
        save_tool.execute(
            meeting_id=meeting_id,
            title=draft_plan["title"],
            status=status,
            mode=draft_plan["mode"],
            scheduled_start=draft_plan["scheduled_start"],
            scheduled_end=draft_plan["scheduled_end"],
            duration_minutes=draft_plan["duration_minutes"],
            purpose=draft_plan["purpose"],
            agenda=draft_plan["agenda"],
            room_name=draft_plan["room_name"],
            meet_url=draft_plan["meet_url"],
            approval_status="PENDING",
            parsed_details={**parsed, "validation": val_output.data},
            conflicts=conflicts,
            critique_notes="; ".join(critique_res.get("critique_notes", [])),
            participants=draft_plan["participants"]
        )

        # Update session memory
        self.session_memory.update_session(meeting_id, {
            "status": status,
            "draft_plan": draft_plan,
            "approval_state": "PENDING"
        })

        self.audit_memory.record_event(
            agent="Meeting Coordinator",
            action="CREATE_DRAFT_PLAN",
            meeting_id=meeting_id,
            status="SUCCESS",
            approval_status="WAITING",
            details={"status": status}
        )

        return draft_plan

agent_runtime = AgentRuntime()
