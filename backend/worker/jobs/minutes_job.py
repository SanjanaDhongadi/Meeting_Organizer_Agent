"""
Minutes-of-Meeting job: end-of-meeting detection, transcript retrieval with retries, MoM generation,
and (after organizer approval) distribution.

Every state change is a conditional UPDATE ("claim"), so the worker, the API and duplicate event notifications
can all call these methods concurrently without double-processing or double-sending.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import or_

from app.config import settings
from app.db import ActionItem, Meeting, MeetingMinutes, MeetingParticipant, RecordingSegment, SessionLocal, User
from app import recordings
from connectors.meet_service import meeting_code_from_url
from connectors.transcription_service import transcription_service
from memory.audit_memory import audit_memory

logger = logging.getLogger("minutes_job")

RETRY_MINUTES = [5, 10, 15, 30, 60]
LEASE_MINUTES = 5            # a retrieval attempt reserves the row so parallel callers skip it
STUCK_GENERATION_MINUTES = 15
TRANSCRIBE_RETRY_MINUTES = [1, 2, 5, 10, 15]     # backoff between transcription attempts of recorded audio
# Meetings in these states never happened (or have not been approved/sent yet), so there is nothing to minute.
NOT_HELD_STATUSES = {"DELETED", "REJECTED", "DRAFT", "NEEDS_INFORMATION", "WAITING_FOR_HUMAN_APPROVAL",
                     "APPROVED", "EXECUTING", "FAILED", "ACTION_FAILED"}


def _parse_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def is_minutes_eligible(m: Meeting) -> bool:
    """Online (Google Meet) meetings that were scheduled with a Meet link. In-person meetings are excluded."""
    return ((m.mode or "").upper() == "ONLINE" and bool(m.meet_url) and bool(m.calendar_event_id)
            and m.status not in NOT_HELD_STATUSES)


def has_ended(m: Meeting, now: Optional[datetime] = None) -> bool:
    end = _parse_utc(m.scheduled_end)
    return bool(end and end <= (now or datetime.utcnow()))


class MinutesJob:
    # ----- helpers -----
    @staticmethod
    def _claim(db, model_id: int, from_statuses: List[str], to_status: str, extra: Optional[Dict[str, Any]] = None,
               require_due: bool = False) -> bool:
        """Atomic compare-and-set on the row's status. True only for the single caller that wins."""
        now = datetime.utcnow()
        query = db.query(MeetingMinutes).filter(MeetingMinutes.id == model_id, MeetingMinutes.status.in_(from_statuses))
        if require_due:
            query = query.filter(or_(MeetingMinutes.next_attempt_at.is_(None), MeetingMinutes.next_attempt_at <= now))
        values = {"status": to_status, "updated_at": now, **(extra or {})}
        won = query.update(values, synchronize_session=False) == 1
        db.commit()
        return won

    @staticmethod
    def ensure_row(db, m: Meeting) -> Optional[MeetingMinutes]:
        row = db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == m.id).first()
        if row or not is_minutes_eligible(m):
            return row
        from sqlalchemy.exc import IntegrityError
        row = MeetingMinutes(meeting_id=m.id, status="WAITING_FOR_TRANSCRIPT")   # next_attempt_at NULL = due now
        db.add(row)
        try:
            db.commit()
        except IntegrityError:          # a parallel caller created it first
            db.rollback()
            row = db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == m.id).first()
        return row

    @staticmethod
    def _deadline(m: Meeting, meta: Dict[str, Any]) -> datetime:
        end = _parse_utc(m.scheduled_end) or datetime.utcnow()
        deadline = end + timedelta(hours=settings.MINUTES_MAX_WAIT_HOURS)
        extended = _parse_utc(meta.get("extended_until"))
        return max(deadline, extended) if extended else deadline

    @staticmethod
    def _organizer_email(db, m: Meeting) -> Optional[str]:
        if settings.DEMO_MODE:
            return None
        workflow = (json.loads(m.parsed_details or "{}").get("workflow") or {})
        if workflow.get("organizer_email"):
            return workflow["organizer_email"]
        user = db.query(User).filter(User.id == m.organizer_id).first() if m.organizer_id else None
        return user.email if user else None

    # ----- scan (worker cycle) -----
    def scan(self) -> Dict[str, int]:
        """Create rows for meetings that just ended, retry due transcripts, generate drafts, free stuck jobs."""
        stats = {"created": 0, "retrieved": 0, "generated": 0}
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            horizon = now - timedelta(hours=settings.MINUTES_MAX_WAIT_HOURS)
            ended = [m for m in db.query(Meeting).filter(Meeting.mode == "ONLINE", Meeting.meet_url != "",
                                                         Meeting.calendar_event_id != "").all()
                     if has_ended(m, now) and (_parse_utc(m.scheduled_end) or now) >= horizon]
            existing = {r.meeting_id for r in db.query(MeetingMinutes.meeting_id).all()}
            for m in ended:
                if m.id not in existing and is_minutes_eligible(m) and self.ensure_row(db, m):
                    stats["created"] += 1
            stuck = now - timedelta(minutes=STUCK_GENERATION_MINUTES)
            db.query(MeetingMinutes).filter(MeetingMinutes.status == "GENERATING_MOM", MeetingMinutes.updated_at < stuck) \
                .update({"status": "TRANSCRIPT_RETRIEVED"}, synchronize_session=False)
            db.query(MeetingMinutes).filter(MeetingMinutes.status == "TRANSCRIBING", MeetingMinutes.updated_at < stuck) \
                .update({"status": "AUDIO_UPLOADED"}, synchronize_session=False)
            db.commit()
            to_transcribe = [r.meeting_id for r in db.query(MeetingMinutes).filter(
                MeetingMinutes.status == "AUDIO_UPLOADED",
                or_(MeetingMinutes.next_attempt_at.is_(None), MeetingMinutes.next_attempt_at <= now)).all()]
            due = [r.meeting_id for r in db.query(MeetingMinutes).filter(
                MeetingMinutes.status == "WAITING_FOR_TRANSCRIPT",
                or_(MeetingMinutes.next_attempt_at.is_(None), MeetingMinutes.next_attempt_at <= now)).all()]
            pending = [r.meeting_id for r in db.query(MeetingMinutes).filter(MeetingMinutes.status == "TRANSCRIPT_RETRIEVED").all()]
        finally:
            db.close()
        for meeting_id in to_transcribe:
            if self.transcribe(meeting_id).get("status") == "TRANSCRIPT_RETRIEVED":
                stats["retrieved"] += 1
                pending.append(meeting_id)
        self.purge_expired_audio()
        for meeting_id in due:
            if self.retrieve(meeting_id).get("status") == "TRANSCRIPT_RETRIEVED":
                stats["retrieved"] += 1
                pending.append(meeting_id)
        for meeting_id in dict.fromkeys(pending):
            if self.generate(meeting_id).get("success"):
                stats["generated"] += 1
        return stats

    def notify(self) -> int:
        """A Meet event notification arrived: make every waiting meeting due now. Safe to call repeatedly."""
        db = SessionLocal()
        try:
            n = db.query(MeetingMinutes).filter(MeetingMinutes.status == "WAITING_FOR_TRANSCRIPT") \
                .update({"next_attempt_at": datetime.utcnow()}, synchronize_session=False)
            db.commit()
            return n
        finally:
            db.close()

    # ----- transcript retrieval -----
    def retrieve(self, meeting_id: str, force: bool = False) -> Dict[str, Any]:
        from connectors.mcp_gateway import mcp_call
        db = SessionLocal()
        try:
            m = db.query(Meeting).filter(Meeting.id == meeting_id).first()
            row = self.ensure_row(db, m) if m else None
            if not m or not row:
                return {"status": None, "error": "This meeting has no Google Meet transcript to retrieve."}
            if row.status == "TRANSCRIPT_UNAVAILABLE" and force:
                meta = json.loads(row.transcript_meta or "{}")
                meta["extended_until"] = (datetime.utcnow() + timedelta(hours=24)).isoformat()
                row.transcript_meta = json.dumps(meta)
                row.status, row.retrieval_attempts = "WAITING_FOR_TRANSCRIPT", 0
                db.commit()
            if row.status != "WAITING_FOR_TRANSCRIPT":
                return {"status": row.status, "skipped": True}      # duplicate trigger: already handled
            lease = datetime.utcnow() + timedelta(minutes=LEASE_MINUTES)
            if not self._claim(db, row.id, ["WAITING_FOR_TRANSCRIPT"], "WAITING_FOR_TRANSCRIPT",
                               {"next_attempt_at": lease}, require_due=not force):
                return {"status": row.status, "skipped": True}
            db.refresh(row)
            meta = json.loads(row.transcript_meta or "{}")
            code = meeting_code_from_url(m.meet_url)
            if not code:
                return self._finish_failed(db, row, "The meeting has no valid Google Meet link.")
            start = _parse_utc(m.scheduled_start)
            not_before = (start - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ") if start else None
            result = mcp_call("meet.get_transcript", {
                "meeting_code": code, "organizer_email": self._organizer_email(db, m), "not_before": not_before})
            now = datetime.utcnow()

            if result.get("success") and result.get("ready"):
                meta.update(result.get("meta") or {})
                meta["source"] = "GOOGLE_MEET"
                if not self._claim(db, row.id, ["WAITING_FOR_TRANSCRIPT"], "TRANSCRIPT_RETRIEVED", {
                        "transcript_text": result["transcript_text"], "transcript_meta": json.dumps(meta),
                        "last_error": "", "next_attempt_at": None}):
                    return {"status": row.status, "skipped": True}      # the organizer chose to record in the meantime
                db.refresh(row)
                audit_memory.record_event("Minutes Job", "TRANSCRIPT_RETRIEVED", meeting_id=meeting_id,
                                          details={"characters": len(row.transcript_text)})
                return {"status": row.status}

            message = result.get("message") or result.get("error") or "Transcript is not available yet."
            attempts = (row.retrieval_attempts or 0) + 1
            values = {"retrieval_attempts": attempts, "last_error": message}
            if now >= self._deadline(m, meta):
                values.update(next_attempt_at=None, last_error=f"No transcript after {settings.MINUTES_MAX_WAIT_HOURS} hours. Last result: {message}")
                target = "TRANSCRIPT_UNAVAILABLE"
            else:
                target = "WAITING_FOR_TRANSCRIPT"
                values["next_attempt_at"] = now + timedelta(minutes=RETRY_MINUTES[min(attempts - 1, len(RETRY_MINUTES) - 1)])
            if not self._claim(db, row.id, ["WAITING_FOR_TRANSCRIPT"], target, values):
                return {"status": row.status, "skipped": True}
            db.refresh(row)
            audit_memory.record_event("Minutes Job", "TRANSCRIPT_RETRY", meeting_id=meeting_id, status="WAITING",
                                      error=message, details={"attempt": row.retrieval_attempts, "reason": result.get("reason"),
                                                              "error_code": result.get("error_code")})
            return {"status": row.status, "message": message}
        finally:
            db.close()

    @staticmethod
    def _finish_failed(db, row: MeetingMinutes, message: str) -> Dict[str, Any]:
        row.status, row.last_error, row.next_attempt_at = "TRANSCRIPT_UNAVAILABLE", message, None
        db.commit()
        return {"status": row.status, "message": message}

    # ----- browser-recorded audio (alternative to a native Google Meet transcript) -----
    def recording_start(self, meeting_id: str, consent_at: Optional[datetime] = None) -> Dict[str, Any]:
        """The organizer explicitly chose to record: stop polling Google for this meeting. One caller wins."""
        db = SessionLocal()
        try:
            m = db.query(Meeting).filter(Meeting.id == meeting_id).first()
            row = self.ensure_row(db, m) if m else None
            if not m or not row:
                return {"success": False, "code": 400, "error": "Recording is only available for online Google Meet meetings."}
            if row.status == "RECORDING_IN_PROGRESS":
                return {"success": True, "status": row.status, "resumed": True}
            meta = json.loads(row.transcript_meta or "{}")
            meta.update(source="RECORDING", recording_started_at=datetime.utcnow().isoformat(),
                        consent_confirmed_at=(consent_at or datetime.utcnow()).isoformat(), speaker_attribution="none")
            if not self._claim(db, row.id, ["WAITING_FOR_TRANSCRIPT", "TRANSCRIPT_UNAVAILABLE"], "RECORDING_IN_PROGRESS",
                               {"transcript_meta": json.dumps(meta), "last_error": "", "next_attempt_at": None}):
                db.refresh(row)
                return {"success": False, "code": 409, "status": row.status,
                        "error": f"This meeting already has a transcript or minutes ({row.status}); it cannot be recorded again."}
            audit_memory.record_event("Human Operator", "RECORDING_STARTED", meeting_id=meeting_id)
            return {"success": True, "status": "RECORDING_IN_PROGRESS"}
        finally:
            db.close()

    def recording_finish(self, meeting_id: str, segment_count: Optional[int] = None) -> Dict[str, Any]:
        """The organizer stopped recording: hand the uploaded audio to transcription. Safe to call twice."""
        db = SessionLocal()
        try:
            row = db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == meeting_id).first()
            if not row:
                return {"success": False, "code": 404, "error": "No recording was started for this meeting."}
            if row.status != "RECORDING_IN_PROGRESS":
                if json.loads(row.transcript_meta or "{}").get("source") == "RECORDING" and row.status != "WAITING_FOR_TRANSCRIPT":
                    return {"success": True, "status": row.status, "duplicate": True}
                return {"success": False, "code": 409, "error": "No recording is in progress for this meeting."}
            indexes = sorted(s.segment_index for s in db.query(RecordingSegment).filter(RecordingSegment.meeting_id == meeting_id).all())
            if not indexes:
                return {"success": False, "code": 409, "error": "No audio has been uploaded yet."}
            missing = [i for i in range(segment_count) if i not in indexes] if segment_count is not None else []
            if missing:
                return {"success": False, "code": 409, "error": f"Audio segment(s) {missing} have not been uploaded yet.", "missing": missing}
            meta = json.loads(row.transcript_meta or "{}")
            meta.update(recording_finished_at=datetime.utcnow().isoformat(), segments=len(indexes),
                        incomplete=bool(set(range(max(indexes) + 1)) - set(indexes)))
            if not self._claim(db, row.id, ["RECORDING_IN_PROGRESS"], "AUDIO_UPLOADED", {
                    "transcript_meta": json.dumps(meta), "retrieval_attempts": 0, "next_attempt_at": None, "last_error": ""}):
                return {"success": True, "status": "AUDIO_UPLOADED", "duplicate": True}
            audit_memory.record_event("Human Operator", "RECORDING_UPLOADED", meeting_id=meeting_id, details={"segments": len(indexes)})
            return {"success": True, "status": "AUDIO_UPLOADED"}
        finally:
            db.close()

    def transcribe(self, meeting_id: str, retry: bool = False) -> Dict[str, Any]:
        """Transcribe the uploaded segments (only those not done yet) and store the transcript. One caller wins."""
        db = SessionLocal()
        try:
            row = db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == meeting_id).first()
            if not row:
                return {"status": None}
            if retry and row.status == "TRANSCRIPTION_FAILED":
                db.query(RecordingSegment).filter(RecordingSegment.meeting_id == meeting_id, RecordingSegment.status == "FAILED") \
                    .update({"status": "UPLOADED", "attempts": 0, "last_error": ""}, synchronize_session=False)
                self._claim(db, row.id, ["TRANSCRIPTION_FAILED"], "AUDIO_UPLOADED",
                            {"retrieval_attempts": 0, "next_attempt_at": None, "last_error": ""})
            if not self._claim(db, row.id, ["AUDIO_UPLOADED"], "TRANSCRIBING", require_due=not retry):
                db.refresh(row)
                return {"status": row.status, "skipped": True}
            segments = db.query(RecordingSegment).filter(RecordingSegment.meeting_id == meeting_id) \
                .order_by(RecordingSegment.segment_index.asc()).all()
            problem: Optional[Dict[str, Any]] = None
            for seg in segments:
                if seg.status == "TRANSCRIBED":
                    continue
                if not seg.storage_path or not os.path.exists(seg.storage_path):
                    seg.status, seg.last_error = "FAILED", "The audio file is missing."
                    problem = problem or {"retryable": False, "error": f"The audio of segment {seg.segment_index} is missing. Record again."}
                    db.commit()
                    continue
                result = transcription_service.transcribe_file(seg.storage_path, seg.mime_type, seg.offset_seconds or 0.0)
                seg.attempts = (seg.attempts or 0) + 1
                if result.get("success"):
                    seg.transcript_text, seg.status, seg.last_error, seg.transcribed_at = result.get("text") or "", "TRANSCRIBED", "", datetime.utcnow()
                else:
                    seg.last_error = result.get("error") or "Transcription failed."
                    if not result.get("retryable"):
                        seg.status = "FAILED"
                    problem = problem or result
                row.updated_at = datetime.utcnow()        # heartbeat: a long recording is not "stuck"
                db.commit()

            if problem:
                attempts = (row.retrieval_attempts or 0) + 1
                error = problem.get("error") or "Transcription failed."
                if problem.get("retryable") and attempts < settings.TRANSCRIPTION_MAX_ATTEMPTS:
                    delay = TRANSCRIBE_RETRY_MINUTES[min(attempts - 1, len(TRANSCRIBE_RETRY_MINUTES) - 1)]
                    self._claim(db, row.id, ["TRANSCRIBING"], "AUDIO_UPLOADED", {
                        "retrieval_attempts": attempts, "last_error": f"{error} Retrying in {delay} min.",
                        "next_attempt_at": datetime.utcnow() + timedelta(minutes=delay)})
                else:
                    self._claim(db, row.id, ["TRANSCRIBING"], "TRANSCRIPTION_FAILED", {
                        "retrieval_attempts": attempts, "last_error": error, "next_attempt_at": None})
                audit_memory.record_event("Minutes Job", "TRANSCRIPTION_FAILED", meeting_id=meeting_id, status="ERROR", error=error,
                                          details={"attempt": attempts, "retryable": bool(problem.get("retryable"))})
                db.refresh(row)
                return {"status": row.status, "message": error}

            text = "\n".join(s.transcript_text for s in segments if (s.transcript_text or "").strip())
            if not text.strip():
                self._claim(db, row.id, ["TRANSCRIBING"], "TRANSCRIPTION_FAILED", {
                    "last_error": "No speech was detected in the recording. Check that the Meet tab's audio was shared.", "next_attempt_at": None})
                db.refresh(row)
                return {"status": row.status}
            meta = json.loads(row.transcript_meta or "{}")
            meta.update(source="RECORDING", transcription_model=settings.GROQ_TRANSCRIPTION_MODEL, speaker_attribution="none",
                        segments=len(segments), recorded_seconds=round(sum(s.duration_seconds or 0 for s in segments)))
            if not self._claim(db, row.id, ["TRANSCRIBING"], "TRANSCRIPT_RETRIEVED", {
                    "transcript_text": text, "transcript_meta": json.dumps(meta), "last_error": "", "next_attempt_at": None}):
                db.refresh(row)
                return {"status": row.status, "skipped": True}
            audit_memory.record_event("Minutes Job", "TRANSCRIPT_RETRIEVED", meeting_id=meeting_id,
                                      details={"characters": len(text), "source": "RECORDING"})
            if settings.RECORDING_RETENTION_DAYS <= 0:
                recordings.delete_audio(db, segments)
            return {"status": "TRANSCRIPT_RETRIEVED"}
        finally:
            db.close()

    def process_recording(self, meeting_id: str, retry: bool = False) -> Dict[str, Any]:
        """Transcribe, then run the existing MoM workflow. Used by the API (background task) and by scan()."""
        result = self.transcribe(meeting_id, retry=retry)
        if result.get("status") == "TRANSCRIPT_RETRIEVED":
            self.generate(meeting_id)
        return result

    def discard_recording(self, meeting_id: str) -> Dict[str, Any]:
        """Throw away an unfinished or failed recording and go back to waiting for a Google transcript."""
        db = SessionLocal()
        try:
            row = db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == meeting_id).first()
            if not row or row.status not in ("RECORDING_IN_PROGRESS", "TRANSCRIPTION_FAILED"):
                return {"success": False, "code": 409, "error": "There is no unfinished recording to discard."}
            segments = db.query(RecordingSegment).filter(RecordingSegment.meeting_id == meeting_id).all()
            recordings.delete_audio(db, segments)
            for seg in segments:
                db.delete(seg)
            meta = json.loads(row.transcript_meta or "{}")
            for key in ("source", "recording_started_at", "recording_finished_at", "consent_confirmed_at", "segments", "incomplete"):
                meta.pop(key, None)
            if not self._claim(db, row.id, ["RECORDING_IN_PROGRESS", "TRANSCRIPTION_FAILED"], "WAITING_FOR_TRANSCRIPT", {
                    "transcript_meta": json.dumps(meta), "retrieval_attempts": 0, "next_attempt_at": None, "last_error": ""}):
                return {"success": False, "code": 409, "error": "The recording changed state; try again."}
            audit_memory.record_event("Human Operator", "RECORDING_DISCARDED", meeting_id=meeting_id)
            return {"success": True, "status": "WAITING_FOR_TRANSCRIPT"}
        finally:
            db.close()

    @staticmethod
    def purge_expired_audio() -> int:
        """Retention: delete raw audio whose transcript was stored more than RECORDING_RETENTION_DAYS ago."""
        db = SessionLocal()
        try:
            cutoff = datetime.utcnow() - timedelta(days=max(settings.RECORDING_RETENTION_DAYS, 0))
            old = db.query(RecordingSegment).filter(RecordingSegment.status == "TRANSCRIBED", RecordingSegment.storage_path != "",
                                                    RecordingSegment.transcribed_at < cutoff).all()
            return recordings.delete_audio(db, old) if old else 0
        except Exception:
            logger.exception("Audio retention purge failed")
            return 0
        finally:
            db.close()

    # ----- generation -----
    def claim_generation(self, meeting_id: str, regenerate: bool = False) -> bool:
        """Reserve the right to generate (TRANSCRIPT_RETRIEVED / MOM_FAILED -> GENERATING_MOM). One caller wins."""
        db = SessionLocal()
        try:
            row = db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == meeting_id).first()
            if not row:
                return False
            allowed = ["TRANSCRIPT_RETRIEVED", "MOM_FAILED"] + (["AWAITING_ORGANIZER_APPROVAL"] if regenerate else [])
            return self._claim(db, row.id, allowed, "GENERATING_MOM")
        finally:
            db.close()

    def generate(self, meeting_id: str, regenerate: bool = False) -> Dict[str, Any]:
        from graph.mom_graph import run_mom_generation
        if not self.claim_generation(meeting_id, regenerate):
            return {"success": False, "skipped": True}
        return run_mom_generation(meeting_id)

    # ----- approval + distribution -----
    @staticmethod
    def _recipients(db, m: Meeting) -> List[str]:
        emails = [p.email.lower() for p in db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == m.id).all()
                  if p.email and p.response_status != "REJECTED"]
        organizer = db.query(User).filter(User.id == m.organizer_id).first() if m.organizer_id else None
        if organizer:
            emails.append(organizer.email.lower())
        return list(dict.fromkeys(emails))

    @staticmethod
    def render_email(m: Meeting, draft: Dict[str, Any], items: List[ActionItem]) -> Dict[str, str]:
        from skills.format_skill import FormatMeetingSkill
        info = draft.get("meeting") or {}
        return FormatMeetingSkill().run({
            "title": m.title, "date": info.get("date"), "duration_minutes": info.get("duration_minutes"),
            "participants": [p["name"] for p in info.get("participants", [])],
            "summary": draft.get("summary"), "discussion_points": draft.get("discussion_points", []),
            "decisions": draft.get("decisions", []),
            "action_items": [{"description": i.description, "owner_name": i.owner_name, "due_date": i.due_date}
                             for i in items],
        }, "minutes")

    def approve_and_distribute(self, meeting_id: str, approver_id: int) -> Dict[str, Any]:
        from connectors.mcp_gateway import mcp_call
        db = SessionLocal()
        try:
            m = db.query(Meeting).filter(Meeting.id == meeting_id).first()
            row = db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == meeting_id).first()
            if not m or not row:
                return {"success": False, "code": 404, "error": "Minutes not found."}
            if row.status not in ("AWAITING_ORGANIZER_APPROVAL", "APPROVED_SEND_FAILED"):
                return {"success": False, "code": 409, "error": f"The minutes cannot be approved while they are {row.status}."}
            recipients = self._recipients(db, m)
            if not recipients:
                return {"success": False, "code": 409, "error": "There are no participants to send the minutes to."}
            sender = None
            if not settings.DEMO_MODE:
                from connectors.google_auth import organizer_status
                organizer = db.query(User).filter(User.id == m.organizer_id).first()
                status = organizer_status(recipients, organizer_hint=organizer.email if organizer else None, verify=True)
                if not status["ready"]:
                    return {"success": False, "code": 409, "error": status["error"]}
                sender = status["organizer_email"]
            was_retry = row.status == "APPROVED_SEND_FAILED"
            if not self._claim(db, row.id, ["AWAITING_ORGANIZER_APPROVAL", "APPROVED_SEND_FAILED"], "DISTRIBUTING"):
                return {"success": False, "code": 409, "error": "The minutes are already being sent."}
            # Approval: action items become real tasks only now.
            db.query(ActionItem).filter(ActionItem.meeting_id == meeting_id, ActionItem.finalized.is_(False)) \
                .update({"finalized": True}, synchronize_session=False)
            if not was_retry:
                row.approved_at, row.approved_by = datetime.utcnow(), approver_id
            db.commit()
            db.refresh(row)
            items = db.query(ActionItem).filter(ActionItem.meeting_id == meeting_id, ActionItem.finalized.is_(True)) \
                .order_by(ActionItem.id.asc()).all()
            mail = self.render_email(m, json.loads(row.mom_draft or "{}"), items)
            res = mcp_call("gmail.send", {"to_emails": recipients, "subject": mail["subject"], "body": mail["body"],
                                          "meeting_id": meeting_id, "sender_email": sender})
            if not res.get("success"):
                row.status, row.last_error = "APPROVED_SEND_FAILED", f"The minutes were approved but not sent: {res.get('error')}"
                db.commit()
                audit_memory.record_event("Human Operator", "SEND_MINUTES", meeting_id=meeting_id, status="ERROR",
                                          error=res.get("error") or "", details={"recipients": recipients})
                return {"success": False, "code": 502, "error": row.last_error}
            row.status, row.last_error, row.distributed_at = "APPROVED_AND_DISTRIBUTED", "", datetime.utcnow()
            db.commit()
            audit_memory.record_event("Human Operator", "SEND_MINUTES", meeting_id=meeting_id,
                                      details={"recipients": recipients, "message_id": res.get("message_id")})
            return {"success": True, "recipients": recipients}
        finally:
            db.close()
