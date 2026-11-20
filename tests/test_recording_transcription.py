"""
Browser-recorded audio as an alternative to Google's native Meet transcript.

Only the speech-to-text call (TranscriptionService.transcribe_file), Google Meet (the Meet service) and the LLM
(MoMAgent._complete) are faked. The upload API, storage, worker job, state machine, LangGraph MoM workflow,
approval and email distribution all run unchanged.
"""
import json
import os
import types
from datetime import datetime, timedelta

import pytest

from app.config import settings
from backend.app.db import ActionItem, MeetingMinutes, RecordingSegment, SessionLocal
from backend.connectors.transcription_service import TranscriptionService, format_lines, transcription_service
# Reused fixtures/helpers from the transcript tests (same boundaries, same fakes).
from test_minutes_and_action_items import (  # noqa: F401
    LLM_OUTPUT, PROCESSING, READY, TRANSCRIPT, _llm, _meeting, _row, _set, _user, client, fake_meet, job, setup,
)

SEGMENT_SECONDS = 300
AUDIO = b"\x1aE\xdf\xa3fake-webm-audio"


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RECORDINGS_DIR", str(tmp_path / "recordings"))
    monkeypatch.setattr(settings, "RECORDING_RETENTION_DAYS", 7)
    monkeypatch.setattr(settings, "GROQ_API_KEY", "test-key")
    return tmp_path / "recordings"


class FakeSTT:
    """Stands in for the Whisper call. Each segment's audio bytes decide its text, so tests control the result."""
    def __init__(self, monkeypatch, failures=None):
        self.calls, self.failures = [], dict(failures or {})
        monkeypatch.setattr(transcription_service, "transcribe_file", self)

    def __call__(self, path, mime_type="audio/webm", offset_seconds=0.0):
        self.calls.append((os.path.basename(path), offset_seconds))
        name = os.path.basename(path)
        remaining = self.failures.get(name, 0)
        if remaining:
            self.failures[name] = remaining - 1 if remaining > 0 else remaining
            return {"success": False, "retryable": remaining > 0, "error": "Rate limited" if remaining > 0 else "bad audio", "code": "X"}
        with open(path, "rb") as handle:
            body = handle.read().decode("latin-1")
        lines = [line for line in body.split("|")[1:]] or [""]
        return {"success": True, "retryable": False,
                "text": format_lines([{"start": 5 * n, "text": line} for n, line in enumerate(lines)], offset_seconds)}


def _audio(*lines):
    return AUDIO + b"|" + b"|".join(line.encode("latin-1", "replace") for line in lines)


def _start(org, mid, consent=True):
    return client.post(f"/api/meetings/{mid}/recording/start", headers=org["headers"], json={"consent": consent})


def _upload(org, mid, index, data=None, mime="audio/webm;codecs=opus", offset=None):
    data = data if data is not None else _audio(f"words in segment {index}")
    return client.post(f"/api/meetings/{mid}/recording/segments", headers=org["headers"],
                       data={"segment_index": str(index), "offset_seconds": str(index * SEGMENT_SECONDS if offset is None else offset),
                             "duration_seconds": str(SEGMENT_SECONDS)},
                       files={"file": (f"s{index}.webm", data, mime)})


def _finish(org, mid, count=None):
    return client.post(f"/api/meetings/{mid}/recording/finish", headers=org["headers"], json={"segment_count": count})


def _segments(mid):
    db = SessionLocal()
    try:
        return db.query(RecordingSegment).filter(RecordingSegment.meeting_id == mid).order_by(RecordingSegment.segment_index).all()
    finally:
        db.close()


def _record_meeting(alice, mid, *chunks):
    assert _start(alice, mid).status_code == 200
    for i, chunk in enumerate(chunks):
        assert _upload(alice, mid, i, chunk).status_code == 200
    return _finish(alice, mid, len(chunks))


