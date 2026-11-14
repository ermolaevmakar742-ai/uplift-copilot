"""Uplift contracts. Immutable, strict, deterministic ids; pure (stdlib + copilot_evidence.contracts helpers).

DataQuality (what the data can support, not what the estimate is):
  INSUFFICIENT_DATA  too few eligible samples (below policy.min_samples_per_arm) -> no estimate is made
  OBSERVATIONAL      enough samples, but assignment was not randomized: associations only, NEVER a proven causal uplift
  EXPERIMENTAL       a randomized control exists with enough samples per arm
  CAUSAL_READY       EXPERIMENTAL + recorded assignment probabilities for every sample (not reachable from today's data)
EstimateStatus: ESTIMATED | INSUFFICIENT_DATA | CONTROL_UNAVAILABLE. An estimate that cannot be made is None, never 0.
ShadowRecord: what Shadow Uplift computed for one decision. It never changes a reply.
ActivationEvent: one append-only state change of the activation controller.
"""
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from copilot_evidence.contracts import ContractError, _dt, _enum, _freeze, _int, _thaw

EVALUATION_CONTRACT_VERSION = "uplift_evaluation.v1"
SHADOW_CONTRACT_VERSION = "uplift_shadow.v1"
ACTIVATION_CONTRACT_VERSION = "uplift_activation.v1"


class DataQuality(str, Enum):
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    OBSERVATIONAL = "OBSERVATIONAL"
    EXPERIMENTAL = "EXPERIMENTAL"
    CAUSAL_READY = "CAUSAL_READY"

    @property
    def rank(self) -> int:
        return list(DataQuality).index(self)


class EstimateStatus(str, Enum):
    ESTIMATED = "ESTIMATED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    CONTROL_UNAVAILABLE = "CONTROL_UNAVAILABLE"


def _h(prefix: str, *parts) -> str:
    return prefix + hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]


def _keys(d: Any, keys: tuple, what: str) -> None:
    if not isinstance(d, Mapping) or set(d) != set(keys):
        raise ContractError(f"{what}: exact keys required {sorted(keys)}")


@dataclass(frozen=True)
class ActionStats:
    action_id: str
    eligible: int
    observed: int
    no_outcome: int
    unknown: int
    rate: float | None            # observed / (observed + no_outcome); None when that denominator is 0

    def __post_init__(self):
        for f in ("eligible", "observed", "no_outcome", "unknown"):
            v = getattr(self, f)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ContractError(f"{f} must be an int >= 0")
        if self.eligible != self.observed + self.no_outcome:
            raise ContractError("eligible must equal observed + no_outcome")
        if self.rate is not None and not (isinstance(self.rate, (int, float)) and math.isfinite(self.rate) and 0 <= self.rate <= 1):
            raise ContractError("rate must be within [0, 1] or None")
        if (self.rate is None) != (self.eligible == 0):
            raise ContractError("rate is None exactly when there are no eligible samples")

    def to_dict(self) -> dict:
        return {"action_id": self.action_id, "eligible": self.eligible, "observed": self.observed,
                "no_outcome": self.no_outcome, "unknown": self.unknown, "rate": self.rate}

    @classmethod
    def from_dict(cls, d: Mapping) -> "ActionStats":
        _keys(d, ("action_id", "eligible", "observed", "no_outcome", "unknown", "rate"), "action stats")
        return cls(**dict(d))


@dataclass(frozen=True)
class UpliftEstimate:
    action_id: str
    status: EstimateStatus
    value: float | None
    treatment_n: int
    control_n: int
    data_quality: DataQuality
    reason: str

    def __post_init__(self):
        object.__setattr__(self, "status", _enum(EstimateStatus, "status", self.status))
        object.__setattr__(self, "data_quality", _enum(DataQuality, "data_quality", self.data_quality))
        if (self.status is EstimateStatus.ESTIMATED) != (self.value is not None):
            raise ContractError("value is present exactly when ESTIMATED (a missing estimate is never 0)")
        if self.value is not None and not (isinstance(self.value, (int, float)) and math.isfinite(self.value) and -1 <= self.value <= 1):
            raise ContractError("uplift value must be a finite number within [-1, 1]")
        if self.status is EstimateStatus.ESTIMATED and self.data_quality is DataQuality.INSUFFICIENT_DATA:
            raise ContractError("an ESTIMATED value cannot have INSUFFICIENT_DATA quality")
        for f in ("treatment_n", "control_n"):
            v = getattr(self, f)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ContractError(f"{f} must be an int >= 0")

    def to_dict(self) -> dict:
        return {"action_id": self.action_id, "status": self.status.value, "value": self.value, "treatment_n": self.treatment_n,
                "control_n": self.control_n, "data_quality": self.data_quality.value, "reason": self.reason}

    @classmethod
    def from_dict(cls, d: Mapping) -> "UpliftEstimate":
        _keys(d, ("action_id", "status", "value", "treatment_n", "control_n", "data_quality", "reason"), "estimate")
        return cls(**dict(d))


