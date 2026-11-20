"""Minutes of Meeting, action-item tracker and follow-up (carry-forward) routes. Every route needs a logged-in user."""
import base64
import hmac
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.action_items import STATUSES, compose_agenda, suggest_followups, unfinished_items_query, valid_due_date
from app.auth import get_current_user
from app.config import settings
from app import recordings
from connectors import groq_client
from app.db import ActionItem, Meeting, MeetingMinutes, MeetingParticipant, RecordingSegment, User, get_db
from memory.audit_memory import audit_memory
from worker.jobs.minutes_job import has_ended, is_minutes_eligible
from worker.state.state_manager import state_manager
from worker.worker import background_worker

logger = logging.getLogger("minutes_api")
router = APIRouter(prefix="/api")
job = background_worker.minutes_job

EDITABLE_PLAN_STATUSES = {"DRAFT", "WAITING_FOR_HUMAN_APPROVAL", "RESCHEDULING_REQUIRED", "NEEDS_INFORMATION"}


def _organizer_meeting(db: Session, meeting_id: str, user: User) -> Meeting:
    m = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not m or m.status == "DELETED":
        raise HTTPException(status_code=404, detail="Meeting not found")
    if m.organizer_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied: only the meeting organizer can do this.")
    return m


# ----------------- MINUTES OF MEETING -----------------

def _minutes_view(db: Session, m: Meeting, include_transcript: bool = False) -> Dict[str, Any]:
    row = db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == m.id).first()
    eligible = is_minutes_eligible(m)
    if row:
        status = row.status
    elif not eligible:
        status = "NOT_APPLICABLE"
    else:
        status = "WAITING_FOR_MEETING_END" if not has_ended(m) else "WAITING_FOR_TRANSCRIPT"
    items = db.query(ActionItem).filter(ActionItem.meeting_id == m.id).order_by(ActionItem.id.asc()).all()
    return {
        "meeting_id": m.id,
        "status": status,
        "eligible": eligible,
        "ended": has_ended(m),
        "minutes": row.to_dict(include_transcript=include_transcript) if row else None,
        "action_items": [i.to_dict() for i in items],
        "recording": _recording_view(db, m.id) if eligible else None,
        "participants": [{"name": p.name, "email": p.email}
                         for p in db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == m.id).all()],
    }