MEETING_AUDIO = [_audio(*TRANSCRIPT.splitlines()[:3]), _audio(*TRANSCRIPT.splitlines()[3:])]


# ----------------- start: consent, ownership, native transcript first -----------------

def test_start_needs_consent_the_organizer_and_an_online_meeting(setup, fake_meet):
    alice, bob, mid = setup
    fake_meet(PROCESSING)
    assert _start(alice, mid, consent=False).status_code == 422
    assert _start(bob, mid).status_code == 403
    assert client.post(f"/api/meetings/{mid}/recording/start", json={"consent": True}).status_code in (401, 403)
    assert _upload(alice, mid, 0).status_code == 409                       # nothing is recorded until the organizer starts
    offline = _meeting(alice, [], mode="OFFLINE", title="Room meeting")
    assert _start(alice, offline).status_code == 400
    assert _row(offline) is None                                           # offline meetings never get a recording row
    ok = _start(alice, mid)
    assert ok.status_code == 200 and ok.json()["status"] == "RECORDING_IN_PROGRESS" and not ok.json()["native_transcript"]
    assert _start(alice, mid).status_code == 200                           # idempotent
    assert "consent_confirmed_at" in json.loads(_row(mid).transcript_meta)


def test_a_native_google_transcript_is_used_when_one_exists(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    fake_meet(READY)
    _llm(monkeypatch, LLM_OUTPUT)
    res = _start(alice, mid)
    assert res.status_code == 200 and res.json()["native_transcript"] is True
    row = _row(mid)
    assert row.status == "AWAITING_ORGANIZER_APPROVAL" and json.loads(row.transcript_meta)["source"] == "GOOGLE_MEET"
    assert _upload(alice, mid, 0).status_code == 409                       # no second transcript can be added


def test_choosing_to_record_stops_waiting_for_google(setup, fake_meet):
    alice, bob, mid = setup
    meet = fake_meet(PROCESSING)
    assert _start(alice, mid).status_code == 200
    calls = meet.calls
    _set(mid, next_attempt_at=None)
    job.scan()
    job.scan()
    assert meet.calls == calls and _row(mid).status == "RECORDING_IN_PROGRESS"


def test_a_recording_started_during_a_google_check_is_never_overwritten(setup, fake_meet):
    alice, bob, mid = setup

    class RacingMeet:
        calls = 0

        def get_transcript(self, *args, **kwargs):
            RacingMeet.calls += 1
            assert job.recording_start(mid)["success"]                     # the organizer clicks Record mid-request
            return READY

    from backend.connectors import mcp_gateway as gateway_module
    gateway_module.get_meet_service = lambda: RacingMeet()
    assert job.retrieve(mid).get("skipped")
    row = _row(mid)
    assert row.status == "RECORDING_IN_PROGRESS" and not row.transcript_text


# ----------------- upload -----------------

def test_upload_validates_and_is_idempotent(setup, fake_meet, storage, monkeypatch):
    alice, bob, mid = setup
    fake_meet(PROCESSING)
    _start(alice, mid)
    assert _upload(alice, mid, 0, mime="application/pdf").status_code == 415
    assert _upload(alice, mid, 0, data=b"").status_code == 422
    monkeypatch.setattr(settings, "RECORDING_MAX_SEGMENT_MB", 0)
    assert _upload(alice, mid, 0).status_code == 413
    monkeypatch.setattr(settings, "RECORDING_MAX_SEGMENT_MB", 24)
    assert _upload(bob, mid, 0).status_code == 403                         # a participant cannot add audio
    first = _upload(alice, mid, 0)
    again = _upload(alice, mid, 0)                                         # network retry of the same segment
    assert first.json()["duplicate"] is False and again.json()["duplicate"] is True
    assert len(_segments(mid)) == 1 and os.path.exists(_segments(mid)[0].storage_path)
    assert not [p for p in storage.rglob("*") if p.suffix == ".part"]       # atomic writes leave no partial files
    # audio is on disk and in metadata only, never in a database column
    assert _segments(mid)[0].size_bytes == len(_audio("words in segment 0"))


def test_finish_requires_every_segment_and_is_duplicate_safe(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    fake_meet(PROCESSING)
    stt = FakeSTT(monkeypatch)
    _llm(monkeypatch, LLM_OUTPUT)
    assert _finish(alice, mid).status_code in (404, 409)              # nothing was started
    _start(alice, mid)
    assert _finish(alice, mid).status_code == 409                          # nothing uploaded
    _upload(alice, mid, 0, MEETING_AUDIO[0])
    _upload(alice, mid, 2, MEETING_AUDIO[1])
    missing = _finish(alice, mid, 3)
    assert missing.status_code == 409 and "[1]" in missing.json()["detail"]
    assert stt.calls == [] and _row(mid).status == "RECORDING_IN_PROGRESS"
    assert _upload(alice, mid, 1, _audio("middle")).status_code == 200
    done = _finish(alice, mid, 3)
    assert done.status_code == 200
    again = _finish(alice, mid, 3)
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert _upload(alice, mid, 5, _audio("late")).status_code == 409        # recording is closed
    assert len(stt.calls) == 3                                             # each segment transcribed exactly once


# ----------------- transcription, retries, chunking, MoM -----------------

def test_recording_to_approved_and_distributed_minutes(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    fake_meet(PROCESSING)
    FakeSTT(monkeypatch)
    _llm(monkeypatch, LLM_OUTPUT)
    assert _record_meeting(alice, mid, *MEETING_AUDIO).status_code == 200

    row = _row(mid)
    assert row.status == "AWAITING_ORGANIZER_APPROVAL"                      # LangGraph MoM ran automatically
    meta = json.loads(row.transcript_meta)
    assert meta["source"] == "RECORDING" and meta["speaker_attribution"] == "none" and meta["segments"] == 2
    assert row.transcript_text.count("Speaker:") == len(TRANSCRIPT.splitlines())    # neutral labels, nothing invented
    assert "[00:05:00] Speaker:" in row.transcript_text                      # segment 2 is offset by 300 s
    draft = json.loads(row.mom_draft)
    assert [d["text"] for d in draft["decisions"]] == ["Go with the second vendor."]    # still grounded in the transcript

    view = client.get(f"/api/meetings/{mid}/minutes", headers=alice["headers"]).json()
    assert view["recording"]["transcription_configured"] is True and len(view["recording"]["segments"]) == 2
    items = {i["description"]: i for i in view["action_items"]}
    assert items["Send the revised budget"]["owner_email"] == bob["email"] and items["Send the revised budget"]["due_date"] == "2026-12-04"
    assert items["Update the risk log"]["owner_email"] is None and items["Update the risk log"]["due_date"] is None

    # Nothing is distributed or visible to participants before approval.
    assert client.get("/api/action-items?scope=mine", headers=bob["headers"]).json() == []
    edit = client.put(f"/api/meetings/{mid}/minutes", headers=alice["headers"], json={"summary": "Organizer edited summary."})
    assert edit.status_code == 200
    approved = client.post(f"/api/meetings/{mid}/minutes/approve", headers=alice["headers"])
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED_AND_DISTRIBUTED"
    assert [t["description"] for t in client.get("/api/action-items?scope=mine", headers=bob["headers"]).json()] == ["Send the revised budget"]


def test_duplicate_processing_creates_one_transcript_and_one_set_of_action_items(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    fake_meet(PROCESSING)
    stt = FakeSTT(monkeypatch)
    _llm(monkeypatch, LLM_OUTPUT)
    _record_meeting(alice, mid, *MEETING_AUDIO)
    transcript = _row(mid).transcript_text
    before = len(stt.calls)
    for _ in range(3):                                                    # worker cycles, API retries, double clicks
        job.process_recording(mid)
        job.scan()
        assert _finish(alice, mid, 2).status_code == 200
    assert client.post(f"/api/meetings/{mid}/recording/retry", headers=alice["headers"]).status_code == 409
    assert len(stt.calls) == before and _row(mid).transcript_text == transcript
    db = SessionLocal()
    try:
        assert db.query(ActionItem).filter(ActionItem.meeting_id == mid).count() == 2
        assert db.query(MeetingMinutes).filter(MeetingMinutes.meeting_id == mid).count() == 1
    finally:
        db.close()


def test_transient_transcription_failure_is_retried_without_redoing_finished_segments(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    fake_meet(PROCESSING)
    stt = FakeSTT(monkeypatch, failures={"segment_0001.webm": 2})
    _llm(monkeypatch, LLM_OUTPUT)
    _record_meeting(alice, mid, *MEETING_AUDIO)
    row = _row(mid)
    assert row.status == "AUDIO_UPLOADED" and row.retrieval_attempts == 1 and "Rate limited" in row.last_error
    assert row.next_attempt_at > datetime.utcnow()                         # backed off, not hammered
    assert [s.status for s in _segments(mid)] == ["TRANSCRIBED", "UPLOADED"]
    job.scan()                                                             # not due yet
    assert _row(mid).retrieval_attempts == 1
    _set(mid, next_attempt_at=datetime.utcnow() - timedelta(seconds=1))
    job.scan()                                                             # second failure
    assert _row(mid).retrieval_attempts == 2 and _row(mid).status == "AUDIO_UPLOADED"
    _set(mid, next_attempt_at=datetime.utcnow() - timedelta(seconds=1))
    job.scan()                                                             # succeeds and carries on to the MoM
    assert _row(mid).status == "AWAITING_ORGANIZER_APPROVAL"
    assert [name for name, _ in stt.calls].count("segment_0000.webm") == 1


def test_permanent_failure_stops_and_the_organizer_can_retry(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    fake_meet(PROCESSING)
    stt = FakeSTT(monkeypatch, failures={"segment_0000.webm": -1})        # negative: permanent failure
    _llm(monkeypatch, LLM_OUTPUT)
    _record_meeting(alice, mid, *MEETING_AUDIO)
    row = _row(mid)
    assert row.status == "TRANSCRIPTION_FAILED" and "bad audio" in row.last_error
    n = len(stt.calls)
    job.scan()
    assert len(stt.calls) == n                                             # no endless automatic retries
    stt.failures.clear()
    res = client.post(f"/api/meetings/{mid}/recording/retry", headers=alice["headers"])
    assert res.status_code == 200
    assert _row(mid).status == "AWAITING_ORGANIZER_APPROVAL"
    assert client.post(f"/api/meetings/{mid}/recording/retry", headers=client_other(mid)).status_code == 403


def client_other(mid):
    return _user("Mallory")["headers"]


def test_retries_are_capped(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    fake_meet(PROCESSING)
    monkeypatch.setattr(settings, "TRANSCRIPTION_MAX_ATTEMPTS", 2)
    FakeSTT(monkeypatch, failures={"segment_0000.webm": 99})
    _record_meeting(alice, mid, MEETING_AUDIO[0])
    _set(mid, next_attempt_at=datetime.utcnow() - timedelta(seconds=1))
    job.scan()
    assert _row(mid).status == "TRANSCRIPTION_FAILED"


def test_a_silent_recording_is_reported_not_turned_into_minutes(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    fake_meet(PROCESSING)
    FakeSTT(monkeypatch)
    _record_meeting(alice, mid, AUDIO)                                     # no speech in the audio
    row = _row(mid)
    assert row.status == "TRANSCRIPTION_FAILED" and "No speech" in row.last_error and not row.transcript_text


def test_long_recordings_are_chunked_and_keep_timestamps_in_order(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    fake_meet(PROCESSING)
    FakeSTT(monkeypatch)
    _llm(monkeypatch, LLM_OUTPUT)
    chunks = [_audio(f"minute marker {i}") for i in range(30)]               # 30 x 5 min = 2.5 hours
    assert _record_meeting(alice, mid, *chunks).status_code == 200
    row = _row(mid)
    lines = row.transcript_text.splitlines()
    assert len(lines) == 30 and len(_segments(mid)) == 30
    assert lines[0].startswith("[00:00:00] Speaker: minute marker 0")
    assert lines[29].startswith("[02:25:00] Speaker: minute marker 29")
    assert json.loads(row.transcript_meta)["recorded_seconds"] == 30 * SEGMENT_SECONDS


def test_interrupted_recording_can_be_finished_with_what_was_uploaded(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    fake_meet(PROCESSING)
    FakeSTT(monkeypatch)
    _llm(monkeypatch, LLM_OUTPUT)
    _start(alice, mid)
    _upload(alice, mid, 0, MEETING_AUDIO[0])
    _upload(alice, mid, 2, MEETING_AUDIO[1])                               # segment 1 was lost (tab closed / network)
    assert _finish(alice, mid).status_code == 200                          # no count: use what exists
    assert json.loads(_row(mid).transcript_meta)["incomplete"] is True       # the gap is recorded, not hidden


# ----------------- discard, deletion, retention -----------------

def test_discard_returns_to_waiting_for_google_and_removes_audio(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    meet = fake_meet(PROCESSING)
    _start(alice, mid)
    _upload(alice, mid, 0)
    path = _segments(mid)[0].storage_path
    assert client.delete(f"/api/meetings/{mid}/recording/audio", headers=alice["headers"]).status_code == 409   # still needed
    assert client.post(f"/api/meetings/{mid}/recording/discard", headers=alice["headers"]).status_code == 200
    assert not os.path.exists(path) and _segments(mid) == []
    row = _row(mid)
    assert row.status == "WAITING_FOR_TRANSCRIPT" and "source" not in json.loads(row.transcript_meta)
    before = meet.calls
    job.scan()
    assert meet.calls == before + 1                                        # Google polling resumed
    assert client.post(f"/api/meetings/{mid}/recording/discard", headers=alice["headers"]).status_code == 409


def test_raw_audio_retention_and_manual_deletion(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    fake_meet(PROCESSING)
    FakeSTT(monkeypatch)
    _llm(monkeypatch, LLM_OUTPUT)
    _record_meeting(alice, mid, *MEETING_AUDIO)
    paths = [s.storage_path for s in _segments(mid)]
    assert all(os.path.exists(p) for p in paths)                           # kept for RECORDING_RETENTION_DAYS
    assert job.purge_expired_audio() == 0
    db = SessionLocal()
    try:
        db.query(RecordingSegment).filter(RecordingSegment.meeting_id == mid) \
            .update({"transcribed_at": datetime.utcnow() - timedelta(days=8)})
        db.commit()
    finally:
        db.close()
    assert job.purge_expired_audio() == 2
    assert not any(os.path.exists(p) for p in paths)
    assert all(s.audio_deleted_at and not s.storage_path for s in _segments(mid))
    assert _row(mid).transcript_text                                       # the transcript is kept

    other = _meeting(alice, [{"name": "Bob Smith", "email": bob["email"]}], title="Second")
    _start(alice, other)
    monkeypatch.setattr(settings, "RECORDING_RETENTION_DAYS", 0)           # delete immediately after transcription
    _record_meeting_started(alice, other)
    assert all(not os.path.exists(s.storage_path) for s in _segments(other) if s.storage_path)
    assert all(s.audio_deleted_at for s in _segments(other))


def _record_meeting_started(alice, mid):
    for i, chunk in enumerate(MEETING_AUDIO):
        assert _upload(alice, mid, i, chunk).status_code == 200
    assert _finish(alice, mid, 2).status_code == 200


def test_delete_audio_endpoint_after_transcription(setup, fake_meet, monkeypatch):
    alice, bob, mid = setup
    fake_meet(PROCESSING)
    FakeSTT(monkeypatch)
    _llm(monkeypatch, LLM_OUTPUT)
    _record_meeting(alice, mid, *MEETING_AUDIO)
    assert client.delete(f"/api/meetings/{mid}/recording/audio", headers=_user("Eve")["headers"]).status_code == 403
    res = client.delete(f"/api/meetings/{mid}/recording/audio", headers=alice["headers"])
    assert res.status_code == 200 and res.json()["deleted_files"] == 2


# ----------------- the Whisper adapter itself -----------------

def _fake_openai(monkeypatch, behaviour):
    service = TranscriptionService()
    create = behaviour if callable(behaviour) else (lambda **kw: behaviour)
    client_stub = types.SimpleNamespace(audio=types.SimpleNamespace(transcriptions=types.SimpleNamespace(create=create)))
    monkeypatch.setattr(service, "_client", lambda: client_stub)
    return service


def test_whisper_adapter_formats_timestamps_with_neutral_speakers(tmp_path, monkeypatch):
    audio = tmp_path / "a.webm"
    audio.write_bytes(AUDIO)
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace(text="x", segments=[types.SimpleNamespace(start=3.2, text=" Hello team. "),
                                                         {"start": 65.0, "text": "Budget is approved."}, {"start": 70, "text": "  "}])

    result = _fake_openai(monkeypatch, create).transcribe_file(str(audio), "audio/webm;codecs=opus", offset_seconds=600)
    assert result["success"] and result["text"] == "[00:10:03] Speaker: Hello team.\n[00:11:05] Speaker: Budget is approved."
    assert seen["model"] == settings.GROQ_TRANSCRIPTION_MODEL and seen["response_format"] == "verbose_json"
    plain = _fake_openai(monkeypatch, types.SimpleNamespace(text="Only text", segments=None))
    assert plain.transcribe_file(str(audio))["text"] == "[00:00:00] Speaker: Only text"


def test_whisper_adapter_reports_configuration_and_classifies_errors(tmp_path, monkeypatch):
    audio = tmp_path / "a.webm"
    audio.write_bytes(AUDIO)
    monkeypatch.setattr(settings, "GROQ_API_KEY", None)
    missing = TranscriptionService().transcribe_file(str(audio))
    assert not missing["success"] and not missing["retryable"] and "GROQ_API_KEY" in missing["error"]
    monkeypatch.setattr(settings, "GROQ_API_KEY", "sk-secret")

    class ApiError(Exception):
        def __init__(self, status):
            super().__init__(f"http {status}")
            self.status_code = status

    def failing(status):
        def raise_it(**kwargs):
            raise ApiError(status)
        return raise_it

    assert _fake_openai(monkeypatch, failing(429)).transcribe_file(str(audio))["retryable"] is True
    assert _fake_openai(monkeypatch, failing(503)).transcribe_file(str(audio))["retryable"] is True
    bad_key = _fake_openai(monkeypatch, failing(401)).transcribe_file(str(audio))
    assert not bad_key["retryable"] and bad_key["code"] == "TRANSCRIPTION_AUTH" and "sk-secret" not in bad_key["error"]
    assert not _fake_openai(monkeypatch, failing(400)).transcribe_file(str(audio))["retryable"]


def test_offline_meetings_are_unaffected_by_recording(setup, fake_meet):
    alice, bob, mid = setup
    fake_meet(READY)
    offline = _meeting(alice, [{"name": "Bob Smith", "email": bob["email"]}], mode="OFFLINE", title="In person")
    job.scan()
    assert _row(offline) is None
    assert _start(alice, offline).status_code == 400
    assert _upload(alice, offline, 0).status_code == 409
