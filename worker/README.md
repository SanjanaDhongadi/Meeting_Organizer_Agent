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

## Supported Workflow States

- `DRAFT`: Initial extracted proposal
- `WAITING_FOR_HUMAN_APPROVAL`: Proposal ready for human gate
- `APPROVED`: Human approved, awaiting background processing
- `CHECKING_AVAILABILITY`: Real-time calendar verification
- `WAITING_FOR_PARTICIPANTS`: Waiting for external attendees to accept
- `WAITING_FOR_AUDITORIUM_RESPONSE`: Waiting for an auditorium booking response
- `READY_TO_FINALIZE`: All prerequisites verified
- `RESCHEDULING_REQUIRED`: Conflict, rejection, or room unavailability encountered
- `BOOKED`: Meeting officially scheduled with event ID and Meet/Room link
- `COMPLETED`: Meeting concluded
- `REJECTED`: Explicitly rejected by human gate
- `FAILED`: Fatal execution error

## Running the Worker

```bash
# Standalone background execution
python -m worker.worker
```
