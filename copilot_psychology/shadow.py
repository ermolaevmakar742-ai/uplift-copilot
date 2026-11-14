"""Shadow hook (observation only). Called from the Decision Ledger recorder like the other shadow hooks.
  * Off unless COPILOT_PSYCHOLOGY_SHADOW_ENABLED is true (default false): every call is then an immediate no-op.
  * HARD only: a decision whose production regime is not 'hard', or that is not an LLM reply, never reaches the engine.
  * Inputs come only from the decision's own pre-action snapshot (+ trigger / objection marker): no history, no Telegram, no OSINT.
  * Background task; scheduling never raises and never blocks; a circuit breaker pauses after repeated failures; production is untouched
    in every failure case. The result is stored append-only and is never returned to the caller."""
import asyncio
import logging
import os
import time
from typing import Any

from copilot_contracts.stages import Stage, map_production_stage
from copilot_psychology.contracts import Signals
from copilot_psychology.engine import assess, is_active
from copilot_psychology.repository import PsychologyConflict, PsychologyRepository

logger = logging.getLogger("copilot_psychology")
TIMEOUT_S = 10.0
MAX_PENDING = 50
_BREAKER_FAILS, _BREAKER_PAUSE_S = 3, 300
_tasks: set = set()
_state = {"fails": 0, "paused_until": 0.0}
_sem: asyncio.Semaphore | None = None


def shadow_enabled() -> bool:
    return os.getenv("COPILOT_PSYCHOLOGY_SHADOW_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def signals_from_decision(decision: Any) -> Signals:
    """Whitelisted, already-structured values only. Unknown stays None. Region and any personal characteristic are never read.
    blocker / trust / payment / momentum / contradiction live in Evidence + Shadow and appear after the decision: None here."""
    snap = getattr(decision, "client_state_snapshot", None)
    snap = snap if isinstance(snap, dict) else {}
    st = map_production_stage(snap.get("stage"))
    intent = snap.get("intent")
    mc = snap.get("messages_count")
    return Signals(stage=None if st is Stage.UNKNOWN else st.value, intent=intent if isinstance(intent, str) and intent else None,
                   sentiment=_num(snap.get("sentiment")), complexity=_num(snap.get("complexity")),
                   messages_count=mc if isinstance(mc, int) and not isinstance(mc, bool) else None,
                   trigger=getattr(decision, "trigger", None) if isinstance(getattr(decision, "trigger", None), str) else None,
                   objection_signal=getattr(decision, "objection_signal", None) if isinstance(getattr(decision, "objection_signal", None), str) else None)


def eligible(decision: Any) -> bool:
    return is_active(getattr(decision, "regime", None)) and getattr(decision, "decision_path", "llm_reply") == "llm_reply"


async def _persist(decision, session_factory) -> None:
    global _sem
    try:
        if _sem is None:
            _sem = asyncio.Semaphore(2)
        async with _sem:
            a = assess(signals_from_decision(decision), regime=decision.regime)
            if a is None:
                return
            inserted = await asyncio.wait_for(PsychologyRepository(session_factory).save(decision.decision_id, decision.chat_id, decision.created_at, a), TIMEOUT_S)
        _state["fails"] = 0
        logger.info("PSYCHOLOGY_SHADOW %s decision=%s", "saved" if inserted else "duplicate-skipped", decision.decision_id)
    except asyncio.CancelledError:
        raise
    except PsychologyConflict:
        logger.error("PSYCHOLOGY_SHADOW conflict decision=%s (kept existing)", getattr(decision, "decision_id", None))
    except Exception as e:
        _state["fails"] += 1
        if _state["fails"] >= _BREAKER_FAILS:
            _state["paused_until"] = time.monotonic() + _BREAKER_PAUSE_S
            logger.error("PSYCHOLOGY_SHADOW circuit open for %ss", _BREAKER_PAUSE_S)
        logger.error("PSYCHOLOGY_SHADOW failed decision=%s: %s", getattr(decision, "decision_id", None), type(e).__name__)


def schedule_assessment(decision, session_factory) -> bool:
    """Fire-and-forget. Never raises, never blocks, never touches the reply."""
    try:
        if not shadow_enabled() or not eligible(decision):
            return False
        if time.monotonic() < _state["paused_until"] or len(_tasks) >= MAX_PENDING:
            logger.warning("PSYCHOLOGY_SHADOW skipped (paused/backlog)")
            return False
        task = asyncio.get_running_loop().create_task(_persist(decision, session_factory))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return True
    except Exception:
        logger.error("PSYCHOLOGY_SHADOW schedule failed")
        return False