METRIC_KEYS = ("unique_leads", "decisions", "response_boundaries", "outcomes", "attributed_outcomes", "actions",
               "eligible_samples", "control_samples", "treatment_samples")
COVERAGE_KEYS = ("response_boundary_coverage", "response_boundary_coverage_since_capture", "attribution_coverage")
INTEGRITY_KEYS = ("boundary_violations", "attribution_integrity_errors", "leakage_violations")


@dataclass(frozen=True)
class UpliftEvaluation:
    evaluation_id: str
    estimator_version: str
    feature_version: str
    policy_version: str
    dataset_version: str
    evaluation_timestamp: datetime
    data_quality: DataQuality
    sample_count: int
    control_status: str                          # "CONTROL_UNAVAILABLE" | "AVAILABLE"
    metrics: Mapping[str, int]
    coverage: Mapping[str, float | None]
    integrity: Mapping[str, int]
    conversion_by_action: tuple[ActionStats, ...]
    uplift_estimates: tuple[UpliftEstimate, ...]
    missing_signals: Mapping[str, int]
    truncated: bool = False
    contract_version: str = EVALUATION_CONTRACT_VERSION

    def __post_init__(self):
        s = object.__setattr__
        s(self, "data_quality", _enum(DataQuality, "data_quality", self.data_quality))
        s(self, "evaluation_timestamp", _dt("evaluation_timestamp", self.evaluation_timestamp))
        if self.contract_version != EVALUATION_CONTRACT_VERSION:
            raise ContractError("unsupported contract_version")
        for f in ("estimator_version", "feature_version", "policy_version", "dataset_version"):
            if not (isinstance(getattr(self, f), str) and getattr(self, f)):
                raise ContractError(f"{f} required")
        if self.control_status not in ("CONTROL_UNAVAILABLE", "AVAILABLE"):
            raise ContractError("invalid control_status")
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int) or self.sample_count < 0:
            raise ContractError("sample_count must be an int >= 0")
        m, c, i, ms = _freeze(dict(self.metrics)), _freeze(dict(self.coverage)), _freeze(dict(self.integrity)), _freeze(dict(self.missing_signals))
        _keys(m, METRIC_KEYS, "metrics"), _keys(c, COVERAGE_KEYS, "coverage"), _keys(i, INTEGRITY_KEYS, "integrity")
        for k, v in list(m.items()) + list(i.items()) + list(ms.items()):
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ContractError(f"{k} must be an int >= 0")
        for k, v in c.items():
            if v is not None and not (isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 1):
                raise ContractError(f"coverage {k} must be within [0, 1] or None")
        if m["eligible_samples"] != self.sample_count:
            raise ContractError("sample_count must equal metrics.eligible_samples")
        s(self, "metrics", m), s(self, "coverage", c), s(self, "integrity", i), s(self, "missing_signals", ms)
        s(self, "conversion_by_action", tuple(self.conversion_by_action)), s(self, "uplift_estimates", tuple(self.uplift_estimates))
        if not all(isinstance(x, ActionStats) for x in self.conversion_by_action) or not all(isinstance(x, UpliftEstimate) for x in self.uplift_estimates):
            raise ContractError("conversion_by_action / uplift_estimates hold ActionStats / UpliftEstimate")
        if self.evaluation_id != make_evaluation_id(self):
            raise ContractError("evaluation_id does not match content")

    def to_dict(self) -> dict:
        return {"contract_version": self.contract_version, "evaluation_id": self.evaluation_id,
                "estimator_version": self.estimator_version, "feature_version": self.feature_version,
                "policy_version": self.policy_version, "dataset_version": self.dataset_version,
                "evaluation_timestamp": self.evaluation_timestamp.isoformat(), "data_quality": self.data_quality.value,
                "sample_count": self.sample_count, "control_status": self.control_status, "metrics": _thaw(self.metrics),
                "coverage": _thaw(self.coverage), "integrity": _thaw(self.integrity),
                "conversion_by_action": [x.to_dict() for x in self.conversion_by_action],
                "uplift_estimates": [x.to_dict() for x in self.uplift_estimates],
                "missing_signals": _thaw(self.missing_signals), "truncated": self.truncated}

    @classmethod
    def from_dict(cls, d: Mapping) -> "UpliftEvaluation":
        keys = ("contract_version", "evaluation_id", "estimator_version", "feature_version", "policy_version", "dataset_version",
                "evaluation_timestamp", "data_quality", "sample_count", "control_status", "metrics", "coverage", "integrity",
                "conversion_by_action", "uplift_estimates", "missing_signals", "truncated")
        _keys(d, keys, "uplift evaluation")
        for k in ("conversion_by_action", "uplift_estimates"):
            if not isinstance(d[k], (list, tuple)):
                raise ContractError(f"{k} must be a list")
        if not isinstance(d["truncated"], bool):
            raise ContractError("truncated must be bool")
        return cls(evaluation_id=d["evaluation_id"], estimator_version=d["estimator_version"], feature_version=d["feature_version"],
                   policy_version=d["policy_version"], dataset_version=d["dataset_version"], evaluation_timestamp=d["evaluation_timestamp"],
                   data_quality=d["data_quality"], sample_count=d["sample_count"], control_status=d["control_status"],
                   metrics=d["metrics"], coverage=d["coverage"], integrity=d["integrity"],
                   conversion_by_action=tuple(ActionStats.from_dict(x) for x in d["conversion_by_action"]),
                   uplift_estimates=tuple(UpliftEstimate.from_dict(x) for x in d["uplift_estimates"]),
                   missing_signals=d["missing_signals"], truncated=d["truncated"], contract_version=d["contract_version"])


