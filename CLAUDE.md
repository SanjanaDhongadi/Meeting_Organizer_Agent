# CLAUDE.md

Operating guide for coding sessions on this repo (AI Meeting Organizer: FastAPI + LangGraph + worker + React).
Target spec: `docs/worklet.md` (team 8 Meeting Organizer worklet, Labs 1–14).

## 1. Working rules
- Make minimal, targeted diffs. No unrelated refactors, renames, reformatting or file moves.
- Never remove existing functionality. Do not change the tech stack.
- Ask the user before: database schema redesigns, new external services/dependencies, changes to LangGraph routing, deleting files.
- Inspect the relevant files and their callers before editing. State assumptions explicitly.
- Prefer new code in its own module/router (like `routes_minutes.py`) over growing `main.py`.
- Never claim something works unless it was executed. End every task with: files changed and why, commands run with results (tests before/after counts), and what remains unverified.

## 2. Verification
Run from the repo root (imports are absolute: `backend.app.main`, `worker.state...`).
- Tests: `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` (bash: `.venv/Scripts/python.exe ...`). One file/test: `-m pytest tests/unit/test_skills.py::test_name`, or `-k "name"`.
- `tests/conftest.py` forces `DEMO_MODE=true` and a separate SQLite DB (`tests/.test_meeting_organizer.db`) and seeds employees `TEST001`–`TEST005`. Tests must never need network, Google or Groq. Do not run two pytest processes at once. `conftest.py` blanks `GROQ_API_KEY` so a key in `.env` never reaches tests.
- **Baseline (2026-11-16, 234 tests, identical over repeated runs): 188 passed, 46 failed.** Failing files: `tests/integration/test_live_integration.py` (34), `tests/workflow/test_20_scenarios.py` (10), `tests/integration/test_employee_api.py` (1), `tests/integration/test_graph.py` (1).
- Most failures are stale expectations (removed employee-directory flow; names without emails now stop at `NEEDS_INFORMATION`). Exception: `test_oauth_rejects_tampered_state_and_account_mismatch` fails because of a real bug (section 7).
- Rule: **no new failures; every new feature gets new tests.** Compare against this baseline.
- Frontend: `cd frontend && npm run lint && npm run build`; recorder tests: `node --test src/services/meetingRecorder.test.js`.
- Dev: `uvicorn backend.app.main:app --port 8000 --reload`, `python -m worker.worker`, `cd frontend && npm run dev` (5173, proxies `/api`).

## 3. Current state (verified in code)
- **Participants**: `participant_agent.py` takes names + emails only, no directory lookup; `unknown_participants`/`ambiguous_participants` are always empty. Names without emails produce `NEEDS_INFORMATION` questions. Participants need not be registered.
- **Legacy, not in the UI** (`App.jsx` mounts Navbar, MeetingDashboard, AuthPage, ActionItemsPage): `UnknownParticipantModal.jsx`, `pages/EmployeeDashboard/`, endpoints `/resolve-participant`, `/recheck-participant`, `/remove-participant`. Do not build on them.
- **Availability (Lab 2)**: `node_check_availability` calls `SchedulingAgent` → `MeetingSchedulingSkill` → `check_calendar_availability` / `find_common_slots` tools → MCP → Google free/busy, for the organizer and every participant with their own connected Google credentials. Everyone else (external, unregistered, not connected, OAuth/API failure) is `UNVERIFIED`: no Google call, never free, never blocking, still invited by email. Stored statuses are `AVAILABLE` / `BUSY` / `UNVERIFIED`; UI labels come from `frontend/src/services/availability.js` ("Available", "Busy", "Availability not verified"). Organizer result: `organizer_availability` in the workflow snapshot. Also keep `state_manager.reserve_and_claim` (internal DB overlap at approval).
- **Docker is not maintained**: `docker-compose.yml`, Dockerfiles and `database/schema/schema.sql` do not match the ORM. Do not use or update them; do not tell the user to run docker compose.
- **Two graphs**: `backend/graph/workflow_graph.py` (scheduling lifecycle), `backend/graph/mom_graph.py` (minutes: load → generate → persist | fail).
- **Minutes** only for online Meet meetings (`is_minutes_eligible`): Meet transcript (`meet.get_transcript`) or browser recording transcribed by Groq Whisper.
- **RAG**: 64-dim hashing embeddings (`compute_simple_embedding`), similarity + lexical boost over employees and past CONFIRMED meetings (title/purpose/agenda). `record_completed_meeting` is a no-op (only logs); approved minutes are NOT indexed.
- `db.py` runs `Base.metadata.create_all` at import, in addition to Alembic.