@router.get("/meetings/{id}/minutes")
def get_minutes(id: str, include_transcript: bool = False, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _minutes_view(db, _organizer_meeting(db, id, user), include_transcript)


@router.post("/meetings/{id}/minutes/check-transcript")
def check_transcript(id: str, background: BackgroundTasks, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Organizer's 'check now': one immediate retrieval attempt (the worker keeps retrying on its own)."""
    m = _organizer_meeting(db, id, user)
    if not is_minutes_eligible(m):
        raise HTTPException(status_code=400, detail="Minutes are only produced for online Google Meet meetings.")
    if not has_ended(m):
        raise HTTPException(status_code=409, detail="The meeting has not ended yet.")
    result = job.retrieve(id, force=True)
    if result.get("status") == "TRANSCRIPT_RETRIEVED":
        if job.claim_generation(id):
            from graph.mom_graph import run_mom_generation
            background.add_task(run_mom_generation, id)
    db.expire_all()
    return {**result, **{k: v for k, v in _minutes_view(db, m).items() if k in ("status", "minutes")}}


@router.post("/meetings/{id}/minutes/regenerate")
def regenerate_minutes(id: str, background: BackgroundTasks, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    m = _organizer_meeting(db, id, user)
    if not job.claim_generation(id, regenerate=True):
        raise HTTPException(status_code=409, detail="The minutes cannot be regenerated in their current state.")
    from graph.mom_graph import run_mom_generation
    background.add_task(run_mom_generation, id)
    return {"status": "GENERATING_MOM", "meeting_id": m.id}


class DiscussionPointIn(BaseModel):
    topic: str = ""
    details: str = ""


class DecisionIn(BaseModel):
    text: str
    evidence: Optional[str] = ""


class MinutesActionItemIn(BaseModel):
    id: Optional[int] = None
    description: str
    owner_email: Optional[str] = None
    owner_name: Optional[str] = ""
    due_date: Optional[str] = None
    project_topic: Optional[str] = ""


class MinutesEditIn(BaseModel):
    summary: Optional[str] = None
    discussion_points: Optional[List[DiscussionPointIn]] = None
    decisions: Optional[List[DecisionIn]] = None
    action_items: Optional[List[MinutesActionItemIn]] = None


@router.put("/meetings/{id}/minutes")
def edit_minutes(id: str, payload: MinutesEditIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    m = _organizer_meeting(db, id, user)
    row = db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == id).first()
    if not row or row.status != "AWAITING_ORGANIZER_APPROVAL":
        raise HTTPException(status_code=409, detail="The minutes can only be edited while they await your approval.")
    draft = json.loads(row.mom_draft or "{}")
    if payload.summary is not None:
        draft["summary"] = payload.summary.strip()
    if payload.discussion_points is not None:
        draft["discussion_points"] = [p.model_dump() for p in payload.discussion_points if p.topic.strip() or p.details.strip()]
    if payload.decisions is not None:
        draft["decisions"] = [{"text": d.text.strip(), "evidence": d.evidence or ""} for d in payload.decisions if d.text.strip()]
    if payload.action_items is not None:
        people = {(p.email or "").lower(): p.name for p in db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == id).all()}
        existing = {i.id: i for i in db.query(ActionItem).filter(ActionItem.meeting_id == id, ActionItem.finalized.is_(False)).all()}
        new_rows = []
        for entry in payload.action_items:
            if not entry.description.strip():
                raise HTTPException(status_code=422, detail="Every action item needs a description.")
            try:
                due = valid_due_date(entry.due_date)
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error))
            email = (entry.owner_email or "").strip().lower() or None
            if email and email not in people:
                raise HTTPException(status_code=422, detail=f"{email} is not a participant of this meeting.")
            old = existing.get(entry.id)
            new_rows.append(ActionItem(
                meeting_id=id, organizer_id=m.organizer_id, description=entry.description.strip(),
                owner_email=email, owner_name=people[email] if email else (entry.owner_name or "").strip(),
                due_date=due, project_topic=(entry.project_topic or "").strip()[:255],
                source_quote=old.source_quote if old else "", status="NOT_STARTED", finalized=False))
        for old in existing.values():
            db.delete(old)
        db.add_all(new_rows)
    row.mom_draft = json.dumps(draft)
    db.commit()
    audit_memory.record_event("Human Operator", "EDIT_MINUTES", meeting_id=id)
    return _minutes_view(db, m)


