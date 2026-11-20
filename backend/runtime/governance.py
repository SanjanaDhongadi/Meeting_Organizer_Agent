"""
Runtime governance (Lab 6): budgets for one governed run (workflow steps, LLM calls, wall-clock seconds).

A run is opened with `governed_run(kind, meeting_id)` (scheduling workflow, minutes generation). Inside it,
`count_step()`, `count_llm_call()` and `check_time()` enforce the configured budgets; outside a governed run they
do nothing, so ad-hoc calls (tests, scripts) are unaffected. When the run ends exactly one `Agent Runtime` audit
event is recorded (RUN_COMPLETED or RUN_FAILED) with the usage and the budget that was exceeded, if any.

Import note: groq_client imports this module lazily (inside chat_json), so importing LLMError here is cycle-free.
"""
import contextlib
import contextvars
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional

from app.config import settings
from connectors.groq_client import LLMError

logger = logging.getLogger("governance")


class BudgetExceeded(LLMError):
    """A run budget was used up. An LLMError, so every existing LLM failure handler already copes with it."""

    def __init__(self, budget: str, message: str):
        super().__init__("BUDGET_EXCEEDED", message, retryable=False)
        self.budget = budget


@dataclass(frozen=True)
class RunBudget:
    max_steps: int = 50
    max_llm_calls: int = 20
    max_seconds: float = 300.0

    @classmethod
    def from_settings(cls) -> "RunBudget":
        return cls(settings.RUNTIME_MAX_STEPS, settings.RUNTIME_MAX_LLM_CALLS, settings.RUNTIME_MAX_SECONDS)


class _Run:
    def __init__(self, kind: str, meeting_id: Optional[str], budget: RunBudget):
        self.kind, self.meeting_id, self.budget = kind, meeting_id, budget
        self.steps = 0
        self.llm_calls = 0
        self.exceeded: Optional[str] = None
        self._started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def exceed(self, budget: str, message: str) -> BudgetExceeded:
        self.exceeded = self.exceeded or budget
        return BudgetExceeded(budget, message)

    def check_time(self) -> None:
        if self.elapsed >= self.budget.max_seconds:
            raise self.exceed("seconds", f"The run used its time budget ({self.budget.max_seconds:g}s).")


_current: contextvars.ContextVar[Optional[_Run]] = contextvars.ContextVar("governed_run", default=None)


def current_run() -> Optional[_Run]:
    return _current.get()


def count_step() -> None:
    """One workflow node is about to run. No-op outside a governed run."""
    run = _current.get()
    if not run:
        return
    run.check_time()
    run.steps += 1
    if run.steps > run.budget.max_steps:
        raise run.exceed("steps", f"The run used its step budget ({run.budget.max_steps} steps).")


def count_llm_call() -> None:
    """An LLM request is about to be sent. No-op outside a governed run."""
    run = _current.get()
    if not run:
        return
    run.check_time()
    if run.llm_calls >= run.budget.max_llm_calls:
        raise run.exceed("llm_calls", f"The run used its LLM-call budget ({run.budget.max_llm_calls} calls).")
    run.llm_calls += 1


def check_time() -> None:
    run = _current.get()
    if run:
        run.check_time()


def _record(run: _Run, error: Optional[BaseException]) -> None:
    """Exactly one audit event per run. A failure to write it must never hide the run's own exception."""
    try:
        from memory.audit_memory import audit_memory
        if error is not None and not run.exceeded and type(error).__name__ == "GraphRecursionError":
            run.exceeded = "steps"
        details: Dict[str, Any] = {
            "kind": run.kind, "steps": run.steps, "llm_calls": run.llm_calls, "elapsed_seconds": round(run.elapsed, 3),
            "budgets": {"max_steps": run.budget.max_steps, "max_llm_calls": run.budget.max_llm_calls,
                        "max_seconds": run.budget.max_seconds},
            "exceeded": run.exceeded,
        }
        audit_memory.record_event(
            "Agent Runtime", "RUN_FAILED" if error is not None else "RUN_COMPLETED", meeting_id=run.meeting_id,
            status="ERROR" if error is not None else "SUCCESS",
            error=f"{type(error).__name__}: {error}"[:300] if error is not None else "", details=details)
    except Exception:
        logger.exception("Could not record the run in the audit log")


@contextlib.contextmanager
def governed_run(kind: str, meeting_id: Optional[str] = None, budget: Optional[RunBudget] = None) -> Iterator[_Run]:
    """Budgeted run scope. Re-entering inside an active run joins it (one budget, one audit event)."""
    outer = _current.get()
    if outer is not None:
        yield outer
        return
    run = _Run(kind, meeting_id, budget or RunBudget.from_settings())
    token = _current.set(run)
    error: Optional[BaseException] = None
    try:
        yield run
    except BaseException as caught:
        error = caught
        raise
    finally:
        _current.reset(token)
        _record(run, error)
