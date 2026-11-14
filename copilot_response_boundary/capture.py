"""Production-side capture of response boundaries (phase 5F). Called from the send path right AFTER the Telegram API
call returned (SENT) or raised (FAILED); never before the send, never from decision creation.

  * Off unless COPILOT_RESPONSE_BOUNDARY_ENABLED is true (default false): every call is then an immediate no-op.
  * sent_at = the instant read here, synchronously after the awaited send returned (same bot clock as
    Decision.created_at, so the two are comparable). The Telegram message's own `date` (whole seconds) is stored in
    metadata.telegram_date for cross-checks; its outgoing semantics are not confirmed by existing code, so it is NOT sent_at.
    metadata.timestamp_source records this ("send_path_clock"). The clock is injectable; it is the ONLY clock read in the package.
  * telegram_message_id comes from the object the send call returned; without a valid int id nothing is recorded (no invention).
  * decision_id is whatever the send path was given (None = unlinked); nothing is looked up or guessed.
  * Never raises into production, never blocks: persistence runs in a background task; errors are logged minimally;
    a circuit breaker pauses after repeated failures. No LLM, no network, no DDL, no attribution.
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

from copilot_response_boundary.recorder import build_failed, build_sent

logger = logging.getLogger("copilot_response_boundary")

FLAG = "COPILOT_RESPONSE_BOUNDARY_ENABLED"
TIMESTAMP_SOURCE = "send_path_clock"
TIMEOUT_S = 10.0
MAX_PENDING = 100
_BREAKER_FAILS, _BREAKER_PAUSE_S = 3, 300

_tasks: set = set()
_state = {"fails": 0, "paused_until": 0.0}
_sem: asyncio.Semaphore | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def capture_enabled() -> bool:
    return os.getenv(FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


async def _persist(boundary, session_factory) -> None:
    global _sem
    from copilot_response_boundary.repository import BoundaryConflict, ResponseBoundaryRepository   # lazy: DB-free import
    if _sem is None:
        _sem = asyncio.Semaphore(4)
    try:
        async with _sem:
            inserted = await asyncio.wait_for(ResponseBoundaryRepository(session_factory).save(boundary), TIMEOUT_S)
        _state["fails"] = 0
        logger.info("BOUNDARY %s %s id=%s", boundary.status.value, "saved" if inserted else "duplicate-skipped",
                    boundary.response_boundary_id)
    except asyncio.CancelledError:
        raise
    except BoundaryConflict:
        logger.error("BOUNDARY conflict id=%s (kept existing)", boundary.response_boundary_id)
    except Exception as e:
        _state["fails"] += 1
        if _state["fails"] >= _BREAKER_FAILS:
            _state["paused_until"] = time.monotonic() + _BREAKER_PAUSE_S
            logger.error("BOUNDARY circuit open for %ss", _BREAKER_PAUSE_S)
        logger.error("BOUNDARY failed id=%s: %s", getattr(boundary, "response_boundary_id", None), type(e).__name__)


def _schedule(boundary, session_factory) -> bool:
    if time.monotonic() < _state["paused_until"] or len(_tasks) >= MAX_PENDING:
        logger.warning("BOUNDARY skipped (paused/backlog)")
        return False
    task = asyncio.get_running_loop().create_task(_persist(boundary, session_factory))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


def capture_sent(*, sent_message: Any, chat_id: int, session_factory, decision_id: str | None = None,
                 part_index: int | None = None, parts_total: int | None = None,
                 clock: Callable[[], datetime] | None = None) -> bool:
    """Record a SUCCESSFUL send. `sent_message` is what the Telegram call returned. Never raises, never blocks."""
    try:
        if not capture_enabled():
            return False
        sent_at = (clock or _utcnow)()                      # read immediately: the send call has just returned
        mid = getattr(sent_message, "id", None)
        tdate = getattr(sent_message, "date", None)
        if not isinstance(tdate, datetime) or tdate.tzinfo is None:
            tdate = None
        b = build_sent(chat_id=chat_id, telegram_message_id=mid, sent_at=sent_at, decision_id=decision_id,
                       part_index=part_index, parts_total=parts_total, timestamp_source=TIMESTAMP_SOURCE,
                       telegram_date=tdate)
        return _schedule(b, session_factory)
    except Exception:
        logger.error("BOUNDARY capture_sent failed")
        return False


def capture_failed(*, chat_id: int, session_factory, decision_id: str | None = None, part_index: int | None = None,
                   parts_total: int | None = None, clock: Callable[[], datetime] | None = None) -> bool:
    """Record a FAILED send (never a boundary). The timestamp is when the failure was observed in the send path."""
    try:
        if not capture_enabled():
            return False
        b = build_failed(chat_id=chat_id, failed_at=(clock or _utcnow)(), decision_id=decision_id, part_index=part_index,
                         parts_total=parts_total, timestamp_source=TIMESTAMP_SOURCE)
        return _schedule(b, session_factory)
    except Exception:
        logger.error("BOUNDARY capture_failed failed")
        return False
