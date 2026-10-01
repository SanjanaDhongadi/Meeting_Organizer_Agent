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
                llm = json.loads(res.choices[0].message.content)
                return self._normalize_llm_parse(llm, self._regex_parse(text))
            except Exception as e:
                logger.warning(f"OpenAI extraction failed ({e}), using regex parser.")

        return self._regex_parse(text)

    def _normalize_llm_parse(self, llm: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
        """The LLM returns HH:MM times and dates like 'Friday'; downstream agents need ISO-8601 UTC datetimes."""
        parsed = dict(fallback)
        date_value = str(llm.get("date") or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
            parsed["date"] = date_value
        elif date_value:
            today = datetime.now(timezone.utc)
            names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            if date_value.lower() in names:
                diff = (names.index(date_value.lower()) - today.weekday()) % 7 or 7
                parsed["date"] = (today + timedelta(days=diff)).strftime("%Y-%m-%d")

        def to_iso(value: Any) -> Optional[str]:
            value = str(value or "").strip()
            if re.fullmatch(r"\d{1,2}:\d{2}", value):
                h, m = value.split(":")
                return self._to_utc_iso(parsed["date"], int(h), int(m))
            if "T" in value:
                return value if value.endswith("Z") or "+" in value else value + "Z"
            return None

        start_iso = to_iso(llm.get("start_time")) or fallback["start_time"]
        duration = int(llm.get("duration_minutes") or 0) or None
        end_iso = to_iso(llm.get("end_time"))
        if not end_iso:
            minutes = duration or fallback.get("duration_minutes", 30)
            end_iso = (datetime.fromisoformat(start_iso.replace("Z", "+00:00")) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
        if not duration:
            duration = max(15, int((datetime.fromisoformat(end_iso.replace("Z", "+00:00")) - datetime.fromisoformat(start_iso.replace("Z", "+00:00"))).total_seconds() // 60))
        participants = llm.get("participants")
        parsed.update({
            "participants": [str(p).strip() for p in participants if str(p).strip()] if isinstance(participants, list) else fallback["participants"],
            "start_time": start_iso,
            "end_time": end_iso,
            "duration_minutes": duration,
            "mode": "OFFLINE" if str(llm.get("mode", "")).upper() == "OFFLINE" else ("ONLINE" if llm.get("mode") else fallback["mode"]),
            "purpose": (llm.get("purpose") or fallback["purpose"] or "").strip(),
            "room_name": (llm.get("room_name") or fallback["room_name"] or "").strip(),
            "equipment": (llm.get("equipment") or fallback["equipment"] or "").strip(),
            "parser": "openai",
        })
        return parsed

    @staticmethod
    def _to_utc_iso(date_str: str, hour: int, minute: int) -> str:
        """Times in a request are wall-clock times in MEETING_TIMEZONE; the workflow stores UTC."""
        from zoneinfo import ZoneInfo
        try:
            tz = ZoneInfo(settings.MEETING_TIMEZONE)
        except Exception:
            tz = timezone.utc
        local = datetime.fromisoformat(f"{date_str}T00:00:00").replace(tzinfo=tz) + timedelta(hours=hour, minutes=minute)
        return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _match_room(text_lower: str) -> str:
        """Match a room from the configured catalog (full name, base name, or the alias in parentheses)."""
        from database.seed.seed_data import SEED_ROOMS
        for room in SEED_ROOMS:
            name = room["name"].lower()
            base = name.split(" (")[0]
            alias = name[name.find("(") + 1:name.find(")")] if "(" in name and ")" in name else ""
            if name in text_lower or base in text_lower or (alias and alias in text_lower):
                return room["name"]
        if "auditorium" in text_lower:
            auditoriums = [r for r in SEED_ROOMS if "auditorium" in r["name"].lower()]
            if auditoriums:
                return max(auditoriums, key=lambda r: r.get("capacity", 0))["name"]
        return ""

    def _regex_parse(self, text: str) -> Dict[str, Any]:

        text_lower = text.lower()

        # 1. Mode: Online vs Offline
        mode = "ONLINE"
        if any(w in text_lower for w in ["offline", "in-person", "in person", "auditorium", "room"]):
            mode = "OFFLINE"
        elif "online" in text_lower or "virtual" in text_lower or "meet" in text_lower or "zoom" in text_lower:
            mode = "ONLINE"

        # 2. Participants extraction
        participants = []
        with_match = re.search(r"with\s+([A-Za-z0-9@\.\,\s_+\-]+?)(?=\s+(on|at|from|to|for|in|this|next|it\s+should)|$)", text, re.IGNORECASE)
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
            start_iso = self._to_utc_iso(date_str, s_hour, s_min)
            end_iso = self._to_utc_iso(date_str, e_hour, e_min)
        else:
            single_match = re.search(r"(?:at\s+)(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text_lower)
            if single_match:
                s_hour = int(single_match.group(1))
                s_min = int(single_match.group(2) or 0)
                ampm = single_match.group(3) or "pm"
                if ampm == "pm" and s_hour < 12:
                    s_hour += 12
                duration = 30
                start_iso = self._to_utc_iso(date_str, s_hour, s_min)
                end_iso = self._to_utc_iso(date_str, s_hour, s_min + duration)
            else:
                duration = 30
                start_iso = self._to_utc_iso(date_str, 15, 0)
                end_iso = self._to_utc_iso(date_str, 15, 30)

        room_name = self._match_room(text_lower)

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

    @staticmethod
    def _blocked_weekdays(preferences: str) -> List[str]:
        prefs = (preferences or "").lower()
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        return [
            d for d in days
            if re.search(rf"\bno\s+(?:meetings?\s+(?:on\s+)?)?{d}s?\b|\bno\s+{d}s?\s+meetings?\b|\bnot\s+(?:available\s+)?on\s+{d}s?\b", prefs)
        ]

    def critique(self, merged: Dict[str, Any], base_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Lab 8 Critique: Evaluates combined state for logical, environmental, or constraint discrepancies.
        Participant preferences come from long-term memory (RAG context) when the workflow retrieved them.
        """
        critique_notes = []
        is_consistent = True
        rag_prefs = ((base_state.get("rag_context") or {}).get("participant_preferences") or {})

        slot = merged.get("scheduling_slot") or {}
        start_str = slot.get("start") or base_state.get("target_start_time") or ""

        day_name = ""
        if start_str:
            try:
                dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                day_name = dt.strftime("%A").lower()
            except Exception:
                day_name = "friday"

        hour = None
        if start_str:
            try:
                hour = datetime.fromisoformat(start_str.replace("Z", "+00:00")).hour
            except Exception:
                hour = None
        for p in merged.get("participants", []):
            memory = rag_prefs.get((p.get("email") or "").lower()) or {}
            prefs_text = memory.get("preferences") or p.get("preferences") or ""
            source = "long-term memory" if memory else "directory profile"
            prefs = prefs_text.lower()
            blocked = self._blocked_weekdays(prefs)
            if day_name and day_name in blocked:
                critique_notes.append(
                    f"Participant {p['name']} has a policy against {day_name.capitalize()} meetings ({source}: \"{prefs_text}\")."
                )
                is_consistent = False
            if "morning" in prefs and hour is not None and hour >= 12:
                critique_notes.append(f"Participant {p['name']} prefers morning meetings, but an afternoon slot is proposed ({source}).")
            if "afternoon" in prefs and hour is not None and hour < 12:
                critique_notes.append(f"Participant {p['name']} prefers afternoon meetings, but a morning slot is proposed ({source}).")

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
            if sc.get("severity") == "RESOLVED":
                conflicts.append({
                    "conflict_id": f"conf-{uuid.uuid4().hex[:6]}",
                    "conflict": sc.get("conflict", "Time slot conflict"),
                    "source_agents": ["Scheduling & Availability Agent"],
                    "conflicting_values": {"participant": sc.get("participant"), "field": sc.get("field")},
                    "evidence": sc.get("conflict"),
                    "resolution": sc.get("resolution", "Alternative slot proposed."),
                    "status": "RESOLVED"
                })
                continue
            if sc.get("field") in {"working_hours", "working_days"}:
                resolution = "Outside declared working time; confirm with the participant or edit the slot before approving."
            elif is_warning:
                resolution = "Calendar access is unverified; review with the participant before confirming."
            else:
                resolution = "Find an alternative slot or reschedule to another day."
            conflicts.append({
                "conflict_id": f"conf-{uuid.uuid4().hex[:6]}",
                "conflict": sc.get("conflict", "Time slot conflict"),
                "source_agents": ["Scheduling & Availability Agent"],
                "conflicting_values": {"participant": sc.get("participant"), "field": sc.get("field")},
                "evidence": sc.get("conflict"),
                "resolution": resolution,
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
                    "resource": f"{resource_data.get('room_name')} booking is PENDING facility review"
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
