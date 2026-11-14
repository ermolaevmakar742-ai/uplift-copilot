"""Activation controller: pure functions over an APPEND-ONLY list of ActivationEvents. Nothing here reads the environment, a
clock or a DB; the caller (service.py) loads events and persists the event returned.

Stored states: OFF (initial), ACTIVE, BLOCKED. READY is derived (never stored): not ACTIVE, every gate passes.
  OFF      never activated / disabled; gates not (all) passing -> "Activation locked"
  READY    all gates pass; waiting for an admin
  ACTIVE   an admin enabled it AND the gates still pass; otherwise it is shown (and treated) as BLOCKED
  BLOCKED  an enable attempt was rejected, an integrity failure, or a corrupt event chain
Enabling requires: the exact confirmation text, all gates passing, no integrity failure. It is never automatic.
A corrupt/inconsistent event chain is BLOCKED and cannot be enabled (fail-safe). The kill switch can only remove influence."""
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from copilot_uplift.contract import ActivationEvent, ActivationState as S, DataQuality, UpliftEvaluation, new_event
from copilot_uplift.gates import GateReport
from copilot_uplift.policy import UpliftPolicy, is_locked

CONFIRMATION = "ENABLE UPLIFT"


class ActivationError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StoredState:
    state: S
    last_event: ActivationEvent | None
    corrupt: bool = False
    reason: str = ""


def replay(events: Iterable[ActivationEvent]) -> StoredState:
    evs = sorted(events, key=lambda e: (e.changed_at, e.event_id))
    if not evs:
        return StoredState(S.OFF, None)
    prev = S.OFF
    for e in evs:
        if not isinstance(e, ActivationEvent) or e.previous_state is not prev:
            return StoredState(S.BLOCKED, evs[-1] if isinstance(evs[-1], ActivationEvent) else None, True, "activation_event_chain_broken")
        prev = e.new_state
    return StoredState(prev, evs[-1])


def effective_status(stored: StoredState, report: GateReport | None) -> S:
    if stored.corrupt:
        return S.BLOCKED
    ok = report is not None and report.all_passed and not report.integrity_failed
    if stored.state is S.ACTIVE:
        return S.ACTIVE if ok else S.BLOCKED
    if ok:
        return S.READY
    return S.BLOCKED if (stored.state is S.BLOCKED and report is not None and report.integrity_failed) else S.OFF


def request_enable(events: Iterable[ActivationEvent], *, changed_by: str, confirmation: str, report: GateReport,
                   policy: UpliftPolicy, now: datetime) -> tuple[ActivationEvent, S]:
    stored = replay(events)
    if stored.corrupt:
        raise ActivationError("activation_state_corrupt")
    if stored.state is S.ACTIVE:
        raise ActivationError("already_active")
    if confirmation != CONFIRMATION:
        raise ActivationError("confirmation_required")
    gates = report.to_dict()
    if not report.all_passed or report.integrity_failed:
        return new_event(stored.state, S.BLOCKED, changed_by=changed_by, changed_at=now, policy_version=policy.policy_version,
                         reason="enable rejected, missing gates: " + ", ".join(report.missing or ("integrity",)), gates=gates), S.BLOCKED
    return new_event(stored.state, S.ACTIVE, changed_by=changed_by, changed_at=now, policy_version=policy.policy_version,
                     reason=f"enabled by admin; {policy.min_unique_leads}+ unique leads confirmed; all safety gates passed",
                     gates=gates), S.ACTIVE


def request_disable(events: Iterable[ActivationEvent], *, changed_by: str, reason: str, policy: UpliftPolicy, now: datetime,
                    integrity_failure: bool = False) -> ActivationEvent:
    stored = replay(events)
    if stored.corrupt:
        raise ActivationError("activation_state_corrupt")
    if stored.state is not S.ACTIVE:
        raise ActivationError("not_active")
    return new_event(S.ACTIVE, S.BLOCKED if integrity_failure else S.OFF, changed_by=changed_by, changed_at=now,
                     policy_version=policy.policy_version, reason=reason or "disabled by admin", gates={})


@dataclass(frozen=True)
class Influence:
    allowed: bool
    reason: str


def influence_allowed(stored: StoredState, *, kill_switch: bool, policy: UpliftPolicy, evaluation: UpliftEvaluation | None,
                      now: datetime) -> Influence:
    """Single place that decides whether Uplift may influence a production decision. Fail-closed, ordered reasons."""
    try:
        if not kill_switch:
            return Influence(False, "kill_switch_off")
        if stored.corrupt:
            return Influence(False, "activation_state_corrupt")
        if stored.state is not S.ACTIVE:
            return Influence(False, "not_activated")
        if not is_locked(policy):
            return Influence(False, "policy_not_locked")
        if not isinstance(evaluation, UpliftEvaluation):
            return Influence(False, "no_evaluation")
        if evaluation.policy_version != policy.policy_version:
            return Influence(False, "stale_policy")
        if (now - evaluation.evaluation_timestamp).total_seconds() > policy.max_evaluation_age_seconds:
            return Influence(False, "stale_evaluation")
        if any(v for v in evaluation.integrity.values()):
            return Influence(False, "integrity_failure")
        if evaluation.truncated or any(
            v for k, v in evaluation.missing_signals.items()
            if k.startswith("provider_unreadable_rows:")
            or k.startswith("provider_required_table_unavailable:")
            or k in {"ambiguous_experiment_assignments"}
        ):
            return Influence(False, "provider_data_incomplete")
        if evaluation.data_quality.rank < DataQuality(policy.min_quality_production).rank:
            return Influence(False, "data_quality_below_production_bar")
        return Influence(True, "active")
    except Exception:
        return Influence(False, "error")
