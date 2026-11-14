"""Uplift estimator v1: deterministic, versioned, offline, reproducible. No LLM, no network, no randomness, no production imports.

Marginal observed-rate difference (not a model):
  rate(A)  = observed / (observed + no_outcome)   over ELIGIBLE samples of action A   (None when the denominator is 0)
  uplift(A) = rate(A) - rate(control)   only when a CONTROL exists
Eligible sample = a decision with a registry action, a measurable outcome status OBSERVED or NO_OUTCOME (UNKNOWN is excluded and
counted separately: absence of an outcome is only counted as a non-event once its window is over and capture was running).
CONTROL = DO_NOTHING samples that carry RANDOMIZATION information (sample.randomized). DO_NOTHING without it is just an
observational action: control_status stays CONTROL_UNAVAILABLE and no uplift is estimated (value None, never 0).
Data quality: INSUFFICIENT_DATA (< min_samples_per_arm eligible) / OBSERVATIONAL / EXPERIMENTAL (randomized control and an arm
both >= min) / CAUSAL_READY (EXPERIMENTAL and an assignment probability recorded for every sample)."""
from dataclasses import dataclass
from typing import Iterable

from copilot_prediction.contract import ObservationStatus
from copilot_uplift.actions import BASELINE_ACTION_ID, DEFAULT_REGISTRY, ActionRegistry
from copilot_uplift.contract import ActionStats, DataQuality, EstimateStatus, UpliftEstimate
from copilot_uplift.policy import UpliftPolicy

ESTIMATOR_VERSION = "estimator.v1"


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
            continue                                               # unmapped action: not a sample of any action
        if s.status is ObservationStatus.UNKNOWN:
            unknown_by[s.action_id] = unknown_by.get(s.action_id, 0) + 1
        else:
            by.setdefault(s.action_id, []).append(s)
    stats = []
    for a in sorted(set(by) | set(unknown_by)):
        rows = by.get(a, [])
        obs = sum(1 for r in rows if r.status is ObservationStatus.OBSERVED)
        stats.append(ActionStats(a, len(rows), obs, len(rows) - obs, unknown_by.get(a, 0), _rate(obs, len(rows) - obs)))
    st = {x.action_id: x for x in stats}
    eligible = sum(x.eligible for x in stats)
    control = [r for r in by.get(BASELINE_ACTION_ID, []) if r.randomized]
    treat = [r for a, rows in by.items() if a != BASELINE_ACTION_ID for r in rows]
    n_min = policy.min_samples_per_arm
    control_ok = len(control) > 0
    arm_ready = any(len(rows) >= n_min for a, rows in by.items() if a != BASELINE_ACTION_ID)
    if eligible < n_min:
        quality = DataQuality.INSUFFICIENT_DATA
    elif control_ok and len(control) >= n_min and arm_ready:
        allp = all(r.assignment_probability is not None and 0 < r.assignment_probability < 1 for r in control + treat)
        quality = DataQuality.CAUSAL_READY if allp else DataQuality.EXPERIMENTAL
    else:
        quality = DataQuality.OBSERVATIONAL
    ests = []
    for a in sorted(x for x in st if x != BASELINE_ACTION_ID):
        n_t = st[a].eligible
        if not control_ok:
            ests.append(UpliftEstimate(a, EstimateStatus.CONTROL_UNAVAILABLE, None, n_t, 0, quality, "no_randomized_control"))
        elif n_t < n_min or len(control) < n_min:
            ests.append(UpliftEstimate(a, EstimateStatus.INSUFFICIENT_DATA, None, n_t, len(control), quality, "below_min_samples_per_arm"))
        else:
            c_obs = sum(1 for r in control if r.status is ObservationStatus.OBSERVED)
            val = round(st[a].rate - _rate(c_obs, len(control) - c_obs), 6)
            ests.append(UpliftEstimate(a, EstimateStatus.ESTIMATED, val, n_t, len(control), quality,
                                       "observed_rate_difference_vs_randomized_control"))
    return EstimatorResult(tuple(stats), tuple(ests), eligible, len(control), len(treat),
                           "AVAILABLE" if control_ok else "CONTROL_UNAVAILABLE", quality, len([a for a in st if st[a].eligible > 0]))
