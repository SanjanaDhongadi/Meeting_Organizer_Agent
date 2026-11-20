# Backend: MEETING ORGANIZER AGENT

FastAPI and LangGraph backend for the MEETING ORGANIZER AGENT.

## Modules

- `app/`: FastAPI application, configuration (`config.py`, all values from `.env`), SQLAlchemy models.
- `graph/`: the LangGraph workflow used by the live API. New requests run intake → parse → participants →
  RAG context → availability → (alternative slot) → agenda → resource → validate/critique → draft → approval gate.
  Approval, edits and asynchronous responses resume the SAME persisted meeting at the gate / response step.
- `agents/`: Meeting Coordinator, Participant, Scheduling, Agenda, Resource, Validation agents.
- `skills/`: Meeting Scheduling, Agenda Preparation, Validation skills.
- `tools/`: 12 typed tools; calendar, email and room tools call the MCP gateway.
- `connectors/`: MCP gateway plus Google Calendar/Meet, Gmail, email-based venue request adapters, and OAuth helpers.
  Mock adapters exist for tests only (`DEMO_MODE=true`).
- Minutes of Meeting: `connectors/meet_service.py` (transcripts), `agents/mom_agent.py` + `graph/mom_graph.py` (draft MoM, grounded in the
  transcript), `app/routes_minutes.py` (review/approve, action-item tracker, follow-up suggestions), `app/action_items.py` (rules and
  carry-forward ranking). Browser-recorded audio (`app/recordings.py` storage, `connectors/transcription_service.py` Groq Whisper adapter) is the
  alternative to Google transcripts. Tables: `meeting_minutes`, `action_items`, `recording_segments`.
- `memory/`: session, long-term (RAG) and audit memory.
- `runtime/`: agent runtime; `run_meeting_request` runs the LangGraph workflow.

## Running Locally

```bash
pip install -r backend/requirements.txt
cp .env.example .env        # fill in Google OAuth client, SECRET_KEY, etc.
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
python -m worker.worker     # optional: background execution + real response polling
pytest                      # uses DEMO_MODE and a separate test database
```
