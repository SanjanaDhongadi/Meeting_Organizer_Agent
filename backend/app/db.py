import json
import hashlib
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
import numpy as np
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Index, func
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from backend.app.config import settings

logger = logging.getLogger("db")

Base = declarative_base()

class Employee(Base):
    __tablename__ = "employees"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(150), nullable=False)
    email = Column(String(150), unique=True, nullable=False, index=True)
    __table_args__ = (Index("uq_employees_email_lower", func.lower(email), unique=True),)
    designation = Column(String(150), nullable=False)
    department = Column(String(100), nullable=False)
    working_days = Column(String(100), default="Monday,Tuesday,Wednesday,Thursday,Friday")
    working_hours_start = Column(String(10), default="09:00:00")
    working_hours_end = Column(String(10), default="17:00:00")
    timezone = Column(String(50), default="America/New_York")
    meeting_preferences = Column(Text, default="Prefers 30-min meetings, online preferred")
    preferred_duration = Column(Integer, default=30)
    mode_preference = Column(String(30), default="Online")
    location = Column(String(200), default="HQ Tech Park, Building A")
    other_info = Column(Text, default="")
    google_calendar_connected = Column(Boolean, default=False)
    google_tokens = Column(Text, default="{}")
    embedding = Column(Text, default="[]")  # Serialized vector representation
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "employee_id": self.employee_id,
            "name": self.name,
            "email": self.email,
            "designation": self.designation,
            "department": self.department,
            "working_days": self.working_days,
            "working_hours_start": self.working_hours_start,
            "working_hours_end": self.working_hours_end,
            "timezone": self.timezone,
            "meeting_preferences": self.meeting_preferences,
            "preferred_duration": self.preferred_duration,
            "mode_preference": self.mode_preference,
            "location": self.location,
            "other_info": self.other_info,
            "google_calendar_connected": self.google_calendar_connected,
            "has_embedding": bool(self.embedding and self.embedding != "[]"),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

class Meeting(Base):
    __tablename__ = "meetings"

    id = Column(String(64), primary_key=True)
    title = Column(String(255), nullable=False)
    raw_request = Column(Text, nullable=False)
    status = Column(String(50), default="DRAFT", index=True)
    mode = Column(String(30), default="ONLINE")
    scheduled_start = Column(String(50), nullable=True)
    scheduled_end = Column(String(50), nullable=True)
    duration_minutes = Column(Integer, default=30)
    purpose = Column(Text, default="")
    agenda = Column(Text, default="")
    room_name = Column(String(100), default="")
    meet_url = Column(String(255), default="")
    calendar_event_id = Column(String(255), default="")
    approval_status = Column(String(30), default="PENDING")
    approval_notes = Column(Text, default="")
    parsed_details = Column(Text, default="{}")
    conflicts = Column(Text, default="[]")
    critique_notes = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "raw_request": self.raw_request,
            "status": self.status,
            "mode": self.mode,
            "scheduled_start": self.scheduled_start,
            "scheduled_end": self.scheduled_end,
            "duration_minutes": self.duration_minutes,
            "purpose": self.purpose,
            "agenda": self.agenda,
            "room_name": self.room_name,
            "meet_url": self.meet_url,
            "calendar_event_id": self.calendar_event_id,
            "approval_status": self.approval_status,
            "approval_notes": self.approval_notes,
            "parsed_details": json.loads(self.parsed_details) if self.parsed_details else {},
            "conflicts": json.loads(self.conflicts) if self.conflicts else [],
            "critique_notes": self.critique_notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

class MeetingParticipant(Base):
    __tablename__ = "meeting_participants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    meeting_id = Column(String(64), ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    employee_id = Column(String(50), nullable=True)
    name = Column(String(150), nullable=False)
    email = Column(String(150), nullable=False)
    is_external = Column(Boolean, default=False)
    calendar_status = Column(String(30), default="UNVERIFIED")  # AVAILABLE, BUSY, UNVERIFIED
    response_status = Column(String(30), default="PENDING")     # PENDING, ACCEPTED, REJECTED
    response_time = Column(String(50), nullable=True)
    notes = Column(Text, default="")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "meeting_id": self.meeting_id,
            "employee_id": self.employee_id,
            "name": self.name,
            "email": self.email,
            "is_external": self.is_external,
            "calendar_status": self.calendar_status,
            "response_status": self.response_status,
            "response_time": self.response_time,
            "notes": self.notes,
        }

class RoomBooking(Base):
    __tablename__ = "room_bookings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    meeting_id = Column(String(64), ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    room_name = Column(String(100), nullable=False)
    capacity = Column(Integer, default=10)
    equipment_requirements = Column(Text, default="")
    status = Column(String(30), default="PENDING")  # PENDING, CONFIRMED, REJECTED
    response_time = Column(String(50), nullable=True)
    notes = Column(Text, default="")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "meeting_id": self.meeting_id,
            "room_name": self.room_name,
            "capacity": self.capacity,
            "equipment_requirements": self.equipment_requirements,
            "status": self.status,
            "response_time": self.response_time,
            "notes": self.notes,
        }

class OAuthPendingState(Base):
    __tablename__ = "oauth_pending_states"

    state = Column(String(512), primary_key=True)
    employee_id = Column(Integer, nullable=False)
    code_verifier = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    meeting_id = Column(String(64), index=True, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    agent = Column(String(100), nullable=False)
    action = Column(String(100), nullable=False)
    tool = Column(String(100), default="")
    status = Column(String(30), default="SUCCESS")  # SUCCESS, WARNING, ERROR, WAITING
    error = Column(Text, default="")
    approval_status = Column(String(30), default="")
    details = Column(Text, default="{}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "meeting_id": self.meeting_id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "agent": self.agent,
            "action": self.action,
            "tool": self.tool,
            "status": self.status,
            "error": self.error,
            "approval_status": self.approval_status,
            "details": json.loads(self.details) if self.details else {},
        }

# Engine & Session Setup
database_url = settings.DATABASE_URL
connect_args = {}
if database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

try:
    engine = create_engine(database_url, connect_args=connect_args)
    Base.metadata.create_all(bind=engine)
    logger.info(f"Database connected using: {database_url}")
except Exception as e:
    logger.warning(f"Could not connect to {database_url} ({e}). Falling back to local SQLite.")
    engine = create_engine("sqlite:///./meeting_organizer.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Vector Similarity / RAG Helpers
def compute_simple_embedding(text: str) -> List[float]:
    """
    Computes a deterministic normalized semantic representation for local RAG
    when external embedding APIs are not configured.
    """
    # 64-dimensional feature vector based on term hashing & n-grams
    dim = 64
    vec = np.zeros(dim, dtype=np.float32)
    text = text.lower()
    for i, word in enumerate(text.split()):
        idx = int.from_bytes(hashlib.sha256(word.encode("utf-8")).digest()[:4], "big") % dim
        vec[idx] += 1.0 / (1.0 + 0.1 * i)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.tolist()

def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    a = np.array(v1, dtype=np.float32)
    b = np.array(v2, dtype=np.float32)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))
