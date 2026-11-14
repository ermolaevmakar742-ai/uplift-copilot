"""Uplift policy: versioned, validated, LOCKABLE. Pure.

A policy is a frozen set of parameters. "Locked" means: policy.locked is True AND its fingerprint equals the one recorded in
LOCKED_FINGERPRINTS for that policy_version. Changing any parameter changes the fingerprint, so the lock gate FAILS until a new,
reviewed policy_version is recorded here. These are POLICY PARAMETERS (conservative defaults), not statistical claims.

recommend() is read-only: it names a registered action from the latest evaluation's ESTIMATED positive uplifts, or None.
Production influence additionally needs data quality >= min_quality_production (EXPERIMENTAL by default), so purely
observational data can be recorded in Shadow but can never steer a reply."""
import hashlib
import json
from dataclasses import dataclass, fields
from datetime import datetime
from types import MappingProxyType

from copilot_evidence.contracts import ContractError
from copilot_uplift.actions import DEFAULT_REGISTRY, ActionRegistry
from copilot_uplift.contract import DataQuality, EstimateStatus, UpliftEvaluation
from copilot_uplift.features import FeatureVector

POLICY_VERSION = "uplift-policy.v1"


class PolicyError(ContractError):
    pass


@dataclass(frozen=True)
class UpliftPolicy:
    policy_version: str
    locked: bool
    min_unique_leads: int = 500
    min_samples_per_arm: int = 30
    target_outcomes: tuple[str, ...] = ("CONTINUED",)
    min_quality_shadow: str = "OBSERVATIONAL"
    min_quality_production: str = "EXPERIMENTAL"
    max_evaluation_age_seconds: int = 7 * 86400
    registry_version: str = "actions.v1"
    mapping_version: str = "action-mapping.v1"      # which production-behaviour -> action mapping the evaluation uses

    def __post_init__(self):
        object.__setattr__(self, "target_outcomes", tuple(self.target_outcomes))
        if not (isinstance(self.policy_version, str) and self.policy_version.strip()):
            raise PolicyError("policy_version required")
        if not isinstance(self.locked, bool):
            raise PolicyError("locked must be bool")
        for f in ("min_unique_leads", "min_samples_per_arm", "max_evaluation_age_seconds"):
            v = getattr(self, f)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise PolicyError(f"{f} must be a positive int")
        if not self.target_outcomes or not all(isinstance(t, str) and t for t in self.target_outcomes):
            raise PolicyError("target_outcomes required")
        for f in ("min_quality_shadow", "min_quality_production"):
            if getattr(self, f) not in {q.value for q in DataQuality}:
                raise PolicyError(f"{f} must be a DataQuality")
        if DataQuality(self.min_quality_production).rank < DataQuality(self.min_quality_shadow).rank:
            raise PolicyError("production quality bar cannot be below the shadow bar")
        from copilot_uplift.action_mapping import SUPPORTED_VERSIONS
        if self.mapping_version not in SUPPORTED_VERSIONS:
            raise PolicyError("unknown mapping_version")
        if DataQuality(self.min_quality_production) is DataQuality.INSUFFICIENT_DATA:
            raise PolicyError("production cannot be allowed on INSUFFICIENT_DATA")

    def fingerprint(self) -> str:
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "locked"}
        d["target_outcomes"] = list(self.target_outcomes)
        return hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


POLICY_V1 = UpliftPolicy(POLICY_VERSION, True)                       # historical: action-mapping.v1 (regime label treated as action)
POLICY_VERSION_V2 = "uplift-policy.v2"
POLICY_V2 = UpliftPolicy(POLICY_VERSION_V2, True, mapping_version="action-mapping.v2")   # current default
DEFAULT_POLICY = POLICY_V2
LOCKED_FINGERPRINTS = MappingProxyType({POLICY_VERSION: "375cd689f6e56249ffa8f3fd", POLICY_VERSION_V2: "d992b51d704445b535cfeee5"})


def is_locked(policy: UpliftPolicy) -> bool:
    return isinstance(policy, UpliftPolicy) and policy.locked and LOCKED_FINGERPRINTS.get(policy.policy_version) == policy.fingerprint()


@dataclass(frozen=True)
class Recommendation:
    recommended_action: str | None
    alternative_action: str | None
    estimated_uplift: float | None
    data_quality: str
    policy_version: str
    reason: str
    eligible_for_production: bool


def _none(policy, quality, reason) -> Recommendation:
    return Recommendation(None, None, None, quality, policy.policy_version, reason, False)


def recommend(vector: FeatureVector | None, evaluation: UpliftEvaluation | None, policy: UpliftPolicy,
              registry: ActionRegistry = DEFAULT_REGISTRY, now: datetime | None = None) -> Recommendation:
    """Never raises for bad inputs: anything unusable yields (None, reason). v1 estimates are marginal (not conditioned on
    the feature vector); the vector is required only to prove the decision has leak-free pre-action features."""
    try:
        if not isinstance(policy, UpliftPolicy):
            return Recommendation(None, None, None, "INSUFFICIENT_DATA", "unknown", "missing_policy", False)
        if not isinstance(evaluation, UpliftEvaluation):
            return _none(policy, "INSUFFICIENT_DATA", "no_evaluation")
        q = evaluation.data_quality
        if not isinstance(vector, FeatureVector) or not vector.features:
            return _none(policy, q.value, "no_features")
        if evaluation.policy_version != policy.policy_version:
            return _none(policy, q.value, "policy_version_mismatch")
        if now is not None and (now - evaluation.evaluation_timestamp).total_seconds() > policy.max_evaluation_age_seconds:
            return _none(policy, q.value, "stale_evaluation")
        if q.rank < DataQuality(policy.min_quality_shadow).rank:
            return _none(policy, q.value, "data_quality_insufficient")
        cands = [e for e in evaluation.uplift_estimates if e.status is EstimateStatus.ESTIMATED and e.value > 0
                 and e.action_id in registry and not registry.get(e.action_id).baseline]
        if not cands:
            return _none(policy, q.value, "no_positive_estimate")
        cands.sort(key=lambda e: (-e.value, e.action_id))
        if len(cands) > 1 and cands[0].value == cands[1].value:
            return _none(policy, q.value, "tie_between_actions")
        top = cands[0]
        provider_complete = (not evaluation.truncated and not any(
            v for k, v in evaluation.missing_signals.items()
            if k.startswith("provider_unreadable_rows:")
            or k.startswith("provider_required_table_unavailable:")
                         or k.startswith("experiment_observation_unknown:")
            or k in {"ambiguous_experiment_assignments", "invalid_experiment_assignment_rows"}
        ))
        return Recommendation(top.action_id, cands[1].action_id if len(cands) > 1 else None, top.value, q.value,
                              policy.policy_version, "positive_estimated_uplift",
                              q.rank >= DataQuality(policy.min_quality_production).rank and provider_complete)
    except Exception:
        return Recommendation(None, None, None, "INSUFFICIENT_DATA", getattr(policy, "policy_version", "unknown"), "malformed_input", False)
