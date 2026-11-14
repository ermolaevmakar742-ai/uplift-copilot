"""Outcome capture (phase 4D): persist an ALREADY FORMED Outcome through the existing repository, nothing else.

  * Off unless COPILOT_OUTCOME_CAPTURE_ENABLED is true (default false): then every call is a no-op.
  * capture_outcome(outcome, session_factory): INSERT-only OutcomeRepository.save() in a background task.
    The Outcome is passed through untouched (observed_at, decision_id, confidence stay exactly as given).
  * capture_event(source, data, session_factory=...): thin convenience for production hook points: the existing
    adapters (postback / telegram / production) build the Outcome from the event, then capture_outcome stores it.
  * No join, no attribution, no decision lookup, no decision_id computation (None stays None), no clock,
    no LLM, no network, no DDL. Never raises into production; DB/repository/conflict errors are only logged
    (minimal text). A small circuit breaker pauses capture after repeated failures.
"""
import asyncio
import logging
import os
import time

from copilot_outcomes.adapters import adapt
from copilot_outcomes.contracts import Outcome

logger = logging.getLogger("copilot_outcomes")

FLAG = "COPILOT_OUTCOME_CAPTURE_ENABLED"
TIMEOUT_S = 10.0
MAX_PENDING = 100
_BREAKER_FAILS, _BREAKER_PAUSE_S = 3, 300

_tasks: set = set()
_state = {"fails": 0, "paused_until": 0.0}
_sem: asyncio.Semaphore | None = None


def capture_enabled() -> bool:
    return os.getenv(FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


async def _persist(outcome: Outcome, session_factory) -> None:
    global _sem
    from copilot_outcomes.repository import OutcomeConflict, OutcomeRepository   # lazy: contract layer stays DB-free
    if _sem is None:
        _sem = asyncio.Semaphore(4)
    try:
        async with _sem:
            inserted = await asyncio.wait_for(OutcomeRepository(session_factory).save(outcome), TIMEOUT_S)
        _state["fails"] = 0
        logger.info("OUTCOME %s id=%s", "saved" if inserted else "duplicate-skipped", outcome.outcome_id)
    except asyncio.CancelledError:
        raise
    except OutcomeConflict:
        logger.error("OUTCOME conflict id=%s (different content already stored; kept existing)", outcome.outcome_id)
    except Exception as e:
        _state["fails"] += 1
        if _state["fails"] >= _BREAKER_FAILS:
            _state["paused_until"] = time.monotonic() + _BREAKER_PAUSE_S
            logger.error("OUTCOME circuit open for %ss", _BREAKER_PAUSE_S)
        logger.error("OUTCOME failed id=%s: %s", getattr(outcome, "outcome_id", None), type(e).__name__)


def capture_outcome(outcome, session_factory) -> bool:
    """Fire-and-forget. True if a save task was scheduled. Never raises, never blocks."""
    try:
        if not capture_enabled():
            return False
        if not isinstance(outcome, Outcome):
            logger.error("OUTCOME capture rejected: not an Outcome")
            return False
        if time.monotonic() < _state["paused_until"] or len(_tasks) >= MAX_PENDING:
            logger.warning("OUTCOME capture skipped (paused/backlog)")
            return False
        task = asyncio.get_running_loop().create_task(_persist(outcome, session_factory))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return True
    except Exception:
        logger.error("OUTCOME schedule failed")
        return False


def capture_event(source, data, *, session_factory) -> bool:
    """Existing event -> Outcome (adapters) -> capture_outcome. True if something was scheduled. Never raises."""
    try:
        if not capture_enabled():
            return False
        scheduled = False
        for o in adapt(source, data).outcomes:
            scheduled = capture_outcome(o, session_factory) or scheduled
        return scheduled
    except Exception:
        logger.error("OUTCOME capture_event failed")
        return False
