# Backend: MEETING ORGANIZER AGENT

FastAPI and LangGraph backend for the MEETING ORGANIZER AGENT.

## Modules

- `app/`: FastAPI application, configuration, SQLAlchemy database models, seed scripts.
- `agents/`: Meeting Coordinator, Participant Agent, Scheduling Agent, Agenda Agent, Resource Agent, Validation Agent.
- `skills/`: Reusable skills (Meeting Scheduling Skill, Agenda Preparation Skill, Validation Skill).
- `tools/`: 12 typed tools with Pydantic input/output schemas.
- `connectors/`: CalendarService, EmailService, RoomService adapters (Mock & Real Google/Gmail) and MCP Gateway.
- `memory/`: Session memory, employee preference retrieval, and internal audit memory.
- `graph/`: Explicit LangGraph workflow state machine with conditional routing.
- `runtime/`: Reusable agent runtime loop.

## Running Locally

```bash
# Start backend server
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```
