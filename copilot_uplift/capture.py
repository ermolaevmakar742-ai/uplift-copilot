"""Shadow Uplift capture: for each recorded production decision, compute (read-only) what the policy WOULD recommend and store it
append-only. It NEVER changes a reply: it only observes, calculates, stores. Hooked (like Decision Evidence) from the ledger recorder.

  * Off unless COPILOT_UPLIFT_SHADOW_ENABLED is true (default false): then every call is an immediate no-op.
  * Background task only; synchronous scheduling never raises and never blocks; errors are logged minimally; a circuit breaker pauses
    after repeated failures. A missing evaluation / table, stale policy, unknown action ... only yields recommended_action None
    (with the reason), or a logged failure; production is unaffected in every case.
  * Features come only from the decision's own pre-action Client State snapshot (leakage-checked). No LLM, no network.
  * The clock is injectable; it stamps computed_at (the moment of the calculation), never an outcome time."""
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from copilot_uplift.action_mapping import MappingStatus, map_decision
from copilot_uplift.actions import DEFAULT_REGISTRY
from copilot_uplift.contract import ShadowRecord, make_shadow_id
from copilot_uplift.estimator import ESTIMATOR_VERSION
from copilot_uplift.features import FEATURE_VERSION, build_features
from copilot_uplift.policy import DEFAULT_POLICY, recommend
from copilot_uplift.gates import shadow_enabled

logger = logging.getLogger("copilot_uplift")

TIMEOUT_S = 10.0
MAX_PENDING = 50
_BREAKER_FAILS, _BREAKER_PAUSE_S = 3, 300
_tasks: set = set()
_state = {"fails": 0, "paused_until": 0.0}
_sem: asyncio.Semaphore | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_shadow_record(decision: Any, evaluation, *, now: datetime, policy=DEFAULT_POLICY, registry=DEFAULT_REGISTRY) -> ShadowRecord:
    vec = build_features(decision.decision_id, decision.chat_id, decision.created_at, getattr(decision, "client_state_snapshot", None))
    rec = recommend(vec, evaluation, policy, registry, now=now)
    mapped = map_decision(getattr(decision, "regime", None), getattr(decision, "trigger", None), version=policy.mapping_version, registry=registry,
                          decision_path=getattr(decision, "decision_path", None) or "llm_reply")
    action = mapped.action_id if (mapped.status is MappingStatus.MAPPED and mapped.sample_capable) else None
    return ShadowRecord(make_shadow_id(decision.decision_id, policy.policy_version, ESTIMATOR_VERSION, FEATURE_VERSION),
                        decision.decision_id, decision.chat_id, now,
                        {"fingerprint": vec.fingerprint(), **{f.feature_id: f.value for f in vec.features}}, action,
                        rec.recommended_action, rec.alternative_action, rec.estimated_uplift, rec.data_quality, policy.policy_version,
                        ESTIMATOR_VERSION, FEATURE_VERSION, getattr(evaluation, "evaluation_id", None), rec.reason)


async def _persist(decision, session_factory, clock) -> None:
    global _sem
    from copilot_uplift.repository import EvaluationRepository, ShadowRepository, UpliftConflict
    if _sem is None:
        _sem = asyncio.Semaphore(4)
    try:
        async with _sem:
            async def work():
                try:
                    ev = await EvaluationRepository(session_factory).latest()
                except Exception:
                    ev = None                                   # table missing / unreadable -> recommendation None, reason no_evaluation
                return await ShadowRepository(session_factory).save(build_shadow_record(decision, ev, now=(clock or _utcnow)()))
            inserted = await asyncio.wait_for(work(), TIMEOUT_S)
        _state["fails"] = 0
        logger.info("UPLIFT_SHADOW %s decision=%s", "saved" if inserted else "duplicate-skipped", decision.decision_id)
    except asyncio.CancelledError:
        raise
    except UpliftConflict:
        logger.error("UPLIFT_SHADOW conflict decision=%s (kept existing)", getattr(decision, "decision_id", None))
    except Exception as e:
        _state["fails"] += 1
        if _state["fails"] >= _BREAKER_FAILS:
            _state["paused_until"] = time.monotonic() + _BREAKER_PAUSE_S
            logger.error("UPLIFT_SHADOW circuit open for %ss", _BREAKER_PAUSE_S)
        logger.error("UPLIFT_SHADOW failed decision=%s: %s", getattr(decision, "decision_id", None), type(e).__name__)


def schedule_shadow(decision, session_factory, clock: Callable[[], datetime] | None = None) -> bool:
    """Fire-and-forget. Never raises, never blocks, never touches the reply."""
    try:
        if not shadow_enabled():
            return False
        if time.monotonic() < _state["paused_until"] or len(_tasks) >= MAX_PENDING:
            logger.warning("UPLIFT_SHADOW skipped (paused/backlog)")
            return False
        task = asyncio.get_running_loop().create_task(_persist(decision, session_factory, clock))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return True
    except Exception:
        logger.error("UPLIFT_SHADOW schedule failed")
        return False