def evaluation_id_for(estimator_version, feature_version, policy_version, dataset_version, ts_iso, data_quality, sample_count,
                      control_status, metrics, stats, estimates, missing_signals=None, truncated=False) -> str:
    return _h("ue_", estimator_version, feature_version, policy_version, dataset_version, ts_iso, data_quality, sample_count,
              control_status, dict(metrics), [x.to_dict() for x in stats], [x.to_dict() for x in estimates],
              dict(missing_signals or {}), bool(truncated))


def make_evaluation_id(e: "UpliftEvaluation") -> str:
    return evaluation_id_for(e.estimator_version, e.feature_version, e.policy_version, e.dataset_version,
                             e.evaluation_timestamp.isoformat(), e.data_quality.value, e.sample_count, e.control_status,
                             _thaw(e.metrics), e.conversion_by_action, e.uplift_estimates, _thaw(e.missing_signals), e.truncated)


@dataclass(frozen=True)
class ShadowRecord:
    shadow_id: str
    decision_id: str
    chat_id: int
    computed_at: datetime
    features: Mapping[str, Any]
    action_taken: str | None             # registry action mapped from what production did (None = unmapped)
    recommended_action: str | None
    alternative_action: str | None
    estimated_uplift: float | None
    data_quality: DataQuality
    policy_version: str
    estimator_version: str
    feature_version: str
    evaluation_id: str | None
    reason: str
    contract_version: str = SHADOW_CONTRACT_VERSION

    def __post_init__(self):
        s = object.__setattr__
        s(self, "chat_id", _int("chat_id", self.chat_id))
        s(self, "computed_at", _dt("computed_at", self.computed_at))
        s(self, "data_quality", _enum(DataQuality, "data_quality", self.data_quality))
        s(self, "features", _freeze(dict(self.features)))
        if self.contract_version != SHADOW_CONTRACT_VERSION:
            raise ContractError("unsupported contract_version")
        if not (isinstance(self.decision_id, str) and self.decision_id):
            raise ContractError("decision_id required")
        if (self.estimated_uplift is None) != (self.recommended_action is None):
            raise ContractError("estimated_uplift is present exactly when an action is recommended")
        if self.estimated_uplift is not None and not (isinstance(self.estimated_uplift, (int, float)) and math.isfinite(self.estimated_uplift)):
            raise ContractError("estimated_uplift must be finite")
        if self.alternative_action is not None and self.recommended_action is None:
            raise ContractError("an alternative needs a recommendation")
        if not (isinstance(self.reason, str) and self.reason):
            raise ContractError("reason required")
        if self.shadow_id != make_shadow_id(self.decision_id, self.policy_version, self.estimator_version, self.feature_version):
            raise ContractError("shadow_id does not match content")

    def to_dict(self) -> dict:
        return {"contract_version": self.contract_version, "shadow_id": self.shadow_id, "decision_id": self.decision_id,
                "chat_id": self.chat_id, "computed_at": self.computed_at.isoformat(), "features": _thaw(self.features),
                "action_taken": self.action_taken, "recommended_action": self.recommended_action,
                "alternative_action": self.alternative_action, "estimated_uplift": self.estimated_uplift,
                "data_quality": self.data_quality.value, "policy_version": self.policy_version,
                "estimator_version": self.estimator_version, "feature_version": self.feature_version,
                "evaluation_id": self.evaluation_id, "reason": self.reason}

    @classmethod
    def from_dict(cls, d: Mapping) -> "ShadowRecord":
        keys = ("contract_version", "shadow_id", "decision_id", "chat_id", "computed_at", "features", "action_taken",
                "recommended_action", "alternative_action", "estimated_uplift", "data_quality", "policy_version",
                "estimator_version", "feature_version", "evaluation_id", "reason")
        _keys(d, keys, "shadow record")
        return cls(**{k: d[k] for k in keys})


