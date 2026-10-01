import json
import logging
from typing import Dict, Any, Optional
from datetime import datetime
from backend.app.db import SessionLocal, Meeting, MeetingParticipant, RoomBooking, AuditLog

logger = logging.getLogger("state_manager")

class StateManager:
    """
    Worker State Manager:
    - Loads persisted workflow state from database
    - Saves updated state transitions
    - Prevents duplicate executions (idempotency keying)
    - Resumes paused workflows on the same meeting entity
    """
    def __init__(self):
        self._processed_events = set()

    def is_event_processed(self, event_id: str) -> bool:
        if event_id in self._processed_events:
            return True
        # Idempotency is persisted so the API process and the worker process share it.
        db = SessionLocal()
        try:
            return db.query(AuditLog).filter(
                AuditLog.action == "EVENT_PROCESSED", AuditLog.tool == event_id
            ).first() is not None
        finally:
            db.close()

    def mark_event_processed(self, event_id: str, meeting_id: Optional[str] = None, source: str = ""):
        self._processed_events.add(event_id)
        db = SessionLocal()
        try:
            db.add(AuditLog(meeting_id=meeting_id, agent="Worker StateManager", action="EVENT_PROCESSED",
                            tool=event_id, status="SUCCESS", details=json.dumps({"source": source})))
            db.commit()
        except Exception as e:
            logger.error(f"Failed to persist processed event {event_id}: {e}")
            db.rollback()
        finally:
            db.close()

    def load_meeting(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        db = SessionLocal()
        try:
            m = db.query(Meeting).filter(Meeting.id == meeting_id).first()
            if not m:
                return None
            
            participants = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id).all()
            room_bookings = db.query(RoomBooking).filter(RoomBooking.meeting_id == meeting_id).all()
            
            data = m.to_dict()
            data["participants"] = [p.to_dict() for p in participants]
            data["room_bookings"] = [rb.to_dict() for rb in room_bookings]
            return data
        finally:
            db.close()

    def update_meeting_status(self, meeting_id: str, new_status: str, audit_action: str = "STATUS_UPDATE", notes: str = "") -> bool:
        db = SessionLocal()
        try:
            m = db.query(Meeting).filter(Meeting.id == meeting_id).first()
            if not m:
                logger.error(f"Cannot update status for missing meeting {meeting_id}")
                return False

            old_status = m.status
            m.status = new_status
            if notes:
                m.approval_notes = f"{m.approval_notes or ''}\n{notes}".strip()

            audit = AuditLog(
                meeting_id=meeting_id,
                agent="Worker StateManager",
                action=audit_action,
                status="SUCCESS",
                details=json.dumps({"old_status": old_status, "new_status": new_status, "notes": notes})
            )
            db.add(audit)
            db.commit()
            logger.info(f"[StateManager] Meeting {meeting_id} transitioned: {old_status} -> {new_status}")
            return True
        except Exception as e:
            logger.error(f"Failed to update meeting status: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    def claim_status(self, meeting_id: str, from_status: str, to_status: str) -> bool:
        """Atomically move a meeting from one status to another; only one caller can win the claim."""
        db = SessionLocal()
        try:
            updated = db.query(Meeting).filter(
                Meeting.id == meeting_id, Meeting.status == from_status
            ).update({Meeting.status: to_status}, synchronize_session=False)
            db.commit()
            return updated == 1
        except Exception as e:
            logger.error(f"Failed to claim meeting {meeting_id} ({from_status} -> {to_status}): {e}")
            db.rollback()
            return False
        finally:
            db.close()

    def reset_participant_responses(self, meeting_id: str) -> None:
        db = SessionLocal()
        try:
            for p in db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == meeting_id).all():
                p.response_status = "PENDING"
                p.response_time = None
                p.notes = ""
            db.commit()
        finally:
            db.close()

    def update_participant_response(self, meeting_id: str, email: str, response_status: str, notes: str = "") -> bool:
        db = SessionLocal()
        try:
            p = db.query(MeetingParticipant).filter(
                MeetingParticipant.meeting_id == meeting_id,
                MeetingParticipant.email.ilike(email)
            ).first()
            if p:
                p.response_status = response_status
                p.response_time = datetime.utcnow().isoformat()
                p.notes = notes
                db.commit()
                logger.info(f"[StateManager] Participant {email} in meeting {meeting_id} set to {response_status}")
                return True
            else:
                logger.warning(f"Ignoring response from unlisted participant {email} for meeting {meeting_id}")
                return False
        except Exception as e:
            logger.error(f"Failed to record participant response: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    def update_room_response(self, meeting_id: str, room_name: str, status: str, notes: str = "",
                             capacity: Optional[int] = None, equipment: Optional[str] = None) -> bool:
        db = SessionLocal()
        try:
            rb = db.query(RoomBooking).filter(
                RoomBooking.meeting_id == meeting_id,
                RoomBooking.room_name == room_name
            ).first()
            if rb:
                rb.status = status
                rb.response_time = datetime.utcnow().isoformat()
                # Keep the history (including the request reference) instead of overwriting it.
                rb.notes = f"{rb.notes}\n{notes}".strip() if rb.notes and notes else (notes or rb.notes)
            else:
                rb = RoomBooking(
                    meeting_id=meeting_id,
                    room_name=room_name,
                    status=status,
                    response_time=datetime.utcnow().isoformat(),
                    notes=notes
                )
                db.add(rb)
            if capacity is not None:
                rb.capacity = capacity
            if equipment is not None:
                rb.equipment_requirements = equipment
            db.commit()
            logger.info(f"[StateManager] Room {room_name} for meeting {meeting_id} set to {status}")
            return True
        except Exception as e:
            logger.error(f"Failed to update room booking response: {e}")
            db.rollback()
            return False
        finally:
            db.close()

state_manager = StateManager()
