"""Decision Evidence capture (phase 3D): the ONLY bridge from the production decision flow into the
evidence layer. Called by copilot_decisions.recorder right after it built the ProductionDecision.

Rules
  * Off unless COPILOT_DECISION_EVIDENCE_ENABLED is true (default false). Independent of the ledger flag,
    but a snapshot only exists for a decision the ledger actually recorded (the hook lives there).
  * Only PRE-decision data: the ProductionDecision itself (ACTION) and the Client State snapshot embedded
    in it. Nothing is read from the DB, Client State runtime, OSINT or Shadow. OSINT 2.0 is not wired
    into production and has no trustworthy pre-decision timestamp, so it contributes nothing here.
  * Temporal gate (filter_pre_decision): evidence created_at/observed_at must be <= decision time
    (decision.created_at, equality allowed); Shadow is excluded unconditionally (post-response analytics);
    no timestamp is ever fabricated.
  * Non-blocking and fail-safe: schedule_capture() is synchronous, cheap, never raises; the snapshot is built
    and persisted (repository.save, the only persistence API) in a background task. Errors are logged only.
  * No network, no LLM, no new decision logic.
"""
import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from copilot_evidence.adapters import adapt_client_state, adapt_production_decision
from copilot_evidence.contracts import Evidence, EvidenceSource
from copilot_evidence.decision import DecisionEvidence, build_decision_evidence
from copilot_evidence.fusion import fuse

logger = logging.getLogger("copilot_evidence")

FLAG = "COPILOT_DECISION_EVIDENCE_ENABLED"
EXCLUDED_SOURCES = frozenset({EvidenceSource.SHADOW})
TIMEOUT_S = 10.0
MAX_PENDING = 50
_BREAKER_FAILS, _BREAKER_PAUSE_S = 3, 300

_tasks: set = set()
_state = {"fails": 0, "paused_until": 0.0}
_sem: asyncio.Semaphore | None = None


def capture_enabled() -> bool:
    return os.getenv(FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Exclusion:
    evidence_id: str | None
    reason: str


def filter_pre_decision(items: Iterable, decision_time: Any) -> tuple[tuple[Evidence, ...], tuple[Exclusion, ...]]:
    """Keep only Evidence provably available at decision_time. Never fabricates a timestamp."""
    kept, out = [], []
    reliable = isinstance(decision_time, datetime) and decision_time.tzinfo is not None
    for e in items:
        if not isinstance(e, Evidence):
            out.append(Exclusion(None, "not_an_evidence"))
        elif e.source in EXCLUDED_SOURCES:
            out.append(Exclusion(e.evidence_id, "shadow_is_post_decision_analytics"))
        elif not reliable:
            out.append(Exclusion(e.evidence_id, "decision_time_unreliable"))
        elif e.created_at > decision_time:
            out.append(Exclusion(e.evidence_id, "created_after_decision"))
        elif e.observed_at is not None and e.observed_at > decision_time:
            out.append(Exclusion(e.evidence_id, "observed_after_decision"))
        else:
            kept.append(e)
    return tuple(kept), tuple(out)


def collect_pre_decision_evidence(decision, extra_evidence: Iterable = ()) -> tuple[tuple[Evidence, ...], tuple[Exclusion, ...]]:
    """ACTION + embedded Client State snapshot (+ any extra evidence, which must pass the same gate)."""
    d = decision.to_dict()
    items = list(adapt_production_decision(d).evidence)
    snap = d.get("client_state_snapshot")
    if isinstance(snap, dict):
        items += adapt_client_state(snap, chat_id=d["chat_id"], created_at=decision.created_at,
                                    message_id=d["message_id"], source_id=d["decision_id"]).evidence
    items += list(extra_evidence)
    return filter_pre_decision(items, decision.created_at)


def build_snapshot(decision, extra_evidence: Iterable = (), now: datetime | None = None) -> DecisionEvidence:
    kept, _ = collect_pre_decision_evidence(decision, extra_evidence)
    return build_decision_evidence(decision.decision_id, fuse(kept), chat_id=decision.chat_id,
                                   message_id=decision.message_id, created_at=now or datetime.now(timezone.utc))


async def _persist(decision, session_factory) -> None:
    global _sem
    from copilot_evidence.repository import DecisionEvidenceRepository   # lazy: contracts stay DB-free
    if _sem is None:
        _sem = asyncio.Semaphore(4)
    try:
        async with _sem:
            async def work():
                return await DecisionEvidenceRepository(session_factory).save(build_snapshot(decision))
            inserted = await asyncio.wait_for(work(), TIMEOUT_S)
        _state["fails"] = 0
        logger.info("DECISION_EVIDENCE %s decision=%s", "saved" if inserted else "duplicate-skipped",
                    decision.decision_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _state["fails"] += 1
        if _state["fails"] >= _BREAKER_FAILS:
            _state["paused_until"] = time.monotonic() + _BREAKER_PAUSE_S
            logger.error("DECISION_EVIDENCE circuit open for %ss", _BREAKER_PAUSE_S)
        logger.error("DECISION_EVIDENCE failed decision=%s: %s", getattr(decision, "decision_id", None),
                     type(e).__name__)


def schedule_capture(decision, session_factory) -> bool:
    """Fire-and-forget. True if a task was scheduled. Never raises, never blocks."""
    try:
        if not capture_enabled():
            return False
        if time.monotonic() < _state["paused_until"] or len(_tasks) >= MAX_PENDING:
            logger.warning("DECISION_EVIDENCE skipped (paused/backlog)")
            return False
        task = asyncio.get_running_loop().create_task(_persist(decision, session_factory))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return True
    except Exception:
        logger.error("DECISION_EVIDENCE schedule failed")
        return False
