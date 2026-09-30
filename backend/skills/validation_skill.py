from typing import Dict, Any, List
from backend.skills.base import BaseSkill, SkillMetadata
import logging

logger = logging.getLogger("validation_skill")

class ValidationSkill(BaseSkill):
    @property
    def metadata(self) -> SkillMetadata:
        return SkillMetadata(
            name="Validation Skill",
            purpose="Rigorously validate proposed meeting plans against participant constraints, working hours, room capacities, and completeness before human approval.",
            instructions=(
                "1. Check that at least one valid participant is identified.\n"
                "2. Flag unverified external participants.\n"
                "3. Ensure proposed time falls within participants' declared working hours.\n"
                "4. For OFFLINE meetings, confirm room capacity >= participant count.\n"
                "5. For ONLINE meetings, verify Google Meet link feasibility.\n"
                "6. Output comprehensive PASS/WARNING/FAIL decision with clear issue items."
            ),
            inputs={
                "meeting_plan": "dict - Candidate meeting specification",
                "participants": "List[dict] - Verified participant profile list",
                "mode": "str - ONLINE or OFFLINE",
                "room_details": "dict - Optional room booking details for offline meetings"
            },
            outputs={
                "status": "str - PASS, WARNING, or FAIL",
                "errors": "List[str] - Fatal blockers preventing execution",
                "warnings": "List[str] - Non-fatal items requiring human notice",
                "can_proceed_to_approval": "bool - Whether plan is ready for human approval gate"
            },
            constraints=[
                "Unknown participants with unresolved identities cause validation FAIL.",
                "Ambiguous participants without selection cause validation FAIL.",
                "Offline meetings without room designation cause validation FAIL."
            ],
            examples=[
                {
                    "input": {
                        "mode": "ONLINE",
                        "participants": [{"name": "Alice Chen", "status": "AVAILABLE"}]
                    },
                    "output": {
                        "status": "PASS",
                        "errors": [],
                        "warnings": [],
                        "can_proceed_to_approval": True
                    }
                }
            ]
        )

    def run(self, inputs: Dict[str, Any], context: Dict[str, Any] = None) -> Dict[str, Any]:
        plan = inputs.get("meeting_plan", {})
        participants = inputs.get("participants", [])
        mode = (inputs.get("mode") or plan.get("mode", "ONLINE")).upper()
        room_details = inputs.get("room_details", {})

        errors: List[str] = []
        warnings: List[str] = []

        # 1. Participant count and resolution
        if not participants:
            errors.append("No participants were identified or provided for this meeting.")

        has_unknown = False
        has_ambiguous = False
        for p in participants:
            if p.get("match_type") == "UNKNOWN" or p.get("is_unknown"):
                errors.append(f"Participant '{p.get('name')}' is not registered in the system. Email or registration required.")
                has_unknown = True
            elif p.get("match_type") == "AMBIGUOUS" or p.get("is_ambiguous"):
                errors.append(f"Participant '{p.get('name')}' is ambiguous. Please select the correct person.")
                has_ambiguous = True
            elif p.get("is_external"):
                warnings.append(f"Participant '{p.get('name')}' is external ({p.get('email')}). Availability could not be verified.")

        # 2. Time & Date validation
        start_time = plan.get("scheduled_start") or plan.get("start_time")
        if not start_time:
            errors.append("No scheduled start time has been specified.")

        # 3. Mode & Resource validation
        if mode == "OFFLINE":
            room_name = plan.get("room_name") or room_details.get("room_name")
            if not room_name:
                errors.append("Offline meeting requires a designated physical room or auditorium.")
            else:
                capacity = room_details.get("capacity", 10)
                if len(participants) > capacity:
                    warnings.append(f"Room capacity ({capacity}) is less than total participants ({len(participants)}).")

        # 4. Status determination
        if errors:
            status = "FAIL"
            can_proceed = False
        elif warnings:
            status = "WARNING"
            can_proceed = True
        else:
            status = "PASS"
            can_proceed = True

        return {
            "status": status,
            "errors": errors,
            "warnings": warnings,
            "can_proceed_to_approval": can_proceed
        }
