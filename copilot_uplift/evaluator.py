"""Offline Uplift evaluator: pure analytics over data handed in by a read-only provider (provider.py). No SQL here, no clock
(evaluated_at is an argument), no LLM, no network, no randomness, no ranking / leaderboard / "best action".

evaluate_uplift(data, policy, evaluated_at=...) runs: Attribution v2 (boundary based) -> per-decision observation
(OBSERVED / NO_OUTCOME / UNKNOWN) -> leak-free features -> estimator, and reports unique leads, decisions, response boundaries,
outcomes, attributed outcomes, actions, eligible / control / treatment samples, conversion by action, uplift estimates, data
quality, coverage, missing signals and integrity counters. INSUFFICIENT_DATA is a legitimate result."""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from copilot_attribution.contract import AttributionPolicy, Method, PolicyKind, Relationship
from copilot_attribution.contract import DecisionRef, Interruption
from copilot_attribution.rules import DEFAULT_POLICY as ATTRIBUTION_POLICY
from copilot_attribution.rules_v2 import BoundaryRef, attribute_all_v2, boundary_info
from copilot_evidence.contracts import ContractError, _dt
from copilot_outcomes.contracts import Outcome, OutcomeType
from copilot_prediction.contract import ObservationStatus
from copilot_prediction.evaluator import Observation, observe_decision
from copilot_uplift.action_mapping import MappingStatus, map_decision, rules_fingerprint
from copilot_uplift.actions import BASELINE_ACTION_ID, DEFAULT_REGISTRY, ActionRegistry
from copilot_uplift.contract import UpliftEvaluation, evaluation_id_for
from copilot_uplift.estimator import ESTIMATOR_VERSION, Sample, estimate
from copilot_uplift.features import FEATURE_VERSION, audit_vectors, build_features
from copilot_uplift.policy import UpliftPolicy


@dataclass(frozen=True)
class UpliftData:
    """What a provider loaded. `decisions` are mappings: decision_id, chat_id, created_at, regime, state (client state snapshot).
    The *_total / unique_leads fields are exact aggregates from the source (the lists may be bounded: see `truncated`)."""
    decisions: tuple = ()
    boundaries: tuple = ()
    outcomes: tuple = ()
    interruptions: tuple = ()
    unique_leads: int | None = None
    decisions_total: int | None = None
    boundaries_sent_total: int | None = None
    outcomes_total: int | None = None
    truncated: bool = False
    unreadable: Mapping[str, int] = field(default_factory=dict)
    notes: tuple = ()
    tables: Mapping[str, bool] = field(default_factory=dict)
    assignments: tuple = ()               # (assignment_id, chat_id, arm "CONTROL"|"TREATMENT", assignment_probability, decided_at)
    assignments_total: int | None = None


