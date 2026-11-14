"""Uplift estimator v2: descriptive rates are separated from randomized experiment estimates.

Only one post-assignment index decision per chat can enter the experiment. The caller marks it
randomized=True only when the decision is after assignment and matches the assigned arm:
CONTROL -> DO_NOTHING; TREATMENT -> a registered non-control action.

The experiment assigns only a generic TREATMENT arm, not individual actions. Therefore an
action-specific contrast is emitted only when exactly one treatment action is present in the
randomized sample. Multiple treatment actions are not independently randomized and are not
compared as if they were. Descriptive rates remain available for all eligible observations.
"""
from dataclasses import dataclass
from typing import Iterable

from copilot_prediction.contract import ObservationStatus
from copilot_uplift.actions import BASELINE_ACTION_ID, DEFAULT_REGISTRY, ActionRegistry
from copilot_uplift.contract import ActionStats, DataQuality, EstimateStatus, UpliftEstimate
from copilot_uplift.policy import UpliftPolicy

ESTIMATOR_VERSION = "estimator.v2"


@dataclass(frozen=True)
class Sample:
    decision_id: str
    action_id: str | None
    status: ObservationStatus
    randomized: bool = False
    assignment_probability: float | None = None


@dataclass(frozen=True)
class EstimatorResult:
    stats: tuple[ActionStats, ...]
    estimates: tuple[UpliftEstimate, ...]
    eligible: int
    control_n: int
    treatment_n: int
    control_status: str
    data_quality: DataQuality
    actions: int


def _rate(observed: int, no_outcome: int):
    d = observed + no_outcome
    return None if d == 0 else round(observed / d, 6)


def estimate(samples: Iterable[Sample], policy: UpliftPolicy, registry: ActionRegistry = DEFAULT_REGISTRY) -> EstimatorResult:
    by: dict[str, list[Sample]] = {}
    unknown_by: dict[str, int] = {}
    for s in sorted(samples, key=lambda x: x.decision_id):
        if s.action_id is None or s.action_id not in registry:
            continue
        if s.status is ObservationStatus.UNKNOWN:
            unknown_by[s.action_id] = unknown_by.get(s.action_id, 0) + 1
        else:
            by.setdefault(s.action_id, []).append(s)

    stats = []
    for a in sorted(set(by) | set(unknown_by)):
        rows = by.get(a, [])
        obs = sum(1 for r in rows if r.status is ObservationStatus.OBSERVED)
        stats.append(ActionStats(a, len(rows), obs, len(rows) - obs, unknown_by.get(a, 0),
                                 _rate(obs, len(rows) - obs)))
    st = {x.action_id: x for x in stats}
    eligible = sum(x.eligible for x in stats)

    # randomized=True means membership in the assigned experiment, not "is control".
    control = [r for r in by.get(BASELINE_ACTION_ID, []) if r.randomized]
    treatment_by_action = {
        a: [r for r in rows if r.randomized]
        for a, rows in by.items() if a != BASELINE_ACTION_ID and any(r.randomized for r in rows)
    }
    treatment = [r for rows in treatment_by_action.values() for r in rows]
    n_min = policy.min_samples_per_arm

    if eligible < n_min or (control and treatment and (len(control) < n_min or len(treatment) < n_min)):
        quality = DataQuality.INSUFFICIENT_DATA
    elif control and treatment:
        experiment_rows = control + treatment
        allp = all(r.assignment_probability is not None and 0 < r.assignment_probability < 1
                   for r in experiment_rows)
        quality = DataQuality.CAUSAL_READY if allp else DataQuality.EXPERIMENTAL
    else:
        quality = DataQuality.OBSERVATIONAL

    estimates = []
    treatment_actions = sorted(treatment_by_action)
    for action_id in sorted(a for a in st if a != BASELINE_ACTION_ID):
        action_rows = treatment_by_action.get(action_id, [])
        treatment_n = len(action_rows)
        if not control:
            estimates.append(UpliftEstimate(action_id, EstimateStatus.CONTROL_UNAVAILABLE, None, 0, 0,
                                            quality, "no_randomized_control"))
        elif not action_rows:
            estimates.append(UpliftEstimate(action_id, EstimateStatus.INSUFFICIENT_DATA, None, 0, len(control),
                                            quality, "not_in_randomized_experiment"))
        elif len(treatment_actions) != 1:
            estimates.append(UpliftEstimate(action_id, EstimateStatus.INSUFFICIENT_DATA, None, treatment_n,
                                            len(control), quality,
                                            "multiple_treatment_actions_not_independently_randomized"))
        elif treatment_n < n_min or len(control) < n_min:
            estimates.append(UpliftEstimate(action_id, EstimateStatus.INSUFFICIENT_DATA, None, treatment_n,
                                            len(control), quality, "below_min_samples_per_arm"))
        else:
            c_obs = sum(1 for r in control if r.status is ObservationStatus.OBSERVED)
            t_obs = sum(1 for r in action_rows if r.status is ObservationStatus.OBSERVED)
            value = round(_rate(t_obs, treatment_n - t_obs) - _rate(c_obs, len(control) - c_obs), 6)
            estimates.append(UpliftEstimate(action_id, EstimateStatus.ESTIMATED, value, treatment_n, len(control),
                                            quality, "randomized_index_decision_rate_difference_vs_control"))

    return EstimatorResult(tuple(stats), tuple(estimates), eligible, len(control), len(treatment),
                           "AVAILABLE" if control else "CONTROL_UNAVAILABLE", quality,
                           len([a for a in st if st[a].eligible > 0]))