def make_shadow_id(decision_id, policy_version, estimator_version, feature_version) -> str:
    return _h("us_", decision_id, policy_version, estimator_version, feature_version)


class ActivationState(str, Enum):
    OFF = "OFF"
    READY = "READY"          # derived only: never stored
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"


STORED_STATES = (ActivationState.OFF, ActivationState.ACTIVE, ActivationState.BLOCKED)


@dataclass(frozen=True)
class ActivationEvent:
    event_id: str
    previous_state: ActivationState
    new_state: ActivationState
    changed_by: str
    changed_at: datetime
    reason: str
    policy_version: str
    gates: Mapping[str, Any]
    contract_version: str = ACTIVATION_CONTRACT_VERSION

    def __post_init__(self):
        s = object.__setattr__
        s(self, "previous_state", _enum(ActivationState, "previous_state", self.previous_state))
        s(self, "new_state", _enum(ActivationState, "new_state", self.new_state))
        s(self, "changed_at", _dt("changed_at", self.changed_at))
        s(self, "gates", _freeze(dict(self.gates)))
        if self.previous_state not in STORED_STATES or self.new_state not in STORED_STATES:
            raise ContractError("READY is derived and cannot be stored")
        for f in ("changed_by", "reason", "policy_version"):
            v = getattr(self, f)
            if not (isinstance(v, str) and v.strip()) or len(v) > 500:
                raise ContractError(f"{f} must be a non-empty str (<= 500)")
        if self.contract_version != ACTIVATION_CONTRACT_VERSION:
            raise ContractError("unsupported contract_version")
        if self.event_id != make_event_id(self.previous_state.value, self.new_state.value, self.changed_by,
                                          self.changed_at.isoformat(), self.policy_version):
            raise ContractError("event_id does not match content")

    def to_dict(self) -> dict:
        return {"contract_version": self.contract_version, "event_id": self.event_id, "previous_state": self.previous_state.value,
                "new_state": self.new_state.value, "changed_by": self.changed_by, "changed_at": self.changed_at.isoformat(),
                "reason": self.reason, "policy_version": self.policy_version, "gates": _thaw(self.gates)}

    @classmethod
    def from_dict(cls, d: Mapping) -> "ActivationEvent":
        keys = ("contract_version", "event_id", "previous_state", "new_state", "changed_by", "changed_at", "reason",
                "policy_version", "gates")
        _keys(d, keys, "activation event")
        return cls(**{k: d[k] for k in keys})


def make_event_id(prev, new, by, at_iso, policy_version) -> str:
    return _h("ua_", prev, new, by, at_iso, policy_version)


def new_event(previous: ActivationState, new: ActivationState, *, changed_by: str, changed_at: datetime, reason: str,
              policy_version: str, gates: Mapping | None = None) -> ActivationEvent:
    at = _dt("changed_at", changed_at)
    return ActivationEvent(make_event_id(previous.value, new.value, changed_by, at.isoformat(), policy_version), previous, new,
                           changed_by, at, reason, policy_version, dict(gates or {}))
