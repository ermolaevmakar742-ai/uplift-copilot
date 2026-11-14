"""Versioned, auditable, leakage-free feature contract (features.v1).

Only information available BEFORE the action: the Client State snapshot that production embedded in the decision record.
Each Feature carries feature_id, value, source and observed_at, and is validated:
  * feature_id in the closed FEATURE_IDS set, source in ALLOWED_SOURCES (outcome / deposit / future state / later messages /
    shadow / post-action evidence cannot be expressed: they have no id and no allowed source);
  * observed_at <= the decision time (as_of). Anything later raises LeakageError.
PII minimised: stage (canonical), intent label, sentiment number, country code, message count. No names, usernames or text."""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from copilot_contracts.stages import map_production_stage
from copilot_evidence.contracts import ContractError, _dt, _int
from copilot_state_delta.contract import StateSnapshot

FEATURE_VERSION = "features.v1"
FEATURE_IDS = ("stage", "intent", "sentiment", "region", "messages")
ALLOWED_SOURCES = frozenset({"client_state_snapshot"})


class LeakageError(ContractError):
    """A feature would carry information from after the action."""


@dataclass(frozen=True)
class Feature:
    feature_id: str
    value: Any
    source: str
    observed_at: datetime

    def __post_init__(self):
        if self.feature_id not in FEATURE_IDS:
            raise LeakageError(f"feature {self.feature_id!r} is not an allowed pre-action feature")
        if self.source not in ALLOWED_SOURCES:
            raise LeakageError(f"feature source {self.source!r} is not an allowed pre-action source")
        object.__setattr__(self, "observed_at", _dt("observed_at", self.observed_at))
        if self.value is None or isinstance(self.value, bool) or not isinstance(self.value, (str, int, float)):
            raise ContractError("feature value must be a known str/int/float (unknown features are simply absent)")

    def to_dict(self) -> dict:
        return {"feature_id": self.feature_id, "value": self.value, "source": self.source, "observed_at": self.observed_at.isoformat()}


@dataclass(frozen=True)
class FeatureVector:
    feature_version: str
    decision_id: str
    chat_id: int
    as_of: datetime                 # decision time
    features: tuple[Feature, ...]

    def __post_init__(self):
        object.__setattr__(self, "chat_id", _int("chat_id", self.chat_id))
        object.__setattr__(self, "as_of", _dt("as_of", self.as_of))
        if self.feature_version != FEATURE_VERSION:
            raise ContractError("unsupported feature_version")
        if not (isinstance(self.decision_id, str) and self.decision_id):
            raise ContractError("decision_id required")
        ids = [f.feature_id for f in self.features]
        if ids != sorted(set(ids), key=FEATURE_IDS.index):
            raise ContractError("features must be unique and in canonical order")
        for f in self.features:
            if f.observed_at > self.as_of:
                raise LeakageError(f"feature {f.feature_id} observed after the decision")

    def get(self, feature_id: str) -> Any:
        return next((f.value for f in self.features if f.feature_id == feature_id), None)

    def to_dict(self) -> dict:
        return {"feature_version": self.feature_version, "decision_id": self.decision_id, "chat_id": self.chat_id,
                "as_of": self.as_of.isoformat(), "features": [f.to_dict() for f in self.features]}

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def build_features(decision_id: str, chat_id: int, created_at: datetime, state: Mapping | None) -> FeatureVector:
    """From the Client State snapshot embedded in the decision (its observed_at is the decision time). Unknown -> absent."""
    snap = StateSnapshot.from_client_state(state, created_at)
    stage = None if snap.stage is None else map_production_stage(snap.stage).value
    vals = {"stage": None if stage == "UNKNOWN" else stage,          # an unmapped production stage is simply absent
            "intent": snap.intent, "sentiment": snap.sentiment, "region": snap.region, "messages": snap.messages}
    feats = tuple(Feature(i, vals[i], "client_state_snapshot", created_at) for i in FEATURE_IDS if vals[i] is not None)
    return FeatureVector(FEATURE_VERSION, decision_id, chat_id, created_at, feats)


def audit_vectors(vectors: Iterable[FeatureVector]) -> int:
    """Number of vectors that violate the leakage rules (re-validated independently of construction)."""
    bad = 0
    for v in vectors:
        try:
            FeatureVector(v.feature_version, v.decision_id, v.chat_id, v.as_of, tuple(Feature(f.feature_id, f.value, f.source, f.observed_at) for f in v.features))
        except ContractError:
            bad += 1
    return bad
