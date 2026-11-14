"""Production adapter (interface) for FUTURE use: not called by main.py, not wired to any reply.

select_action(...) answers one question: "may Uplift name an action for this decision, and which REGISTERED action_id?"
It returns None (=> production keeps its existing behaviour) unless ALL hold, checked fail-closed in this order:
  kill switch COPILOT_UPLIFT_ENABLED true -> activation state ACTIVE (valid event chain) -> locked policy -> an evaluation made with
  that policy, not stale, with zero integrity errors and data quality >= the production bar -> a positive ESTIMATED recommendation
  marked eligible_for_production -> the action is in the registry.
Any exception, malformed/unknown/stale input, disabled flag or missing storage returns None. It gives Uplift NO access to Telegram:
the existing production layer decides whether and how to carry out the returned action_id."""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from copilot_uplift.activation import StoredState, influence_allowed, replay
from copilot_uplift.actions import DEFAULT_REGISTRY, ActionRegistry
from copilot_uplift.contract import ActivationState as S
from copilot_uplift.features import build_features
from copilot_uplift.gates import kill_switch_on
from copilot_uplift.policy import DEFAULT_POLICY, UpliftPolicy, recommend
from copilot_uplift.repository import ActivationRepository, EvaluationRepository

logger = logging.getLogger("copilot_uplift")


@dataclass(frozen=True)
class ActionSelection:
    action_id: str
    policy_version: str
    evaluation_id: str
    estimated_uplift: float


async def select_action(session_factory, context: Mapping, *, clock: Callable[[], datetime] | None = None,
                        env: Mapping | None = None, policy: UpliftPolicy = DEFAULT_POLICY,
                        registry: ActionRegistry = DEFAULT_REGISTRY) -> ActionSelection | None:
    """context: decision_id, chat_id, created_at (decision time), state (pre-action client state snapshot)."""
    try:
        if not kill_switch_on(env):
            return None                                           # physically closed: no DB access at all
        now = (clock or (lambda: datetime.now(timezone.utc)))()
        try:
            events = await ActivationRepository(session_factory).list_events()
            stored = replay(events)
        except Exception:
            stored = StoredState(S.BLOCKED, None, True, "activation_state_unreadable")
        evaluation = await EvaluationRepository(session_factory).latest()
        if not influence_allowed(stored, kill_switch=True, policy=policy, evaluation=evaluation, now=now).allowed:
            return None
        vec = build_features(context["decision_id"], context["chat_id"], context["created_at"], context.get("state"))
        rec = recommend(vec, evaluation, policy, registry, now=now)
        if not rec.eligible_for_production or rec.recommended_action is None or rec.recommended_action not in registry:
            return None
        return ActionSelection(rec.recommended_action, policy.policy_version, evaluation.evaluation_id, rec.estimated_uplift)
    except Exception:
        logger.error("uplift adapter failed; falling back to existing production behaviour")
        return None
