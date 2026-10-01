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
  (`calendar.get_event`) and auditorium replies from Gmail (`gmail.read`). Simulated responses from the UI are
  stored with source `SIMULATED`; real ones with `GOOGLE_CALENDAR` / `GMAIL_REPLY`.

## Supported Workflow States

- `DRAFT`: Initial extracted proposal
- `WAITING_FOR_HUMAN_APPROVAL`: Proposal ready for human gate
- `APPROVED`: Human approved, awaiting background processing
- `EXECUTING`: Approved actions are being executed (claimed by one process)
- `ACTION_FAILED`: An approved external action failed; the real error is stored on the meeting
- `CHECKING_AVAILABILITY`: Real-time calendar verification
- `WAITING_FOR_PARTICIPANTS`: Waiting for external attendees to accept
- `WAITING_FOR_AUDITORIUM_RESPONSE`: Waiting for an auditorium booking response
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
