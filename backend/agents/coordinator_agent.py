import re
import uuid
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

from backend.runtime.agent_interface import BaseAgent, AgentOutput
from backend.agents.participant_agent import ParticipantAgent
from backend.agents.scheduling_agent import SchedulingAgent
from backend.agents.agenda_agent import AgendaAgent
from backend.agents.resource_agent import ResourceAgent
from backend.agents.validation_agent import ValidationAgent
from backend.app.config import settings

logger = logging.getLogger("coordinator_agent")

class MeetingCoordinator(BaseAgent):
    def __init__(self):
        super().__init__(
            name="Meeting Coordinator",
            role="Orchestrate end-to-end multi-agent workflow, parallel dispatch, merge, critique, and conflict resolution",
            description="Controls the lifecycle from natural-language intake to draft plan assembly, conflict management, and final execution."
        )
        self.participant_agent = ParticipantAgent()
        self.scheduling_agent = SchedulingAgent()
        self.agenda_agent = AgendaAgent()
        self.resource_agent = ResourceAgent()
        self.validation_agent = ValidationAgent()

    def parse_natural_language_request(self, text: str) -> Dict[str, Any]:
        """
        Extracts participants, date, time/range, duration, mode, purpose, and room constraints.
        Supports OpenAI LLM extraction with deterministic fallback.
        """
        if settings.OPENAI_API_KEY and not settings.DEMO_MODE:
            try:
                from openai import OpenAI
                import json
                client = OpenAI(api_key=settings.OPENAI_API_KEY)
                prompt = (
                    "You are a meeting extraction engine. Extract JSON with keys: "
                    "participants (list of names/emails), date (YYYY-MM-DD or 'Friday'), "
                    "start_time (HH:MM in 24h format), end_time (HH:MM in 24h format), "
                    "duration_minutes (int), mode ('ONLINE' or 'OFFLINE'), "
                    "purpose (string), room_name (string or empty), equipment (string or empty). "
                    f"User request: '{text}'"
                )
                res = client.chat.completions.create(
                    model=settings.OPENAI_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"}
                )
                return json.loads(res.choices[0].message.content)
            except Exception as e:
                logger.warning(f"OpenAI extraction failed ({e}), using regex parser.")

        text_lower = text.lower()

        # 1. Mode: Online vs Offline
        mode = "ONLINE"
        if any(w in text_lower for w in ["offline", "in-person", "in person", "auditorium", "room"]):
            mode = "OFFLINE"
        elif "online" in text_lower or "virtual" in text_lower or "meet" in text_lower or "zoom" in text_lower:
            mode = "ONLINE"

        # 2. Participants extraction
        participants = []
        with_match = re.search(r"with\s+([A-Za-z0-9@\.\,\s]+?)(?=\s+(on|at|from|to|for|in|this|next|it\s+should)|$)", text, re.IGNORECASE)
        if with_match:
            raw_p = with_match.group(1)
            parts = re.split(r",|\band\b|&", raw_p)
            for p in parts:
                p_clean = p.strip()
                if p_clean and p_clean.lower() not in ["a meeting", "the team", "everyone"]:
                    participants.append(p_clean)

        # 3. Purpose extraction
        purpose = ""
        p_match = re.search(r"(?:purpose\s+is\s+to|purpose:\s*|to\s+discuss|for\s+discussing|discuss\s+|topic\s+is\s+|to\s+review)(.+?)(?=\.|\band\b\s+(?:it|the)|$)", text, re.IGNORECASE)
        if p_match:
            purpose = p_match.group(1).strip()
            if not purpose.startswith("discuss") and not purpose.startswith("review"):
                purpose = f"Discuss {purpose}"
        elif "discuss" in text_lower:
            d_idx = text_lower.find("discuss")
            purpose = text[d_idx:].split(".")[0].strip()
        elif "review" in text_lower:
            r_idx = text_lower.find("review")
            purpose = text[r_idx:].split(".")[0].strip()

        # 4. Date & Time extraction
        today = datetime.now(timezone.utc)
        days_ahead = (4 - today.weekday()) % 7 # Default to upcoming Friday
        if days_ahead == 0:
            days_ahead = 7
        target_date = today + timedelta(days=days_ahead)
        date_str = target_date.strftime("%Y-%m-%d")

        day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        for idx, d_name in enumerate(day_names):
            if d_name in text_lower:
                days_diff = (idx - today.weekday()) % 7
                if days_diff == 0:
                    days_diff = 7
                target_date = today + timedelta(days=days_diff)
                date_str = target_date.strftime("%Y-%m-%d")
                break

        time_match = re.search(r"(?:from\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:to|-)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text_lower)
        if time_match:
            s_hour = int(time_match.group(1))
            s_min = int(time_match.group(2) or 0)
            s_ampm = time_match.group(3) or time_match.group(6) or "pm"
            if s_ampm == "pm" and s_hour < 12:
                s_hour += 12
            elif s_ampm == "am" and s_hour == 12:
                s_hour = 0

            e_hour = int(time_match.group(4))
            e_min = int(time_match.group(5) or 0)
            e_ampm = time_match.group(6) or s_ampm
            if e_ampm == "pm" and e_hour < 12:
                e_hour += 12
            elif e_ampm == "am" and e_hour == 12:
                e_hour = 0

            duration = max(15, (e_hour * 60 + e_min) - (s_hour * 60 + s_min))
            start_iso = f"{date_str}T{s_hour:02d}:{s_min:02d}:00Z"
            end_iso = f"{date_str}T{e_hour:02d}:{e_min:02d}:00Z"
        else:
            single_match = re.search(r"(?:at\s+)(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text_lower)
            if single_match:
                s_hour = int(single_match.group(1))
                s_min = int(single_match.group(2) or 0)
                ampm = single_match.group(3) or "pm"
                if ampm == "pm" and s_hour < 12:
                    s_hour += 12
                duration = 30
                start_iso = f"{date_str}T{s_hour:02d}:{s_min:02d}:00Z"
                end_iso = f"{date_str}T{(s_hour + 1):02d}:{s_min:02d}:00Z"
            else:
                duration = 30
                start_iso = f"{date_str}T15:00:00Z"
                end_iso = f"{date_str}T15:30:00Z"

        room_name = ""
        if "auditorium" in text_lower:
            room_name = "Auditorium Alpha"
        elif "conference room b" in text_lower or "innovation lab" in text_lower:
            room_name = "Conference Room B (Innovation Lab)"

        equipment = ""
        equipment_match = re.search(
            r"(?:required equipment|equipment(?: requirements)?):\s*(.+?)(?=\.|$)",
            text,
            re.IGNORECASE,
        )
        if equipment_match:
            equipment = equipment_match.group(1).strip()

        return {
            "participants": participants,
            "date": date_str,
            "start_time": start_iso,
            "end_time": end_iso,
            "duration_minutes": duration,
            "mode": mode,
            "purpose": purpose,
            "room_name": room_name,
            "equipment": equipment
        }

    def execute_parallel_agents(self, state: Dict[str, Any]) -> Dict[str, AgentOutput]:
        """
        Lab 8: Executes independent agents in parallel (Participant, Scheduling, Agenda, Resource).
        """
        logger.info("[Coordinator] Launching parallel agent execution swarm...")
        results = {}

        p_output = self.participant_agent.execute(state)
        results["participant"] = p_output

        updated_state = dict(state)
        updated_state["resolved_participants"] = p_output.data.get("resolved_participants", [])
        updated_state["participant_emails"] = p_output.data.get("participant_emails", [])

        with ThreadPoolExecutor(max_workers=3) as executor:
            future_sched = executor.submit(self.scheduling_agent.execute, updated_state)
            future_agenda = executor.submit(self.agenda_agent.execute, updated_state)
            future_res = executor.submit(self.resource_agent.execute, updated_state)

            results["scheduling"] = future_sched.result()
            results["agenda"] = future_agenda.result()
            results["resource"] = future_res.result()

        return results

    def merge_results(self, agent_outputs: Dict[str, AgentOutput], base_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Lab 8 Merge: Aggregates parallel agent findings into unified draft candidate.
        """
        p_res = agent_outputs.get("participant", AgentOutput(agent_name="Participant", status="ERROR"))
        s_res = agent_outputs.get("scheduling", AgentOutput(agent_name="Scheduling", status="ERROR"))
        a_res = agent_outputs.get("agenda", AgentOutput(agent_name="Agenda", status="ERROR"))
        r_res = agent_outputs.get("resource", AgentOutput(agent_name="Resource", status="ERROR"))

        merged = {
            "participants": p_res.data.get("resolved_participants", []),
            "unknown_participants": p_res.data.get("unknown_participants", []),
            "ambiguous_participants": p_res.data.get("ambiguous_participants", []),
            "scheduling_slot": s_res.data.get("selected_slot"),
            "alternative_slots": s_res.data.get("alternative_slots", []),
            "participant_calendar_statuses": s_res.data.get("participant_statuses", {}),
            "scheduling_conflicts": s_res.data.get("conflicts", []),
            "agenda_text": a_res.data.get("agenda", ""),
            "has_purpose": a_res.data.get("has_purpose", False),
            "agenda_prompt": a_res.message,
            "resource_info": r_res.data,
            "mode": r_res.data.get("mode", base_state.get("mode", "ONLINE")),
            "raw_agent_errors": p_res.errors + s_res.errors + a_res.errors + r_res.errors
        }
        return merged

    def critique(self, merged: Dict[str, Any], base_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Lab 8 Critique: Evaluates combined state for logical, environmental, or constraint discrepancies.
        """
        critique_notes = []
        is_consistent = True

        slot = merged.get("scheduling_slot") or {}
        start_str = slot.get("start") or base_state.get("target_start_time") or ""

        day_name = ""
        if start_str:
            try:
                dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                day_name = dt.strftime("%A").lower()
            except Exception:
                day_name = "friday"

        for p in merged.get("participants", []):
            prefs = (p.get("preferences") or "").lower()
            if "no meetings on friday" in prefs and ("friday" in start_str.lower() or day_name == "friday"):
                critique_notes.append(f"Participant {p['name']} has strict policy against Friday meetings.")
                is_consistent = False
            if "morning" in prefs and "15:00" in start_str:
                critique_notes.append(f"Participant {p['name']} prefers morning meetings, but afternoon slot proposed.")

        mode = merged.get("mode", "ONLINE")
        if mode == "OFFLINE" and not merged.get("resource_info", {}).get("room_name"):
            critique_notes.append("Offline meeting has no room allocated.")
            is_consistent = False

        return {
            "is_consistent": is_consistent,
            "critique_notes": critique_notes
        }

    def detect_and_resolve_conflicts(self, merged: Dict[str, Any], critique_res: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Lab 8 Conflict Detection & Resolution:
        Records Conflict, Source Agents, Conflicting Values, Evidence, and Resolution.
        """
        conflicts = []
        resource_data = merged.get("resource_info") or {}

        # 1. Unknown participants
        if merged.get("unknown_participants"):
            for u in merged["unknown_participants"]:
                conflicts.append({
                    "conflict_id": f"conf-{uuid.uuid4().hex[:6]}",
                    "conflict": f"Unregistered participant '{u['queried_name']}'",
                    "source_agents": ["Participant Agent"],
                    "conflicting_values": {"participant": u["queried_name"], "db_status": "NOT_FOUND"},
                    "evidence": "Participant query did not match any active employee profile in database.",
                    "resolution": "Solicit participant's email from organizer or provide registration link.",
                    "status": "UNRESOLVED_BLOCKER"
                })

        # 2. Ambiguous participants
        if merged.get("ambiguous_participants"):
            for a in merged["ambiguous_participants"]:
                conflicts.append({
                    "conflict_id": f"conf-{uuid.uuid4().hex[:6]}",
                    "conflict": f"Ambiguous name match for '{a['queried_name']}'",
                    "source_agents": ["Participant Agent"],
                    "conflicting_values": {"queried": a["queried_name"], "match_count": len(a["candidates"])},
                    "evidence": "Multiple employee profiles share the same name.",
                    "resolution": "Halt automatic scheduling and prompt user to select specific candidate.",
                    "status": "UNRESOLVED_BLOCKER"
                })

        # 3. Direct Scheduling Conflicts (Calendar / Working Hours)
        for sc in merged.get("scheduling_conflicts", []):
            is_warning = sc.get("severity") == "WARNING"
            conflicts.append({
                "conflict_id": f"conf-{uuid.uuid4().hex[:6]}",
                "conflict": sc.get("conflict", "Time slot conflict"),
                "source_agents": ["Scheduling & Availability Agent"],
                "conflicting_values": {"participant": sc.get("participant"), "field": sc.get("field")},
                "evidence": sc.get("conflict"),
                "resolution": (
                    "Calendar access is unverified; review with the participant before confirming."
                    if is_warning else "Find an alternative slot or reschedule to another day."
                ),
                "status": "FLAGGED_FOR_HUMAN" if is_warning else "UNRESOLVED_BLOCKER"
            })

        # 4. Resource vs Scheduling conflict
        if resource_data.get("mode") == "OFFLINE" and resource_data.get("requires_facility_approval"):
            conflicts.append({
                "conflict_id": f"conf-{uuid.uuid4().hex[:6]}",
                "conflict": "Auditorium booking requires asynchronous facility manager signoff",
                "source_agents": ["Scheduling Agent", "Resource/Room Agent"],
                "conflicting_values": {
                    "scheduling": "Common participant calendar slot available",
                    "resource": "Auditorium Alpha booking is PENDING facility review"
                },
                "evidence": f"Facility manager approval needed for {resource_data.get('room_name')}.",
                "resolution": "Wait for the auditorium response after human organizer approval.",
                "status": "PENDING_ASYNC"
            })

        # 5. Critique preference conflicts
        if not critique_res.get("is_consistent"):
            for note in critique_res.get("critique_notes", []):
                # Avoid duplicate if already covered by scheduling conflict
                if not any(note in c["conflict"] for c in conflicts):
                    conflicts.append({
                        "conflict_id": f"conf-{uuid.uuid4().hex[:6]}",
                        "conflict": note,
                        "source_agents": ["Participant Agent", "Scheduling Agent"],
                        "conflicting_values": {"preference": "Restricted"},
                        "evidence": note,
                        "resolution": "Flagged to Human Organizer in Hard Gate approval view.",
                        "status": "FLAGGED_FOR_HUMAN"
                    })

        return conflicts

    def execute(self, state: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> AgentOutput:
        agent_outputs = self.execute_parallel_agents(state)
        merged = self.merge_results(agent_outputs, state)
        critique_res = self.critique(merged, state)
        conflicts = self.detect_and_resolve_conflicts(merged, critique_res)

        return AgentOutput(
            agent_name=self.name,
            status="SUCCESS",
            data={
                "merged": merged,
                "critique": critique_res,
                "conflicts": conflicts
            }
        )