def _h(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def _ratio(n: int, d: int):
    return None if d == 0 else round(n / d, 6)


def _baseline_window(policy: UpliftPolicy, attribution_policy: AttributionPolicy) -> timedelta | None:
    """The holdout window: from the decision time (when the follow-up WOULD have been sent) for every target outcome with a
    WINDOW policy. None when any target has no window (then a holdout sample stays UNKNOWN, like the treated path)."""
    secs = []
    for t in policy.target_outcomes:
        pol = attribution_policy.outcome_policies.get(OutcomeType(t))
        if pol is None or pol.kind is not PolicyKind.WINDOW or pol.window_seconds is None:
            return None
        secs.append(pol.window_seconds)
    return timedelta(seconds=max(secs)) if secs else None


def _experiment_observation(chat_id: int, assigned_at: datetime, outcomes: tuple, targets: tuple, window: timedelta,
                             capture_since: datetime | None, evaluated_at: datetime) -> Observation:
    """Intent-to-treat outcome for one randomized chat. Both arms use the same window from assignment time,
    and outcomes are counted regardless of message attribution or treatment compliance."""
    end = assigned_at + window
    inwin = [o for o in outcomes if o.chat_id == chat_id and assigned_at < o.observed_at <= end
             and o.outcome_type.value in targets]
    if inwin:
        return Observation(ObservationStatus.OBSERVED, "experiment_window_outcome",
                           tuple(sorted(o.outcome_id for o in inwin)),
                           tuple(sorted({o.outcome_type.value for o in inwin})))
    if capture_since is None:
        return Observation(ObservationStatus.UNKNOWN, "no_outcome_capture_evidence")
    if end < capture_since:
        return Observation(ObservationStatus.UNKNOWN, "outcome_capture_started_later")
    if evaluated_at <= end:
        return Observation(ObservationStatus.UNKNOWN, "window_open")
    return Observation(ObservationStatus.NO_OUTCOME, "experiment_window_elapsed_no_outcome")

def evaluate_uplift(data: UpliftData, policy: UpliftPolicy, *, evaluated_at: datetime,
                    registry: ActionRegistry = DEFAULT_REGISTRY,
                    attribution_policy: AttributionPolicy | None = None) -> UpliftEvaluation:
    evaluated_at = _dt("evaluated_at", evaluated_at)
    attribution_policy = ATTRIBUTION_POLICY if attribution_policy is None else attribution_policy
    decisions = sorted(data.decisions, key=lambda d: (d["created_at"], d["decision_id"]))
    refs = [DecisionRef(d["decision_id"], d["chat_id"], d["created_at"]) for d in decisions]
    boundaries = [BoundaryRef.of(b) for b in data.boundaries]
    outcomes = [o for o in data.outcomes]
    if not all(isinstance(o, Outcome) for o in outcomes):
        raise ContractError("outcomes must be Outcome instances")
    results = attribute_all_v2(outcomes, refs, boundaries, interruptions=list(data.interruptions), policy=attribution_policy)

    bmap: dict[str, list[BoundaryRef]] = {}
    for b in boundaries:
        if b.decision_id:
            bmap.setdefault(b.decision_id, []).append(b)
    info = {r.decision_id: boundary_info(r, bmap.get(r.decision_id, ())) for r in refs}
    refs_by_id = {r.decision_id: r for r in refs}
    outs_by_id: dict[str, Outcome] = {}
    for o in outcomes:
        outs_by_id.setdefault(o.outcome_id, o)

    # --- integrity (never trusted, always re-derived)
    boundary_violations = sum(i.invalid for i in info.values())
    attribution_errors = 0
    for r in results:
        if r.relationship is not Relationship.ATTRIBUTED:
            continue
        d, o = refs_by_id.get(r.decision_id), outs_by_id.get(r.outcome_id)
        ok = d is not None and o is not None and d.chat_id == o.chat_id and o.observed_at > d.created_at
        if ok and r.method is Method.TEMPORAL:
            end = info[d.decision_id].end
            ok = end is not None and o.observed_at > end and o.decision_id is None
        elif ok and r.method is Method.DIRECT:
            ok = o.decision_id == d.decision_id
        attribution_errors += 0 if ok else 1

    # --- per-decision: action, features, observation
    capture_since = min((o.observed_at for o in outcomes), default=None)
    samples, vectors, missing = [], [], {}
    def miss(k: str, n: int = 1):
        if n:
            missing[k] = missing.get(k, 0) + n
    assignments_by_chat: dict[int, list[tuple]] = {}
    seen_assignment_ids: set[str] = set()
    for assignment in data.assignments:
        valid = (
            isinstance(assignment, tuple) and len(assignment) == 5
            and isinstance(assignment[0], str) and bool(assignment[0])
            and isinstance(assignment[1], int) and not isinstance(assignment[1], bool)
            and assignment[2] in ("CONTROL", "TREATMENT")
            and isinstance(assignment[3], (int, float)) and not isinstance(assignment[3], bool)
            and 0.0 < float(assignment[3]) < 1.0
            and isinstance(assignment[4], datetime) and assignment[4].tzinfo is not None
        )
        if not valid or assignment[0] in seen_assignment_ids:
            miss("invalid_experiment_assignment_rows")
            continue
        seen_assignment_ids.add(assignment[0])
        assignments_by_chat.setdefault(assignment[1], []).append(assignment)
    # More than one assignment for a chat is ambiguous; fail closed instead of choosing one.
    asg_by_chat = {chat_id: rows[0] for chat_id, rows in assignments_by_chat.items() if len(rows) == 1}
    ambiguous_assignment_chats = {chat_id for chat_id, rows in assignments_by_chat.items() if len(rows) != 1}
    # Each assignment is one randomized unit. Decision rows from assigned chats are excluded from
    # descriptive samples so they cannot be double-counted beside the ITT assignment sample.
    for chat_id in ambiguous_assignment_chats:
        miss("ambiguous_experiment_assignments")
    for source, count in data.unreadable.items():
        if count:
            miss("provider_unreadable_rows:" + str(source), int(count))
    if data.tables:
        for required_table in ("response_boundaries", "outcomes", "uplift_assignments"):
            if data.tables.get(required_table) is not True:
                miss("provider_required_table_unavailable:" + required_table)

    targets = tuple(policy.target_outcomes)
    for d, ref in zip(decisions, refs):
        mapped = map_decision(d.get("regime"), d.get("trigger"), version=policy.mapping_version, registry=registry,
                              decision_path=d.get("decision_path") or "llm_reply")
        action_id = mapped.action_id if (mapped.status is MappingStatus.MAPPED and mapped.sample_capable) else None
        if action_id is None:
            miss("decisions_with_unmapped_action")
            if mapped.status is MappingStatus.MAPPED:
                miss("decisions_mapped_not_sample_capable:" + str(mapped.behaviour_id))
            else:
                miss(("decisions_action_ambiguous:" if mapped.status is MappingStatus.AMBIGUOUS else "decisions_action_unmapped:") + mapped.reason)
        vectors.append(build_features(d["decision_id"], d["chat_id"], d["created_at"], d.get("state")))
        if d["chat_id"] in asg_by_chat:
            continue
        obs = observe_decision(d["decision_id"], d["chat_id"], info[d["decision_id"]].end, results,
                               evaluated_at=evaluated_at, target_types=policy.target_outcomes,
                               policy=attribution_policy, capture_since=capture_since)
        if obs.status is ObservationStatus.UNKNOWN:
            miss("decisions_observation_unknown:" + obs.reason)
        if info[d["decision_id"]].end is None and action_id != BASELINE_ACTION_ID:
            miss("decisions_without_sent_boundary")
        samples.append(Sample(d["decision_id"], action_id, obs.status))

    experiment_window = _baseline_window(policy, attribution_policy)
    for assignment_id, chat_id, arm, probability, assigned_at in asg_by_chat.values():
        action_id = BASELINE_ACTION_ID if arm == "CONTROL" else "FOLLOW_UP"
        if action_id not in registry:
            miss("experiment_assignment_action_unregistered")
            continue
        if experiment_window is None:
            obs = Observation(ObservationStatus.UNKNOWN, "window_unknown_for_target")
        else:
            obs = _experiment_observation(chat_id, assigned_at, outcomes, targets, experiment_window,
                                          capture_since, evaluated_at)
        if obs.status is ObservationStatus.UNKNOWN:
            miss("experiment_observation_unknown:" + obs.reason)
        samples.append(Sample("assignment:" + assignment_id, action_id, obs.status,
                              randomized=True, assignment_probability=float(probability)))
    est = estimate(samples, policy, registry)
    for r in results:
        if r.relationship is not Relationship.ATTRIBUTED:
            miss("outcomes_not_attributed:" + r.reason)
    if est.control_status == "CONTROL_UNAVAILABLE":
        miss("control_unavailable")
        miss("no_randomization_information")

    n_dec, n_out = len(decisions), len(results)
    with_b = [d for d in decisions if info[d["decision_id"]].end is not None]
    since = [d for d in decisions if with_b and d["created_at"] >= with_b[0]["created_at"]]
    attributed = sum(1 for r in results if r.relationship is Relationship.ATTRIBUTED)
    sent_total = data.boundaries_sent_total if data.boundaries_sent_total is not None else sum(
        1 for b in boundaries if b.status == "SENT" and b.is_boundary)
    metrics = {"unique_leads": data.unique_leads if data.unique_leads is not None else len({d["chat_id"] for d in decisions}),
               "decisions": data.decisions_total if data.decisions_total is not None else n_dec,
               "response_boundaries": sent_total,
               "outcomes": data.outcomes_total if data.outcomes_total is not None else n_out,
               "attributed_outcomes": attributed, "actions": est.actions, "eligible_samples": est.eligible,
               "control_samples": est.control_n, "treatment_samples": est.treatment_n}
    coverage = {"response_boundary_coverage": _ratio(len(with_b), n_dec),
                "response_boundary_coverage_since_capture": _ratio(len(with_b), len(since)) if with_b else None,
                "attribution_coverage": _ratio(attributed, n_out)}
    integrity = {"boundary_violations": boundary_violations, "attribution_integrity_errors": attribution_errors,
                 "leakage_violations": audit_vectors(vectors)}
    dataset_version = "ds_" + _h(sorted(d["decision_id"] for d in decisions), sorted(outs_by_id), sorted(b.boundary_id for b in boundaries),
                                 sorted(tuple(str(v) for v in a) for a in data.assignments if isinstance(a, tuple)),
                                 data.unique_leads, data.decisions_total, data.outcomes_total, data.boundaries_sent_total,
                                 data.assignments_total, dict(data.unreadable), dict(data.tables), data.truncated, policy.mapping_version,
                                 rules_fingerprint(policy.mapping_version))
    eid = evaluation_id_for(ESTIMATOR_VERSION, FEATURE_VERSION, policy.policy_version, dataset_version, evaluated_at.isoformat(),
                            est.data_quality.value, est.eligible, est.control_status, metrics, est.stats, est.estimates,
                            dict(sorted(missing.items())), bool(data.truncated), coverage, integrity)
    return UpliftEvaluation(eid, ESTIMATOR_VERSION, FEATURE_VERSION, policy.policy_version, dataset_version, evaluated_at,
                            est.data_quality, est.eligible, est.control_status, metrics, coverage, integrity, est.stats,
                            est.estimates, dict(sorted(missing.items())), bool(data.truncated))
