# System Architecture: AI Meeting Organizer Agent

The **AI Meeting Organizer Agent** is an academic and enterprise-grade full-stack autonomous system implementing Labs 1 through 8 from the Agentic AI Lab Guide as a single, progressively layered application.

---

## 1. High-Level System Overview

```
                      +---------------------------------------+
                      |         REACT FRONTEND (VITE)         |
                      | Employees view | Meeting Organizer view|
                      +-------------------+-------------------+
                                          | HTTP / JSON-RPC
                                          v
                      +---------------------------------------+
                      |        FASTAPI REST / MCP API         |
                      +-------------------+-------------------+
                                          |
                      +-------------------v-------------------+
                      |      AGENT RUNTIME LOOP (Lab 6)       |
                      +-------------------+-------------------+
                                          |
               +--------------------------+--------------------------+
               |                                                     |
+--------------v---------------+                       +-------------v--------------+
|   LANGGRAPH WORKFLOW (Lab 7) |                       | PARALLEL AGENT SWARM (Lab 8)|
|  Conditional Graph Nodes     |                       | Participant, Scheduling,    |
|  Hard Approval Gate          |                       | Agenda, Resource Agents     |
+--------------+---------------+                       +-------------+--------------+
               |                                                     |
               +--------------------------+--------------------------+
                                          |
                               +----------v----------+
                               | MERGE & CRITIQUE    |
                               | CONFLICT RESOLUTION |
                               +----------+----------+
                                          |
    +-------------------------------------+-------------------------------------+
    |                                     |                                     |
+---v------------------+       +----------v----------+       +------------------v---+
| STRUCTURED TOOLS (L2)|       | REUSABLE SKILLS (L3)|       | MEMORY & RAG (Lab 4) |
| 12 Pydantic Tools    |       | Sched, Agenda, Val  |       | Session, L-Term, pgvec|
+---+------------------+       +---------------------+       +------------------+---+
    |
+---v-------------------------------------------------------------------------------+
|                      CONNECTOR / ADAPTER LAYER (Lab 5 MCP)                        |
|   DEMO_MODE=true (Mock Services)  <--->  DEMO_MODE=false (Google/Gmail APIs)      |
+-----------------------------------------------------------------------------------+
```

---

## 2. Core Subsystems

### 2.1 Unified frontend
Both views live in ONE frontend application (http://localhost:5173) with in-app navigation; they share the same
backend and database.

- **Employees view (formerly Dashboard 1)**:
  - Captures Employee ID, Name, Email, Designation, Department, Working days/hours, Timezone, Preferences, Mode preference, and Office location.
  - Stored in PostgreSQL with pgvector embeddings for semantic context.
  - Google Calendar OAuth integration status.
- **Meeting Organizer view (formerly Dashboard 2)**:
  - Natural-language scheduling prompt intake.
  - Interactive visualization of extracted slots, participants, and room assignments.
  - Live Conflict & Critique viewer showing multi-agent dispute resolution.
  - Human-in-the-loop Hard Gate (`[APPROVE]`, `[EDIT]`, `[REJECT]`).
  - Asynchronous pause/resume simulator.

### 2.2 Agent Swarm (Lab 8)
- **Meeting Coordinator**: Workflow driver, NLP entity extraction, agent dispatching, merging, critique, and conflict resolution.
- **Participant Agent**: Employee directory search, disambiguation of identical names, and detection of unregistered individuals without data fabrication.
- **Scheduling & Availability Agent**: Interrogates calendar adapters (strictly non-RAG for availability) and aligns with working hours.
- **Agenda Agent**: Prepares structured, minute-allocated meeting agendas when purpose is supplied; prompts or allows skipping when missing.
- **Resource/Room Agent**: Provisions Google Meet rooms or requests physical facilities/auditoriums with capacity checking.
- **Validation Agent**: Enforces all environmental and organizational constraints prior to human approval.

### 2.3 Worker Subsystem (`worker/`)
- Persistent state management across asynchronous cycles.
- Handles deferred participant responses (accept/reject) and delayed facility room manager sign-offs.
- Resumes the **exact same meeting workflow** without creating redundant entities.

### 2.4 Live request path (LangGraph)

The API's `/api/meetings/orchestrate` runs the LangGraph workflow in `backend/graph/workflow_graph.py`:

```
START ─► intake ─► parse ─► identify participants ─► retrieve RAG context ─┬─► ask for information ─► END (blocked draft)
                                                                            └─► availability ─► (alternative slot) ─► agenda
─► resource ─► validate + critique + conflicts ─► draft plan ─► HUMAN APPROVAL GATE ─► END (paused, WAITING_FOR_HUMAN_APPROVAL)

APPROVE  ─► START ─► approval gate (checks the persisted approval) ─► execute ─► wait ─► END
RESPONSE ─► START ─► process responses ─► revalidate ─► finalize ─► END          (same meeting id every time)
EDIT / participant resolved ─► START ─► retrieve context ─► availability … ─► draft ─► gate ─► END
```

- State is persisted in the existing `meetings` / `meeting_participants` / `room_bookings` tables; the resumable
  workflow snapshot lives in `meetings.parsed_details.workflow`.
- Every external capability (availability, slot search, event creation + Meet, Gmail, room request, event read,
  inbox read) goes through the MCP gateway. Consequential capabilities are only executed after human approval.
- RAG context (related past meetings, participants' stored preferences/working hours) is placed in the graph state and
  read by the Scheduling, Agenda and Coordinator (critique) agents.
