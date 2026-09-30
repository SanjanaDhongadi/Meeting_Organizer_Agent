from typing import Dict, Any, List
from datetime import datetime, timedelta
from backend.skills.base import BaseSkill, SkillMetadata
from backend.tools.calendar_tools import CheckCalendarAvailabilityTool, FindCommonSlotsTool
import logging

logger = logging.getLogger("scheduling_skill")

class MeetingSchedulingSkill(BaseSkill):
    @property
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="Meeting Scheduling Skill",
            purpose="Determine mutually compatible meeting time slots by verifying calendar availability and participant working hour constraints.",
            instructions=(
                "1. Gather verified participant emails and requested target date/time.\n"
                "2. Check participant working hours and time zones.\n"
                "3. Query CalendarService via check_calendar_availability tool.\n"
                "4. If requested slot is busy, invoke find_common_slots to locate viable alternatives.\n"
                "5. Never use RAG or cosine similarity to determine calendar availability."
            ),
            inputs={
                "participant_emails": "List[str] - Emails of all meeting attendees",
                "target_date": "str - Desired date (YYYY-MM-DD)",
                "target_start_time": "str - Optional specific start ISO time",
                "target_end_time": "str - Optional specific end ISO time",
                "duration_minutes": "int - Duration in minutes (default 30)",
                "profiles": "List[dict] - Detailed participant profiles with working hours"
            },
            outputs={
                "is_slot_available": "bool - Whether the preferred slot is completely open",
                "selected_slot": "dict - Confirmed start and end ISO timestamps",
                "alternative_slots": "List[dict] - Alternative candidate slots if conflict exists",
                "participant_statuses": "dict - Free/busy status per participant",
                "conflicts": "List[dict] - Any identified scheduling conflicts"
            },
            constraints=[
                "Actual availability must come strictly from CalendarService, never RAG embeddings.",
                "Participants cannot be scheduled outside their declared working hours.",
                "External participants must be explicitly flagged with 'Availability could not be verified'."
            ],
            examples=[
                {
                    "input": {
                        "participant_emails": ["alice.chen@example.com", "bob.smith@example.com"],
                        "target_date": "2026-10-02",
                        "target_start_time": "2026-10-02T15:00:00Z",
                        "target_end_time": "2026-10-02T16:00:00Z",
                        "duration_minutes": 60
                    },
                    "output": {
                        "is_slot_available": True,
                        "selected_slot": {"start": "2026-10-02T15:00:00Z", "end": "2026-10-02T16:00:00Z"},
                        "conflicts": []
                    }
                }
            ]
        )

    def run(self, inputs: Dict[str, Any], context: Dict[str, Any] = None) -> Dict[str, Any]:
        emails = inputs.get("participant_emails", [])
        date_str = inputs.get("target_date", datetime.utcnow().strftime("%Y-%m-%d"))
        start_time = inputs.get("target_start_time")
        end_time = inputs.get("target_end_time")
        duration = inputs.get("duration_minutes", 30)

        # If specific slot provided, verify it directly
        if start_time and end_time:
            check_tool = CheckCalendarAvailabilityTool()
            check_res = check_tool.execute(emails=emails, start_time=start_time, end_time=end_time)
            
            cal_data = check_res.data or {}
            all_avail = cal_data.get("all_available", False)
            details = cal_data.get("details", {})
            
            conflicts = []
            for em, st in details.items():
                if st.get("status") == "BUSY":
                    conflicts.append({
                        "agent": "Scheduling & Availability Agent",
                        "field": "time_slot",
                        "participant": em,
                        "conflict": st.get("reason", "Busy at requested time"),
                        "severity": "HIGH"
                    })
                elif st.get("status") == "UNVERIFIED":
                    conflicts.append({
                        "agent": "Scheduling & Availability Agent",
                        "field": "external_participant",
                        "participant": em,
                        "conflict": "Availability could not be verified",
                        "severity": "WARNING"
                    })

            alternatives = []
            if not all_avail:
                slot_tool = FindCommonSlotsTool()
                alt_res = slot_tool.execute(emails=emails, date_str=date_str, duration_minutes=duration)
                alternatives = alt_res.data.get("common_slots", []) if alt_res.data else []

            return {
                "is_slot_available": all_avail,
                "selected_slot": {"start": start_time, "end": end_time} if all_avail else (alternatives[0] if alternatives else None),
                "alternative_slots": alternatives,
                "participant_statuses": details,
                "conflicts": conflicts
            }
        else:
            # Slot search
            slot_tool = FindCommonSlotsTool()
            res = slot_tool.execute(emails=emails, date_str=date_str, duration_minutes=duration)
            slots = res.data.get("common_slots", []) if res.data else []
            selected = slots[0] if slots else None
            return {
                "is_slot_available": bool(selected),
                "selected_slot": selected,
                "alternative_slots": slots[1:] if len(slots) > 1 else [],
                "participant_statuses": {},
                "conflicts": [] if selected else [{"agent": "Scheduling Agent", "field": "time_slot", "conflict": "No common slots available on date", "severity": "HIGH"}]
            }