## 4. Architecture
- Request path: FastAPI (`main.py`, `routes_minutes.py`) → `runtime/runtime_loop.py` → `workflow_graph.py` → agents → skills/tools → `mcp_call()`/`MCPGateway` → connectors (real or mock by `DEMO_MODE`).
- Scheduling graph: intake → parse → participants → RAG context → (ask for info → END) | availability (skipped) → (alternative slot) → agenda → resource → validate/critique → draft plan → approval gate → END at `WAITING_FOR_HUMAN_APPROVAL`.
- Locked plan: `node_check_availability` runs `PlanSkill` (`backend/skills/plan_skill.py`) and stores the frozen, hashed, versioned `MeetingPlan` in `parsed_details["plan"]`; an edit that changes content re-locks version+1, and `node_validate` adds "Draft differs from locked plan vN: <field>" warnings (never blocking). Invitation/minutes e-mail text comes from `FormatMeetingSkill`; minutes grounding lives in `MinutesOfMeetingSkill` (`MoMAgent._complete` stays the only LLM call).
- Governance (`backend/runtime/governance.py`): `governed_run(kind, meeting_id)` wraps a scheduling run (`workflow_graph._run`, `recursion_limit` = step budget) and a minutes run (`run_mom_generation`). Budgets `RUNTIME_MAX_STEPS`=50 / `RUNTIME_MAX_LLM_CALLS`=20 / `RUNTIME_MAX_SECONDS`=300; `workflow_node` counts a step per node, `groq_client.chat_json` counts every actual request; counters are no-ops outside a run; `BudgetExceeded` is an `LLMError` (parser/agenda fall back to rules; node failures become `WorkflowError` -> `ACTION_FAILED`). Exactly one `Agent Runtime` `RUN_COMPLETED`/`RUN_FAILED` audit event per run (usage + exceeded budget). Every node also writes `LangGraph / NODE_COMPLETED` (node, status before/after, sorted changed state KEYS, duration; never values).
- Checkpoints / recovery: `node_execute` persists `parsed_details["workflow"]["execution_steps"]` (`calendar`: event_id, `invitation`: message_id, each with `done` + UTC `at`) right after each external action succeeds and skips completed steps on resume (no second event, no second `reset_participant_responses`, no second invitation). `MeetingJob.execute` clears them on a fresh approval (after the `APPROVED -> EXECUTING` claim). `MeetingJob.recover_stuck()` (called by `process_cycle` after the APPROVED loop) resumes approved meetings stuck in `EXECUTING` for `EXECUTION_STUCK_MINUTES`=10 via an atomic `updated_at` lease and audits `RUN_RECOVERED`. Limit: a crash between an external success and its marker can repeat that action (Gmail has no idempotency key; the calendar step re-patches the existing event id when one is stored) - delivery is at-least-once, not exactly-once.
- Resume model: no checkpointer. `resume_meeting_workflow(meeting_id, action, **extra)`; `route_entry` routes EDIT/REPLAN/APPROVAL/RESPONSE. State rebuilt by `_state_from_db`; snapshot in `meetings.parsed_details.workflow`.
- Statuses: DRAFT → NEEDS_INFORMATION | WAITING_FOR_HUMAN_APPROVAL | RESCHEDULING_REQUIRED → APPROVED → EXECUTING → WAITING_FOR_PARTICIPANTS / WAITING_FOR_AUDITORIUM_RESPONSE → CONFIRMED; also SCHEDULED (no Google, internal), ACTION_FAILED, REJECTED, DELETED (soft). See `worker/README.md`.
- Approval gate: `node_human_approval` needs persisted `approval_status=APPROVED` + `status=EXECUTING`; `APPROVED → EXECUTING` claim is atomic.
- Worker `process_cycle` (every 5 s): run APPROVED meetings (`MeetingJob`) → `sync_external_responses` (Calendar RSVPs, Gmail venue replies; skipped in DEMO_MODE) → `MinutesJob.scan()`.
- Minutes/action items: `minutes_job.py` gets transcript → `mom_graph.py` + `MoMAgent` (only evidence-quoted decisions/items) → organizer approves in `MinutesPanel` → action items finalized + minutes emailed. Owner may change only task status. `action_items.py` suggests the organizer's open tasks for a new agenda (`compose_agenda` appends them).
- Auth: `auth.py` (PBKDF2, signed bearer tokens). Ownership = `meetings.organizer_id`. Google OAuth (PKCE) tokens stored on `User` (or legacy `Employee`).
- Models (`db.py`): User, Employee, Meeting, MeetingParticipant, RoomBooking, OAuthPendingState, AuditLog, MeetingMinutes, ActionItem, RecordingSegment.

