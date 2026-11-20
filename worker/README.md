# Worker Service: AI Meeting Organizer Agent

The worker subsystem handles background asynchronous state machine progression, external response processing, and workflow resumption.

## Architecture

```
worker/
│
├── __init__.py
├── worker.py                       # Background polling loop & simulation dispatch
│
├── jobs/
│   ├── __init__.py
│   ├── meeting_job.py              # Background meeting finalization
│   ├── calendar_job.py             # Asynchronous calendar event dispatch
│   ├── email_job.py                # Asynchronous email delivery
│   ├── room_booking_job.py         # Room allocation verification
│   └── response_processing_job.py  # Processes external responses & resumes same meeting
│
├── events/
│   ├── __init__.py
│   ├── participant_events.py       # Participant response events
│   ├── room_events.py              # Facility room confirmation/rejection events
│   └── calendar_events.py          # Calendar synchronization events
│
├── state/
│   ├── __init__.py
│   └── state_manager.py            # Idempotency and database state transitions
│
└── README.md
```

## How it connects

- `MeetingJob` executes APPROVED meetings by resuming the LangGraph workflow at the approval gate
  (an atomic `APPROVED → EXECUTING` claim prevents double execution between the API and this worker).
- `ResponseProcessingJob` records a response on the existing meeting and resumes the same workflow
  (process responses → revalidate → finalize).
- Each cycle also reads **real** responses through MCP: attendee status from the Google Calendar event
  (`calendar.get_event`) and the venue coordinator's reply to a venue request from Gmail (`gmail.read`). Simulated responses from the UI are
  stored with source `SIMULATED`; real ones with `GOOGLE_CALENDAR` / `GMAIL_REPLY`.

## Minutes of Meeting (`jobs/minutes_job.py`)

Online (Google Meet) meetings only; in-person meetings are unaffected. Every cycle, `MinutesJob.scan()`:

1. creates a `meeting_minutes` row for each online meeting whose `scheduled_end` has passed (ignored once older than `MINUTES_MAX_WAIT_HOURS`);
2. retrieves the transcript through the internal-only MCP capability `meet.get_transcript` (Meet REST API v2) and stores it in PostgreSQL;
3. retries with back-off (5, 10, 15, 30, then every 60 minutes) until `MINUTES_MAX_WAIT_HOURS` (default 48) after the meeting ended, then marks `TRANSCRIPT_UNAVAILABLE` (the organizer can retry from the dashboard);
4. runs the MoM LangGraph workflow (`backend/graph/mom_graph.py`) and leaves the draft at `AWAITING_ORGANIZER_APPROVAL`.

Statuses: `WAITING_FOR_TRANSCRIPT`, `TRANSCRIPT_RETRIEVED`, `GENERATING_MOM`, `AWAITING_ORGANIZER_APPROVAL`, `APPROVED_AND_DISTRIBUTED`
(plus `TRANSCRIPT_UNAVAILABLE`, `MOM_FAILED`, `DISTRIBUTING`, `APPROVED_SEND_FAILED`). Each transition is a conditional UPDATE, so the worker, the API
and duplicate Pub/Sub notifications (`POST /api/meet/events`) can never double-process or double-send.

### Alternative: browser-recorded audio (no Google transcript needed)

Google only produces transcripts for Workspace accounts with transcription enabled, so personal Gmail meetings get none. The organizer can instead
press **Record** in the Minutes window: the browser (after an explicit share prompt) captures the Meet tab's audio plus the microphone, cuts it into
5-minute segments and uploads each to `POST /api/meetings/{id}/recording/segments` (audio is stored on disk under `RECORDINGS_DIR`; PostgreSQL keeps
`recording_segments` metadata and the transcript). Choosing to record stops Google polling for that meeting. `MinutesJob` then:

1. `recording_finish` (Stop) moves `RECORDING_IN_PROGRESS` to `AUDIO_UPLOADED`;
2. `transcribe` claims `TRANSCRIBING`, sends each not-yet-transcribed segment to Groq Whisper (`connectors/transcription_service.py`), and stores timestamped, neutrally labelled lines (`[hh:mm:ss] Speaker: text`; the mixed track cannot identify speakers);
3. transient failures retry with back-off (1, 2, 5, 10, 15 min, `TRANSCRIPTION_MAX_ATTEMPTS`) and only redo segments that failed; permanent failures stop at `TRANSCRIPTION_FAILED` until the organizer retries;
4. on success the status is `TRANSCRIPT_RETRIEVED` and the existing MoM workflow, approval and email distribution run unchanged;
5. raw audio is deleted `RECORDING_RETENTION_DAYS` after transcription (0 = at once) by `purge_expired_audio`, or earlier via `DELETE /api/meetings/{id}/recording/audio`.

Extra statuses: `RECORDING_IN_PROGRESS`, `AUDIO_UPLOADED`, `TRANSCRIBING`, `TRANSCRIPTION_FAILED`. In Docker the API and worker must share `RECORDINGS_DIR` (the compose file mounts a `recordings` volume).

## Supported Workflow States

- `DRAFT`: Initial extracted proposal
- `WAITING_FOR_HUMAN_APPROVAL`: Proposal ready for human gate
- `APPROVED`: Human approved, awaiting background processing
- `EXECUTING`: Approved actions are being executed (claimed by one process)
- `ACTION_FAILED`: An approved external action failed; the real error is stored on the meeting
- `CHECKING_AVAILABILITY`: Real-time calendar verification
- `WAITING_FOR_PARTICIPANTS`: Waiting for external attendees to accept
- `WAITING_FOR_AUDITORIUM_RESPONSE`: All attendees accepted; waiting for the venue coordinator to confirm the venue (in person)
- `READY_TO_FINALIZE`: All prerequisites verified
- `RESCHEDULING_REQUIRED`: Conflict, rejection, or room unavailability encountered
- `CONFIRMED`: Meeting finalized (all participants accepted / room confirmed)
- `BOOKED`: Legacy name for a finalized meeting
- `COMPLETED`: Meeting concluded
- `REJECTED`: Explicitly rejected by human gate
- `FAILED`: Fatal execution error

## Running the Worker

```bash
# Standalone background execution
python -m worker.worker
```