@router.post("/meetings/{id}/minutes/approve")
def approve_minutes(id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _organizer_meeting(db, id, user)
    result = job.approve_and_distribute(id, user.id)
    if not result.get("success"):
        raise HTTPException(status_code=result.get("code", 500), detail=result["error"])
    db.expire_all()
    return {**result, **_minutes_view(db, _organizer_meeting(db, id, user))}


# ----------------- ACTION ITEM TRACKER -----------------

def _item_payload(item: ActionItem, meeting: Meeting, organizer: Optional[User]) -> Dict[str, Any]:
    return {**item.to_dict(), "meeting_title": meeting.title, "meeting_date": meeting.scheduled_start,
            "organizer_name": organizer.name if organizer else "", "organizer_email": organizer.email if organizer else ""}


@router.get("/action-items")
def list_action_items(scope: str = Query("mine", pattern="^(mine|organized)$"), status: Optional[str] = None,
                      meeting_id: Optional[str] = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """mine = tasks assigned to me; organized = tasks of meetings I organized. Draft (unapproved) tasks never appear."""
    query = (db.query(ActionItem, Meeting, User)
             .join(Meeting, Meeting.id == ActionItem.meeting_id)
             .outerjoin(User, User.id == ActionItem.organizer_id)
             .filter(ActionItem.finalized.is_(True), Meeting.status != "DELETED"))
    if scope == "mine":
        query = query.filter(func.lower(ActionItem.owner_email) == user.email.strip().lower())
    else:
        query = query.filter(ActionItem.organizer_id == user.id, Meeting.organizer_id == user.id)
    if status:
        if status not in STATUSES:
            raise HTTPException(status_code=422, detail=f"Status must be one of {', '.join(STATUSES)}.")
        query = query.filter(ActionItem.status == status)
    if meeting_id:
        query = query.filter(ActionItem.meeting_id == meeting_id)
    rows = sorted(query.all(), key=lambda r: (r[0].status == "COMPLETED", r[0].due_date or "9999-12-31", r[0].id))
    return [_item_payload(i, m, o) for i, m, o in rows]


class ActionItemUpdate(BaseModel):
    status: Optional[str] = None
    description: Optional[str] = None
    owner_email: Optional[str] = None
    due_date: Optional[str] = None
    project_topic: Optional[str] = None


@router.patch("/action-items/{item_id}")
def update_action_item(item_id: int, payload: ActionItemUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = db.query(ActionItem).filter(ActionItem.id == item_id).first()
    meeting = db.query(Meeting).filter(Meeting.id == item.meeting_id).first() if item else None
    is_organizer = bool(item and meeting and item.organizer_id == user.id and meeting.organizer_id == user.id)
    is_owner = bool(item and item.owner_email and item.owner_email.strip().lower() == user.email.strip().lower())
    if not item or not (is_organizer or is_owner) or not item.finalized or (meeting and meeting.status == "DELETED"):
        # Not revealing whether the task exists to someone who has no relation to it.
        raise HTTPException(status_code=404, detail="Action item not found")
    changes = payload.model_fields_set
    if not is_organizer and changes - {"status"}:
        raise HTTPException(status_code=403, detail="You can only update the status of your own tasks.")
    if "status" in changes:
        if payload.status not in STATUSES:
            raise HTTPException(status_code=422, detail=f"Status must be one of {', '.join(STATUSES)}.")
        item.status = payload.status
        item.completed_at = datetime.utcnow() if payload.status == "COMPLETED" else None
    if "description" in changes:
        if not (payload.description or "").strip():
            raise HTTPException(status_code=422, detail="The description cannot be empty.")
        item.description = payload.description.strip()
    if "due_date" in changes:
        try:
            item.due_date = valid_due_date(payload.due_date)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error))
    if "project_topic" in changes:
        item.project_topic = (payload.project_topic or "").strip()[:255]
    if "owner_email" in changes:
        email = (payload.owner_email or "").strip().lower() or None
        if email:
            person = db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == item.meeting_id,
                                                         func.lower(MeetingParticipant.email) == email).first()
            if not person:
                raise HTTPException(status_code=422, detail=f"{email} is not a participant of the original meeting.")
            item.owner_name = person.name
        else:
            item.owner_name = ""
        item.owner_email = email
    db.commit()
    audit_memory.record_event("Human Operator", "UPDATE_ACTION_ITEM", meeting_id=item.meeting_id,
                              details={"item_id": item.id, "by": user.email, "changes": sorted(changes)})
    organizer = db.query(User).filter(User.id == item.organizer_id).first()
    return _item_payload(item, meeting, organizer)


# ----------------- CARRY-FORWARD (follow-up) TASKS -----------------

