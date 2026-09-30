# System Architecture: AI Meeting Organizer Agent

The **AI Meeting Organizer Agent** is an academic and enterprise-grade full-stack autonomous system implementing Labs 1 through 8 from the Agentic AI Lab Guide as a single, progressively layered application.

---

## 1. High-Level System Overview

```
                      +---------------------------------------+
                      |         REACT FRONTEND (VITE)         |
                      |  Dashboard 1 (Emp) | Dashboard 2 (Mtg)|
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

### 2.1 Dashboards
- **Dashboard 1 (Person / Employee Information)**:
  - Captures Employee ID, Name, Email, Designation, Department, Working days/hours, Timezone, Preferences, Mode preference, and Office location.
  - Stored in PostgreSQL with pgvector embeddings for semantic context.
  - Google Calendar OAuth integration status.
- **Dashboard 2 (Meeting Organizer)**:
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