## 5. Conventions for new code (must follow)
- External side effects only via `mcp_call()`/`MCPGateway`; consequential actions only for approved meetings/minutes. Never call Google APIs from agents or graph nodes.
- New/changed routes: `Depends(get_current_user)` + explicit ownership check like `_organizer_meeting` in `routes_minutes.py`. Never copy the `if current_user and ...` pattern from `main.py`.
- Schema change: model in `db.py` + `to_dict()` + Alembic migration with `down_revision = 'b5e2d4f81c36'` (current head; update this line when you add one). `create_all()` hides a missing migration in tests.
- Times: store UTC ISO strings ending in `Z`; interpret user input in `settings.MEETING_TIMEZONE`.
- Audit every agent/human action with `audit_memory.record_event(...)`.
- Worker jobs: idempotent, safe alongside the API (atomic conditional-UPDATE claims, `state_manager.is_event_processed`/`mark_event_processed`, bounded retries). Follow `worker/jobs/minutes_job.py`.
- Agents subclass `BaseAgent` (`runtime/agent_interface.py`), one responsibility, return `AgentOutput`.
- LLM calls only via `connectors/groq_client.py`; outputs must be grounded (follow `MoMAgent`).
- New settings go in `config.py` and `.env.example` with a comment.

## 6. Worklet status (vs `docs/worklet.md`)
| Lab | Status |
|---|---|
| 1 Agent loop (clarify/plan/revise) | Completed |
| 2 Tool-using agent | Completed: free/busy read via typed tools for organizer + connected participants; others "Availability not verified" |
| 3 Skills | Completed: plan (locked, versioned), format and minutes skills with Pydantic schemas |
| 4 Memory & retrieval | Partial: similarity only; no tag filter; minutes not indexed |
| 5 MCP connector | Partial -> mostly complete: external endpoint authenticated, caller-scoped; remaining defensible bypasses: DB-backed preferences, direct transcription_service call, unused worker jobs |
| 6 Runtime | Completed: budgets, per-step audit, execution checkpoints, crash recovery |
| 7 Node graph | Partial: two graphs; no validate→regenerate edge; no reminder node |
| 8 Parallel swarm | Partial: `execute_parallel_agents` only used in tests; live graph sequential |
| 9 Grounding | Partial: MoM evidence quotes; nothing for scheduling; no evidence table |
| 10 Self-healing | Partial: retries/escalation; no auto regenerate loop |
| 11 Acceptance suite | Partial: suite not green |
| 12 Governance | Partial: approval gates; no versioning, rollback or metrics; auth gaps |
| 13 Meeting-type specialists | Not implemented |
| 14 Capstone | Partial: dashboard works; no reminders panel or audit view |

Agents (4.4): Coordinator ✓; Scheduling & Availability Partial; Agenda Preparation ✓; Transcription & Minutes ✓ (online only); Action-Item Tracker ✓; **Follow-up Reminder ✗**; Resource & Room Booking ✓; Owner Review ✓; Distribution & Logging Partial (no version history).

Guardrails (4.12): never circulate minutes without owner approval; never invent discussion content; never mark an action item complete without owner/organizer confirmation; never confirm a slot or booking without approval; keep full version history of agenda and minutes drafts; never let an open action item expire without a reminder.

Remaining work, in order: (1) Follow-up Reminder agent; (2) ~~Lab 2 availability~~ (done); (3) Lab 4 tag-filtered history + approved minutes in RAG; (4) Lab 13 meeting-type templates inside the agenda flow; (5) version history + rollback; (6) update stale tests.

## 7. Known issues — do not make worse, fix only when asked
- Optional-auth bypass in `main.py` (`get_meeting`, `delete_meeting`, `handle_human_approval`, `send_venue_request`): unauthenticated callers skip ownership checks. No auth at all on `clarify`, `agenda`, `resolve/recheck/remove-participant`, `sync-responses`, `/api/audit-logs*`, `/api/employees*`.
- `/api/auth/register` sets a password on a Google-only account without proving email ownership.
- `/meetings/{id}/attendance` lets any logged-in user add any email to any meeting.
- OAuth callback does not check that the returned Google account matches the user/employee.
- `resolve_organizer_email` falls back to any connected account.
- A Google event with Meet link is created at draft stage (`node_create_draft_plan`), before approval.
- Bugs: edited `scheduled_end` is overwritten from duration on resume; `ResponseProcessingJob` marks events processed before validating them; OFFLINE meeting approved without Google → SCHEDULED → finalize → ACTION_FAILED; `agenda_skill.professionalize_sentence` can replace a point with an unrelated generic sentence.
- `database/backups/` is not in `.gitignore`; `config.py` has a default `DATABASE_URL` with a credential.

## 8. Secrets & data
- Never read, print, quote or commit `.env`, OAuth tokens, `database/backups/`, `data/recordings/`, `*.db`.
- Never log tokens or API keys. Use `.env.example` for setting names.
