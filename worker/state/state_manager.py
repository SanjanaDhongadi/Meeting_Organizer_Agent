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
        return event_id in self._processed_events

    def mark_event_processed(self, event_id: str):
        self._processed_events.add(event_id)

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

    def update_room_response(self, meeting_id: str, room_name: str, status: str, notes: str = "") -> bool:
        db = SessionLocal()
        try:
            rb = db.query(RoomBooking).filter(
                RoomBooking.meeting_id == meeting_id,
                RoomBooking.room_name == room_name
            ).first()
            if rb:
                rb.status = status
                rb.response_time = datetime.utcnow().isoformat()
                rb.notes = notes
            else:
                rb = RoomBooking(
                    meeting_id=meeting_id,
                    room_name=room_name,
                    status=status,
                    response_time=datetime.utcnow().isoformat(),
                    notes=notes
                )
                db.add(rb)
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
