import re
import uuid
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

from backend.runtime.agent_interface import BaseAgent, AgentOutput
from backend.agents.participant_agent import ParticipantAgent, normalize_participant
from backend.agents.scheduling_agent import SchedulingAgent
from backend.agents.agenda_agent import AgendaAgent
from backend.connectors import groq_client
from backend.skills.agenda_skill import extract_agenda_points, normalize_agenda_points
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

    # ----------------- Request understanding (explicit facts only, never defaults) -----------------

    MANDATORY_FIELDS = ["participants", "title", "purpose", "date", "start_time", "duration", "mode", "venue"]
    GENERIC_TITLES = {"meeting", "call", "session", "discussion", "meet", "sync", "catch up", "catch-up", "chat",
                      "online meeting", "offline meeting", "virtual meeting", "in-person meeting", "in person meeting"}
    WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    VENUE_WORDS = r"(?:room|hall|auditorium|lab|cafeteria|canteen|office|cabin|lounge|boardroom|floor|building|campus|block|library|studio)"

    def parse_natural_language_request(self, text: str) -> Dict[str, Any]:
        """
        Extracts ONLY what the organizer explicitly stated: attendees, title, purpose, date, time, duration,
        mode, venue, equipment and agenda points. Anything not stated is reported as missing (with a question)
        instead of being filled with a default. Uses the Groq LLM when configured, with the deterministic parser
        as fallback and cross-check.
        """
        regex_result = self._regex_parse(text)
        if groq_client.is_configured() and not settings.DEMO_MODE:
            try:
                today = datetime.now(self._tz()).strftime("%A %Y-%m-%d")
                prompt = (
                    "You are a meeting extraction engine. Extract ONLY facts explicitly stated in the request. "
                    "Use null for anything not stated — never guess or invent. Return JSON with keys: "
                    "participants (list of names/emails), title (short meeting name or null), "
                    "purpose (why the meeting is held, or null — must not just repeat the title), "
                    f"date (YYYY-MM-DD, resolving relative dates from today = {today}, or null), "
                    "start_time (HH:MM 24h or null), end_time (HH:MM 24h or null), duration_minutes (int or null), "
                    "mode ('ONLINE', 'OFFLINE' or null), venue (string or null), equipment (string or null), "
                    "agenda_points (every agenda item the organizer listed, one string per item, in the order given, "
                    "worded as the organizer wrote it, without list numbers or bullets; [] if no agenda was given). "
                    f"Request: '{text}'"
                )
                llm = groq_client.chat_json("You extract meeting details from a request and reply with JSON only.", prompt)
                return self._normalize_llm_parse(llm, regex_result)
            except Exception as e:
                logger.warning(f"AI extraction failed ({e}), using regex parser.")
        return regex_result

    def _normalize_llm_parse(self, llm: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
        """Use the LLM's explicit values; fall back to the deterministic parser per field; never fill defaults."""
        parsed = dict(fallback)

        def pick(key, value):
            if value not in (None, "", []):
                parsed[key] = value

        if isinstance(llm.get("participants"), list) and llm["participants"]:
            parsed["participants"] = self._split_participant_names(llm["participants"])
        pick("title", (llm.get("title") or "").strip() or None)
        pick("purpose", (llm.get("purpose") or "").strip() or None)
        date_value = str(llm.get("date") or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value) and not fallback.get("ambiguities", {}).get("date"):
            parsed["date"] = date_value
        for key in ("start_time", "end_time"):
            value = str(llm.get(key) or "").strip()
            if re.fullmatch(r"\d{1,2}:\d{2}", value):
                h, m = value.split(":")
                parsed[key.replace("_time", "_clock")] = f"{int(h):02d}:{int(m):02d}"
                parsed.get("ambiguities", {}).pop("start_time", None)
        if llm.get("duration_minutes"):
            try:
                parsed["duration_minutes"] = int(llm["duration_minutes"])
            except (TypeError, ValueError):
                pass
        mode = str(llm.get("mode") or "").upper()
        if mode in {"ONLINE", "OFFLINE"} and not fallback.get("ambiguities", {}).get("mode"):
            parsed["mode"] = mode
        pick("room_name", (llm.get("venue") or "").strip() or None)
        pick("equipment", (llm.get("equipment") or "").strip() or None)
        if isinstance(llm.get("agenda_points"), list):
            llm_points = normalize_agenda_points([str(a) for a in llm["agenda_points"]])
            # The model may merge or drop items; the rule-based extraction of the same text must never lose points.
            if len(llm_points) >= len(fallback.get("agenda_points") or []) and llm_points:
                parsed["agenda_points"] = llm_points
        parsed["parser"] = "groq"
        return self.complete_details(parsed)

    @staticmethod
    def _tz():
        from zoneinfo import ZoneInfo
        try:
            return ZoneInfo(settings.MEETING_TIMEZONE)
        except Exception:
            return timezone.utc

    @staticmethod
    def _to_utc_iso(date_str: str, hour: int, minute: int) -> str:
        """Times in a request are wall-clock times in MEETING_TIMEZONE; the workflow stores UTC."""
        tz = MeetingCoordinator._tz()
        local = datetime.fromisoformat(f"{date_str}T00:00:00").replace(tzinfo=tz) + timedelta(hours=hour, minutes=minute)
        return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _match_room(text_lower: str) -> str:
        """Match a room from the configured catalog by its full name, base name, or the alias in parentheses."""
        from database.seed.seed_data import SEED_ROOMS
        for room in SEED_ROOMS:
            name = room["name"].lower()
            base = name.split(" (")[0]
            alias = name[name.find("(") + 1:name.find(")")] if "(" in name and ")" in name else ""
            if name in text_lower or base in text_lower or (alias and alias in text_lower):
                return room["name"]
        return ""

    @staticmethod
    def _title_case(value: str) -> str:
        return " ".join(w if (any(c.isdigit() for c in w) or w.isupper()) else w.capitalize() for w in value.split())

    def _extract_venue(self, text: str) -> str:
        catalog = self._match_room(text.lower())
        if catalog:
            return catalog
        explicit = re.search(r"\bvenue\s*(?:is|:|=)\s*([^.;,\n]+)", text, re.IGNORECASE)
        if explicit:
            return explicit.group(1).strip()
        found = re.search(
            rf"\b(?:in|at)\s+(?:the\s+)?((?:[\w'&-]+\s+){{0,3}}?{self.VENUE_WORDS}(?:\s+(?-i:[A-Z0-9][\w-]*))?)\b",
            text, re.IGNORECASE,
        )
        return self._title_case(found.group(1).strip()) if found else ""

    def _extract_date(self, tl: str, today) -> Dict[str, Any]:
        if "day after tomorrow" in tl:
            return {"date": today + timedelta(days=2)}
        if re.search(r"\btomorrow\b", tl):
            return {"date": today + timedelta(days=1)}
        if re.search(r"\b(today|tonight)\b", tl):
            return {"date": today}
        iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", tl)
        if iso:
            try:
                return {"date": datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))).date()}
            except ValueError:
                return {"ambiguity": f"'{iso.group(0)}' is not a valid date."}
        months = "|".join(self.MONTHS)
        named = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({months})[a-z]*\b(?:,?\s+(\d{{4}}))?", tl) or \
            re.search(rf"\b({months})[a-z]*\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", tl)
        if named:
            g = named.groups()
            day, mon = (int(g[0]), g[1]) if g[0].isdigit() else (int(g[1]), g[0])
            year = int(g[2]) if g[2] else today.year
            try:
                candidate = datetime(year, self.MONTHS.index(mon[:3]) + 1, day).date()
            except ValueError:
                return {"ambiguity": "That date does not exist."}
            if candidate < today and not g[2]:
                candidate = candidate.replace(year=today.year + 1)
            return {"date": candidate}
        for idx, name in enumerate(self.WEEKDAYS):
            if re.search(rf"\b{name}\b", tl):
                upcoming = (idx - today.weekday()) % 7
                if re.search(rf"\bnext\s+{name}\b", tl):
                    first = today + timedelta(days=upcoming or 7)
                    return {"ambiguity": f"'Next {name.capitalize()}' could mean {first:%d %b} or {first + timedelta(days=7):%d %b}. Which date?"}
                if upcoming == 0 and not re.search(rf"\bthis\s+{name}\b", tl):
                    return {"ambiguity": f"Today is {name.capitalize()}. Do you mean today ({today:%d %b}) or {today + timedelta(days=7):%d %b}?"}
                return {"date": today + timedelta(days=upcoming)}
        if re.search(r"\b(next week|this week|later|soon|sometime|some time|next month)\b", tl):
            return {"ambiguity": "The date is not specific. Which date should the meeting be on?"}
        return {}

    @staticmethod
    def _to_24h(hour: int, minute: int, meridiem: Optional[str]) -> Optional[str]:
        if meridiem:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if meridiem == "pm" else 0)
        if hour > 23 or minute > 59:
            return None
        return f"{hour:02d}:{minute:02d}"

    def _extract_times(self, tl: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        tl = re.sub(r"\bnoon\b", "12 pm", tl)
        tl = re.sub(r"\bmidnight\b", "12 am", tl)
        rng = re.search(
            r"(?<![\d:-])(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:to|-|–|until|till|and)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", tl)
        if rng and (rng.group(3) or rng.group(6) or re.search(r"\b(from|between)\s+" + re.escape(rng.group(0)[:2].strip()), tl)
                    or int(rng.group(1)) > 12 or int(rng.group(4)) > 12):
            sh, sm, sap, eh, em, eap = int(rng.group(1)), int(rng.group(2) or 0), rng.group(3), int(rng.group(4)), int(rng.group(5) or 0), rng.group(6)
            if not sap and not eap and sh <= 12 and eh <= 12:
                out["ambiguity_time"] = f"Is {rng.group(0)} in the morning (AM) or afternoon/evening (PM)?"
                return out
            if not sap and eap:
                sap = eap
                start_24 = self._to_24h(sh, sm, sap)
                end_24 = self._to_24h(eh, em, eap)
                if start_24 and end_24 and start_24 >= end_24 and eap == "pm" and sh != 12:
                    sap = "am"  # "11 to 1 pm" -> 11 AM - 1 PM
            start_clock = self._to_24h(sh, sm, sap)
            end_clock = self._to_24h(eh, em, eap or sap)
            if start_clock and end_clock:
                out.update(start_clock=start_clock, end_clock=end_clock)
                return out
        single = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", tl) or \
            re.search(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", tl) or \
            re.search(r"(?<![\d:-])(\d{1,2}):(\d{2})()\b", tl)
        if single:
            hour, minute, meridiem = int(single.group(1)), int(single.group(2) or 0), single.group(3) or None
            if not meridiem and hour <= 12 and not single.group(2):
                out["ambiguity_time"] = f"Did you mean {hour} AM or {hour} PM?"
            else:
                clock = self._to_24h(hour, minute, meridiem)
                if clock:
                    out["start_clock"] = clock
        duration = re.search(r"\b(?:for\s+)?(\d+(?:\.\d+)?)\s*-?\s*(hours?|hrs?|minutes?|mins?)\b", tl)
        if duration:
            value = float(duration.group(1))
            out["duration_minutes"] = int(round(value * 60 if duration.group(2).startswith("h") else value))
        elif re.search(r"\b(half an hour|half-hour|30-minute)\b", tl):
            out["duration_minutes"] = 30
        elif re.search(r"\b(an|one) hour\b", tl):
            out["duration_minutes"] = 60
        return out

    def _regex_parse(self, text: str) -> Dict[str, Any]:
        t = " ".join(text.strip().split())
        tl = t.lower()
        today = datetime.now(self._tz()).date()
        ambiguities: Dict[str, str] = {}

        # Mode: only from explicit words (word boundaries: "meeting" is not "meet").
        online = re.search(r"\b(online|virtual(ly)?|video (call|meeting)|google meet|meet link|zoom|teams call|remote(ly)?)\b", tl)
        offline = re.search(r"\b(offline|in[- ]person|face[- ]to[- ]face|physical(ly)?|on[- ]?site)\b", tl)
        venue = self._extract_venue(t)
        if online and (offline or venue):
            mode, ambiguities["mode"] = None, "You mentioned both online and an in-person place. Which should it be?"
        elif online:
            mode = "ONLINE"
        elif offline or venue:
            mode = "OFFLINE"
        else:
            mode = None

        # Attendees
        participants = []
        with_match = re.search(
            r"\bwith\s+([A-Za-z0-9@\.\,\s_+\-]+?)(?=\s+(?:on|at|from|to|for|in|this|next|tomorrow|today|tonight|titled|called|named|about|regarding|via|over|between|it\s+should)\b|[.;](?:\s|$)|$)",
            t, re.IGNORECASE)
        if with_match:
            for part in re.split(r",|\band\b|&", with_match.group(1)):
                name = part.strip().strip(".")
                if name and name.lower() not in {"a meeting", "the team", "everyone", "me"}:
                    participants.append(name)

        # Title: explicit naming or the meeting type named in "schedule a <title> with ..."
        title = None
        named = re.search(r"\b(?:titled|called|named|title\s*(?:is|:))\s*[\"'“]?([^\"'”.,;]+?)[\"'”]?(?=\s+(?:with|on|at|for|from|to\s+discuss|tomorrow|today)\b|[.,;]|$)", t, re.IGNORECASE) \
            or re.search(r"[\"“]([^\"”]{3,80})[\"”]", t)
        if named:
            title = named.group(1).strip()
        else:
            lead = re.search(
                r"^\s*(?:please\s+|can you\s+|i want to\s+|i'd like to\s+)?(?:schedule|set up|setup|arrange|book|organi[sz]e|plan|create|fix)\s+(?:(?:a|an|the|my|our)\s+)?(.+?)\s+(?=with\b|on\b|at\b|for\b|tomorrow\b|today\b|next\b|this\b|from\b|between\b|in\b)",
                t, re.IGNORECASE)
            if lead:
                candidate = re.sub(r"\b(online|offline|virtual|in[- ]person|remote|quick|short|new)\b", "", lead.group(1), flags=re.IGNORECASE)
                candidate = re.sub(r"\b(meeting|call|session)\s*$", "", candidate.strip(), flags=re.IGNORECASE).strip()
                if candidate and candidate.lower() not in self.GENERIC_TITLES:
                    title = self._title_case(candidate)

        # Purpose: why the meeting is held (not the title)
        purpose = None
        purpose_match = re.search(
            r"\b(?:purpose\s*(?:is|:)\s*(?:to\s+)?|(?:in order\s+)?to\s+(discuss|review|plan|finali[sz]e|decide(?:\s+on)?|go over|present|align on|brainstorm|demo|walk through|kick off|evaluate|prepare)\s+|for\s+discussing\s+|about\s+|regarding\s+)(.+?)(?=\.\s|\.$|;|\s+agenda\b|\s+(?:it|this)\s+should\b|$)",
            t, re.IGNORECASE)
        if purpose_match:
            verb = (purpose_match.group(1) or "").strip()
            body = purpose_match.group(2).strip().rstrip(".")
            if purpose_match.group(0).lower().startswith(("about", "regarding", "for discussing")):
                verb = "discuss"
            purpose = f"{verb} {body}".strip() if verb else body
            purpose = purpose[:1].upper() + purpose[1:]
            if title and purpose.lower() == title.lower():
                purpose = None

        # Date and time (relative dates resolved in MEETING_TIMEZONE)
        date_info = self._extract_date(tl, today)
        date_value = date_info.get("date")
        if date_info.get("ambiguity"):
            ambiguities["date"] = date_info["ambiguity"]
        elif date_value and date_value < today:
            ambiguities["date"] = f"{date_value:%d %b %Y} is in the past. Which date should the meeting be on?"
            date_value = None
        times = self._extract_times(tl)
        if times.get("ambiguity_time"):
            ambiguities["start_time"] = times["ambiguity_time"]

        equipment = ""
        equipment_match = re.search(r"(?:required equipment|equipment(?: requirements)?):\s*(.+?)(?=\.|$)", t, re.IGNORECASE)
        if equipment_match:
            equipment = equipment_match.group(1).strip()

        # The organizer's own agenda, verbatim and in order (numbered, bulleted, one per line, or separated by ; or ,).
        agenda_points = extract_agenda_points(text)

        parsed = {
            "participants": participants,
            "title": title,
            "purpose": purpose,
            "date": date_value.strftime("%Y-%m-%d") if date_value else None,
            "start_clock": times.get("start_clock"),
            "end_clock": times.get("end_clock"),
            "duration_minutes": times.get("duration_minutes"),
            "mode": mode,
            "room_name": venue,
            "equipment": equipment,
            "agenda_points": agenda_points,
            "ambiguities": ambiguities,
            "parser": "rules",
        }
        return self.complete_details(parsed)

    @staticmethod
    def _split_participant_names(values):
        """Keep each attendee as an independent name if extraction grouped a list into one string."""
        names = []
        for value in values:
            text = str(value).strip()
            if "@" not in text:
                names.extend(part.strip() for part in re.split(r",|\band\b|&", text, flags=re.IGNORECASE) if part.strip())
            elif text:
                names.append(text)
        return names

    QUESTIONS = {
        "participants": ("Who should attend? (Enter participant names)", "attendees"),
        "title": ("What should the meeting be called? (a short name, e.g. 'Q3 Project Review')", "text"),
        "purpose": ("What is the purpose of the meeting — what should it achieve?", "text"),
        "date": ("Which date should the meeting be on?", "date"),
        "start_time": ("What time should it start?", "time"),
        "duration": ("How long should the meeting be?", "duration"),
        "mode": ("Should the meeting be online or in person (offline)?", "mode"),
        "venue": ("Where will the in-person meeting take place?", "text"),
        "venue_coordinator_email": ("What is the venue coordinator's email address?", "email"),
    }

    def complete_details(self, parsed: Dict[str, Any]) -> Dict[str, Any]:
        """Derive UTC start/end from what is known and list what is still missing (with targeted questions)."""
        parsed = dict(parsed)
        ambiguities = dict(parsed.get("ambiguities") or {})
        start_clock, end_clock, duration = parsed.get("start_clock"), parsed.get("end_clock"), parsed.get("duration_minutes")
        if start_clock and end_clock and not duration:
            sh, sm = map(int, start_clock.split(":"))
            eh, em = map(int, end_clock.split(":"))
            duration = (eh * 60 + em) - (sh * 60 + sm)
            if duration <= 0:
                duration += 24 * 60  # crosses midnight
            parsed["duration_minutes"] = duration
        parsed["start_time"] = parsed["end_time"] = None
        if parsed.get("date") and start_clock and duration:
            sh, sm = map(int, start_clock.split(":"))
            parsed["start_time"] = self._to_utc_iso(parsed["date"], sh, sm)
            parsed["end_time"] = self._to_utc_iso(parsed["date"], sh, sm + int(duration))
            if datetime.fromisoformat(parsed["start_time"].replace("Z", "+00:00")) < datetime.now(timezone.utc):
                ambiguities["start_time"] = "That time has already passed. Please choose a later time."
                parsed["start_time"] = parsed["end_time"] = None

        # Normalize participants list
        raw_participants = parsed.get("participants") or []
        normalized_participants = []
        for p in raw_participants:
            norm = normalize_participant(p)
            if norm["name"] or norm["email"]:
                normalized_participants.append(norm)
        parsed["participants"] = normalized_participants

        has_participants = len(normalized_participants) > 0
        all_have_emails = has_participants and all(bool(p.get("email")) for p in normalized_participants)

        present = {
            "participants": has_participants and all_have_emails,
            "title": bool(parsed.get("title")),
            "purpose": bool(parsed.get("purpose")),
            "date": bool(parsed.get("date")) and "date" not in ambiguities,
            "start_time": bool(start_clock) and "start_time" not in ambiguities,
            "duration": bool(parsed.get("duration_minutes")),
            "mode": parsed.get("mode") in {"ONLINE", "OFFLINE"} and "mode" not in ambiguities,
            "venue": parsed.get("mode") != "OFFLINE" or bool(parsed.get("room_name")),
        }
        missing = [f for f in self.MANDATORY_FIELDS if not present[f]]
        parsed["ambiguities"] = ambiguities

        questions = []
        effective_missing = []
        for f in missing:
            if f == "participants":
                if not has_participants:
                    effective_missing.append("participants")
                    questions.append({
                        "field": "participants",
                        "question": self.QUESTIONS["participants"][0],
                        "input": self.QUESTIONS["participants"][1],
                        "note": ambiguities.get("participants"),
                    })
                else:
                    # Separate email input field for each identified participant
                    for idx, p in enumerate(normalized_participants, start=1):
                        if not p.get("email"):
                            field_key = f"participant_email_{p['name']}"
                            effective_missing.append(field_key)
                            questions.append({
                                "field": field_key,
                                "participant_name": p["name"],
                                "participant_index": idx,
                                "question": f"Participant {idx} – {p['name']}",
                                "placeholder": f"Enter {p['name']}'s email address",
                                "input": "email",
                                "note": None,
                            })
            elif f in self.QUESTIONS:
                effective_missing.append(f)
                questions.append({
                    "field": f,
                    "question": self.QUESTIONS[f][0],
                    "input": self.QUESTIONS[f][1],
                    "note": ambiguities.get(f),
                })

        if parsed.get("mode") == "OFFLINE" and not parsed.get("venue_coordinator_email"):
            effective_missing.append("venue_coordinator_email")
            questions.append({
                "field": "venue_coordinator_email",
                "question": self.QUESTIONS["venue_coordinator_email"][0],
                "input": "email",
                "placeholder": "Enter the venue coordinator's email address",
                "participant_name": "venue coordinator",
            })

        parsed["missing"] = effective_missing
        parsed["questions"] = questions
        return parsed

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
        Conflict Detection & Resolution:
        Records Conflict, Source Agents, Conflicting Values, Evidence, and Resolution.
        """
        conflicts = []
        resource_data = merged.get("resource_info") or {}

        # 1. Direct Scheduling Conflicts (Calendar / Working Hours)
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

        # 4. Offline venue conflict: only a conflicting booking already in this app is relevant here.
        venue_check = merged.get("venue_check") or {}
        if resource_data.get("mode") == "OFFLINE" and venue_check.get("status") == "BUSY":
            conflicts.append({
                "conflict_id": f"conf-{uuid.uuid4().hex[:6]}",
                "conflict": f"Venue not available: {venue_check.get('reason')}",
                "source_agents": ["Resource/Room Agent"],
                "conflicting_values": {"venue": venue_check.get("venue"), "field": "venue_availability"},
                "evidence": venue_check.get("reason"),
                "resolution": "Change the time or the venue before approving.",
                "status": "UNRESOLVED_BLOCKER",
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
