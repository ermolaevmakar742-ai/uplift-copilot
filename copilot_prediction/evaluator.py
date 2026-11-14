"""Pure evaluator: Decision + Response Boundary + Attribution v2 results (+ Shadow expectation) -> observation / comparison.
No DB, no clock (evaluated_at is an argument), no LLM, no network, no causal claims.

observe_decision(...) answers only: "what was observed after this decision's reply, according to attribution v2?"
  UNKNOWN  no valid SENT boundary / window still open / ambiguous or unresolved attribution / window unknown for a target type /
           no evidence the outcome pipeline was running at that time (outcome capture evidence = earliest known outcome)
  OBSERVED an outcome of a target type is ATTRIBUTED to the decision
  NO_OUTCOME window elapsed, nothing attributable, nothing unresolved: neutral, not a negative outcome
Leakage: only results about the decision's own post-boundary window are read; the prediction's own data is as-of the decision."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from copilot_attribution.contract import AttributionPolicy, PolicyKind, Relationship
from copilot_attribution.rules import DEFAULT_POLICY
from copilot_evidence.contracts import ContractError, _dt
from copilot_outcomes.contracts import OutcomeType
from copilot_prediction.contract import (EXPECTATION_VERSION, EXPECTATIONS, ObservationStatus as S, Prediction,
                                      PredictionComparison, PredictionKind, Relation, make_comparison_id,
                                      make_prediction_id)


@dataclass(frozen=True)
class Observation:
    status: S
    reason: str
    outcome_ids: tuple[str, ...] = ()
    outcome_types: tuple[str, ...] = ()


def observe_decision(decision_id: str, chat_id: int, boundary_end: datetime | None, results: Iterable,
                     *, evaluated_at: datetime, target_types: Iterable[str] | None = None,
                     policy: AttributionPolicy | None = None, capture_since: datetime | None = None,
                     outcome_types: Mapping[str, str] | None = None) -> Observation:
    policy = DEFAULT_POLICY if policy is None else policy
    evaluated_at = _dt("evaluated_at", evaluated_at)
    targets = sorted(target_types) if target_types is not None else sorted(t.value for t in policy.outcome_policies)
    results = list(results)
    mine = [r for r in results if r.relationship is Relationship.ATTRIBUTED and r.decision_id == decision_id
            and r.metadata.get("outcome_type") in targets]
    if mine:
        ids = tuple(sorted(r.outcome_id for r in mine))
        return Observation(S.OBSERVED, "attributed_outcome_observed", ids, tuple(sorted({r.metadata["outcome_type"] for r in mine})))
    if boundary_end is None:
        return Observation(S.UNKNOWN, "no_sent_response_boundary")
    for r in results:
        if r.relationship is Relationship.AMBIGUOUS and decision_id in (r.metadata.get("candidate_decision_ids") or ()) \
                and r.metadata.get("outcome_type") in targets:
            return Observation(S.UNKNOWN, "ambiguous_attribution")
        if r.relationship is Relationship.UNKNOWN and decision_id in (r.metadata.get("unverifiable_decision_ids") or ()) \
                and r.metadata.get("outcome_type") in targets:
            return Observation(S.UNKNOWN, "unresolved_attribution")
    windows = []
    for t in targets:
        pol = policy.outcome_policies.get(OutcomeType(t))
        if pol is None or pol.kind is not PolicyKind.WINDOW:
            return Observation(S.UNKNOWN, "window_unknown_for_target")
        windows.append(pol.window_seconds)
    if not windows:
        return Observation(S.UNKNOWN, "window_unknown_for_target")
    if capture_since is None:
        return Observation(S.UNKNOWN, "no_outcome_capture_evidence")
    if boundary_end < capture_since:
        return Observation(S.UNKNOWN, "outcome_capture_started_later")
    if evaluated_at <= boundary_end + timedelta(seconds=max(windows)):
        return Observation(S.UNKNOWN, "window_open")
    return Observation(S.NO_OUTCOME, "window_elapsed_no_attributed_outcome")


def predictions_from_shadow(rows: Iterable[Mapping]) -> tuple[Prediction, ...]:
    """rows: shadow records with decision_id, chat_id, created_at (recorded), decision_created_at (as-of) and momentum (dict).
    A row without a valid stated direction produces NO prediction (never a fabricated one)."""
    out = []
    for r in rows:
        m = r.get("momentum")
        if not isinstance(m, Mapping) or "error" in m or not m.get("user_turns_seen"):
            continue
        direction = m.get("direction")
        if direction not in EXPECTATIONS or not r.get("decision_id") or not isinstance(r.get("chat_id"), int):
            continue
        try:
            out.append(Prediction(make_prediction_id(r["decision_id"], PredictionKind.MOMENTUM_DIRECTION.value, direction, r["decision_id"]),
                                  r["decision_id"], r["chat_id"], PredictionKind.MOMENTUM_DIRECTION, direction,
                                  tuple(EXPECTATIONS[direction]), EXPECTATION_VERSION, r["decision_id"],
                                  r["decision_created_at"], r["created_at"]))
        except (ContractError, KeyError):
            continue
    return tuple(sorted(out, key=lambda p: p.prediction_id))


def compare(prediction: Prediction, observation: Observation, *, evaluated_at: datetime,
            first_outcome_at: datetime | None = None, extra: Mapping | None = None) -> PredictionComparison:
    """first_outcome_at: observed_at of the earliest attributed outcome (needed to enforce 'prediction before outcome')."""
    status, reason, relation = observation.status, observation.reason, Relation.NOT_COMPARABLE
    if status is S.OBSERVED:
        if first_outcome_at is not None and prediction.recorded_at > first_outcome_at:
            status, reason = S.UNKNOWN, "prediction_recorded_after_outcome"     # cannot claim it was predicted
        elif not prediction.expected_outcome_types:
            reason = "no_expectation_stated"
        else:
            relation = Relation.CONSISTENT if set(observation.outcome_types) & set(prediction.expected_outcome_types) else Relation.DIFFERENT
    ids = observation.outcome_ids if status is S.OBSERVED else ()
    types = observation.outcome_types if status is S.OBSERVED else ()
    at = _dt("evaluated_at", evaluated_at)
    cid = make_comparison_id(prediction.prediction_id, status.value, ids, at.isoformat())
    meta = {"predicted_value": prediction.predicted_value, "expected_outcome_types": list(prediction.expected_outcome_types),
            "expectation_version": prediction.expectation_version, "as_of": prediction.as_of.isoformat(),
            "recorded_at": prediction.recorded_at.isoformat(), **dict(extra or {})}
    return PredictionComparison(cid, prediction.prediction_id, prediction.decision_id, prediction.chat_id, status, relation, ids,
                                types, at, reason, meta)