@router.get("/meetings/{id}/followup-suggestions")
def followup_suggestions(id: str, show_all: bool = False, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    m = _organizer_meeting(db, id, user)
    return suggest_followups(db, m, show_all=show_all)


class FollowupSelection(BaseModel):
    action_item_ids: List[int] = []


@router.put("/meetings/{id}/followups")
def select_followups(id: str, payload: FollowupSelection, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The organizer's choice of unfinished tasks to discuss. Skipped tasks are untouched and stay open."""
    m = _organizer_meeting(db, id, user)
    if m.status not in EDITABLE_PLAN_STATUSES:
        raise HTTPException(status_code=409, detail="Follow-up tasks can only be chosen before the meeting is approved.")
    eligible = {i.id for i, _ in unfinished_items_query(db, user.id, exclude_meeting_id=id).all()}
    ids = list(dict.fromkeys(payload.action_item_ids))
    invalid = [i for i in ids if i not in eligible]
    if invalid:
        raise HTTPException(status_code=422, detail=f"These tasks are not open tasks of your meetings: {invalid}")
    parsed = json.loads(m.parsed_details or "{}")
    parsed["followup_action_item_ids"] = ids
    m.parsed_details = json.dumps(parsed)
    m.agenda = compose_agenda(db, m, m.agenda or "")
    db.commit()
    audit_memory.record_event("Human Operator", "SELECT_FOLLOWUPS", meeting_id=id, details={"action_item_ids": ids})
    return {"meeting_id": id, "selected_ids": ids, "agenda": m.agenda}


# ----------------- GOOGLE MEET EVENT NOTIFICATIONS (optional push) -----------------

@router.post("/meet/events")
async def meet_events(request: Request, token: str = Query("")):
    """
    Pub/Sub push endpoint for Google Workspace Events (e.g. transcript file generated). It only brings the next
    retrieval forward; polling remains the safety net. Duplicate deliveries are recognised and ignored.
    """
    if not settings.MEET_EVENTS_TOKEN:
        raise HTTPException(status_code=503, detail="MEET_EVENTS_TOKEN is not configured.")
    if not hmac.compare_digest(token, settings.MEET_EVENTS_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token.")
    try:
        body = await request.json()
        message = body.get("message") or {}
        message_id = str(message.get("messageId") or message.get("message_id") or "")
        if message.get("data"):
            base64.b64decode(message["data"])  # must be well-formed; content is not trusted
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed Pub/Sub message.")
    if message_id:
        event_id = f"meet-evt-{message_id}"
        if state_manager.is_event_processed(event_id):
            return {"status": "duplicate"}
        state_manager.mark_event_processed(event_id, source="MEET_EVENT")
    return {"status": "accepted", "meetings_due": job.notify()}


# ----------------- BROWSER-RECORDED AUDIO (alternative to a native Google Meet transcript) -----------------

class RecordingStartIn(BaseModel):
    consent: bool = False


class RecordingFinishIn(BaseModel):
    segment_count: Optional[int] = None


def _recording_view(db: Session, meeting_id: str) -> Dict[str, Any]:
    segments = db.query(RecordingSegment).filter(RecordingSegment.meeting_id == meeting_id) \
        .order_by(RecordingSegment.segment_index.asc()).all()
    return {"segments": [s.to_dict() for s in segments],
            "transcription_configured": groq_client.is_configured(),
            "max_segment_mb": settings.RECORDING_MAX_SEGMENT_MB,
            "retention_days": settings.RECORDING_RETENTION_DAYS}


def _fail(result: Dict[str, Any]) -> None:
    raise HTTPException(status_code=result.get("code", 400), detail=result.get("error", "Request failed."))


@router.post("/meetings/{id}/recording/start")
def start_recording(id: str, payload: RecordingStartIn, background: BackgroundTasks, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """The organizer explicitly chose to record. Consent must be confirmed; a native transcript, if one exists, wins."""
    m = _organizer_meeting(db, id, user)
    if not is_minutes_eligible(m):
        raise HTTPException(status_code=400, detail="Recording is only available for online Google Meet meetings.")
    if not payload.consent:
        raise HTTPException(status_code=422, detail="Confirm that every participant has been told about and agreed to the recording.")
    if has_ended(m):
        native = job.retrieve(id, force=True)         # one last look for Google's own transcript
        if native.get("status") == "TRANSCRIPT_RETRIEVED":
            if job.claim_generation(id):
                from graph.mom_graph import run_mom_generation
                background.add_task(run_mom_generation, id)
            return {"status": "TRANSCRIPT_RETRIEVED", "native_transcript": True,
                    "message": "Google already provided a transcript for this meeting, so it is being used instead of a recording."}
    result = job.recording_start(id)
    if not result.get("success"):
        _fail(result)
    return {"status": result["status"], "native_transcript": False}


@router.post("/meetings/{id}/recording/segments")
def upload_recording_segment(id: str, segment_index: int = Form(..., ge=0, le=9999), offset_seconds: float = Form(0.0, ge=0),
                             duration_seconds: Optional[float] = Form(None, ge=0), file: UploadFile = File(...),
                             db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Store one audio segment. Uploading the same segment twice is harmless (retries after a network failure)."""
    _organizer_meeting(db, id, user)
    row = db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == id).first()
    if not row or json.loads(row.transcript_meta or "{}").get("source") != "RECORDING":
        raise HTTPException(status_code=409, detail="Start the recording before uploading audio.")
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime not in recordings.ALLOWED_MIME:
        raise HTTPException(status_code=415, detail=f"Unsupported audio type {mime or 'unknown'}.")
    data = file.file.read(recordings.max_segment_bytes() + 1)
    if not data:
        raise HTTPException(status_code=422, detail="The audio file is empty.")
    if len(data) > recordings.max_segment_bytes():
        raise HTTPException(status_code=413, detail=f"The audio segment is larger than {settings.RECORDING_MAX_SEGMENT_MB} MB.")
    digest = recordings.sha256(data)
    existing = db.query(RecordingSegment).filter(RecordingSegment.meeting_id == id, RecordingSegment.segment_index == segment_index).first()
    if existing and existing.sha256 == digest:
        return {"segment_index": segment_index, "duplicate": True, "status": row.status}
    if row.status != "RECORDING_IN_PROGRESS":
        raise HTTPException(status_code=409, detail="The recording was already finished; no more audio can be added.")
    path = recordings.write_segment(id, segment_index, mime, data)
    fields = dict(offset_seconds=offset_seconds, duration_seconds=duration_seconds, mime_type=mime, size_bytes=len(data), sha256=digest,
                  storage_path=str(path), status="UPLOADED", attempts=0, transcript_text="", last_error="", uploaded_at=datetime.utcnow(),
                  audio_deleted_at=None)
    if existing:
        for key, value in fields.items():
            setattr(existing, key, value)
    else:
        db.add(RecordingSegment(meeting_id=id, segment_index=segment_index, **fields))
    try:
        db.commit()
    except IntegrityError:                    # a parallel upload of the same segment won the insert
        db.rollback()
        return {"segment_index": segment_index, "duplicate": True, "status": row.status}
    return {"segment_index": segment_index, "duplicate": False, "status": row.status}


@router.post("/meetings/{id}/recording/finish")
def finish_recording(id: str, payload: RecordingFinishIn, background: BackgroundTasks, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Recording stopped and uploaded: transcribe it and run the existing MoM workflow in the background."""
    _organizer_meeting(db, id, user)
    result = job.recording_finish(id, payload.segment_count)
    if not result.get("success"):
        _fail(result)
    if not result.get("duplicate"):
        background.add_task(job.process_recording, id)
    return {"status": result["status"], "duplicate": bool(result.get("duplicate"))}


@router.post("/meetings/{id}/recording/retry")
def retry_transcription(id: str, background: BackgroundTasks, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _organizer_meeting(db, id, user)
    row = db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == id).first()
    if not row or row.status not in ("TRANSCRIPTION_FAILED", "AUDIO_UPLOADED"):
        raise HTTPException(status_code=409, detail="There is no failed transcription to retry.")
    background.add_task(job.process_recording, id, True)
    return {"status": "TRANSCRIBING"}


@router.post("/meetings/{id}/recording/discard")
def discard_recording(id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Drop an unfinished/failed recording and its audio; the meeting goes back to waiting for Google's transcript."""
    _organizer_meeting(db, id, user)
    result = job.discard_recording(id)
    if not result.get("success"):
        _fail(result)
    return {"status": result["status"]}


@router.delete("/meetings/{id}/recording/audio")
def delete_recording_audio(id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Delete the raw audio now (the transcript and minutes are kept). Not allowed while the audio is still needed."""
    _organizer_meeting(db, id, user)
    row = db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == id).first()
    if not row or row.status in ("RECORDING_IN_PROGRESS", "AUDIO_UPLOADED", "TRANSCRIBING", "TRANSCRIPTION_FAILED"):
        raise HTTPException(status_code=409, detail="The audio is still needed for transcription. Discard the recording instead.")
    segments = db.query(RecordingSegment).filter(RecordingSegment.meeting_id == id, RecordingSegment.storage_path != "").all()
    return {"deleted_files": recordings.delete_audio(db, segments)}
