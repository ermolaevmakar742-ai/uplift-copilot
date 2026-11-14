"""prediction_outcome.v1 contracts. Immutable, strict, deterministic ids. Pure (stdlib + copilot_evidence.contracts).

Prediction: an expectation that ALREADY exists in production data. Today there is exactly one source:
  Shadow momentum.direction (RISING | FALLING | STABLE), a rule-based reading of the user's engagement trend.
  expectation.v1 maps it to expected outcome types:  RISING -> {CONTINUED};  FALLING / STABLE -> no stated expectation.
  This mapping is a documented, versioned definition, not a model output; nothing else is predicted.
PredictionComparison: status
  OBSERVED    at least one outcome was attributed to the decision (outcome_ids listed)
  NO_OUTCOME  the observation window is over, the outcome pipeline was running, nothing attributable was seen.
              NEUTRAL: it is NOT a negative outcome and is never counted as one
  UNKNOWN     cannot be told (no sent boundary, window still open, ambiguous attribution, window unknown, ...)
relation (meaningful only for OBSERVED with a stated expectation): CONSISTENT | DIFFERENT; otherwise NOT_COMPARABLE.
A prediction recorded AFTER the outcome can never be compared (NOT_COMPARABLE, reason prediction_recorded_after_outcome).
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from copilot_evidence.contracts import ContractError, _dt, _enum, _freeze, _int, _thaw

CONTRACT_VERSION = "prediction_outcome.v1"
EXPECTATION_VERSION = "expectation.v1"
EXPECTATIONS: Mapping[str, tuple] = {"RISING": ("CONTINUED",), "FALLING": (), "STABLE": ()}


class PredictionKind(str, Enum):
    MOMENTUM_DIRECTION = "MOMENTUM_DIRECTION"


class ObservationStatus(str, Enum):
    OBSERVED = "OBSERVED"
    NO_OUTCOME = "NO_OUTCOME"
    UNKNOWN = "UNKNOWN"


class Relation(str, Enum):
    CONSISTENT = "CONSISTENT"
    DIFFERENT = "DIFFERENT"
    NOT_COMPARABLE = "NOT_COMPARABLE"


REASONS = frozenset({
    "attributed_outcome_observed", "window_elapsed_no_attributed_outcome", "no_sent_response_boundary", "window_open",
    "ambiguous_attribution", "window_unknown_for_target", "no_outcome_capture_evidence", "outcome_capture_started_later",
    "no_expectation_stated", "prediction_recorded_after_outcome", "unresolved_attribution"})


def _id(prefix: str, *parts) -> str:
    return prefix + hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()[:20]


@dataclass(frozen=True)
class Prediction:
    prediction_id: str
    decision_id: str
    chat_id: int
    kind: PredictionKind
    predicted_value: str
    expected_outcome_types: tuple[str, ...]
    expectation_version: str
    source_record_id: str            # the Shadow record the value was read from
    as_of: datetime                  # the decision time the value describes (no later information)
    recorded_at: datetime            # when the source record was written

    def __post_init__(self):
        s = object.__setattr__
        s(self, "kind", _enum(PredictionKind, "kind", self.kind))
        s(self, "chat_id", _int("chat_id", self.chat_id))
        s(self, "as_of", _dt("as_of", self.as_of))
        s(self, "recorded_at", _dt("recorded_at", self.recorded_at))
        if not (isinstance(self.decision_id, str) and self.decision_id):
            raise ContractError("decision_id required")
        if self.predicted_value not in EXPECTATIONS:
            raise ContractError(f"unknown predicted_value {self.predicted_value!r}")
        if self.expectation_version != EXPECTATION_VERSION:
            raise ContractError("unsupported expectation_version")
        s(self, "expected_outcome_types", tuple(self.expected_outcome_types))
        if self.expected_outcome_types != tuple(EXPECTATIONS[self.predicted_value]):
            raise ContractError("expected_outcome_types do not match expectation.v1")
        if not (isinstance(self.source_record_id, str) and self.source_record_id):
            raise ContractError("source_record_id required (no prediction without a source record)")
        if self.prediction_id != make_prediction_id(self.decision_id, self.kind.value, self.predicted_value, self.source_record_id):
            raise ContractError("prediction_id does not match content")

    def to_dict(self) -> dict:
        return {"prediction_id": self.prediction_id, "decision_id": self.decision_id, "chat_id": self.chat_id,
                "kind": self.kind.value, "predicted_value": self.predicted_value,
                "expected_outcome_types": list(self.expected_outcome_types), "expectation_version": self.expectation_version,
                "source_record_id": self.source_record_id, "as_of": self.as_of.isoformat(),
                "recorded_at": self.recorded_at.isoformat()}

    @classmethod
    def from_dict(cls, d: Mapping) -> "Prediction":
        keys = ("prediction_id", "decision_id", "chat_id", "kind", "predicted_value", "expected_outcome_types",
                "expectation_version", "source_record_id", "as_of", "recorded_at")
        if not isinstance(d, Mapping) or set(d) != set(keys):
            raise ContractError("prediction: exact keys required")
        if not isinstance(d["expected_outcome_types"], (list, tuple)):
            raise ContractError("expected_outcome_types must be a list")
        return cls(**{**{k: d[k] for k in keys}, "expected_outcome_types": tuple(d["expected_outcome_types"])})


def make_prediction_id(decision_id, kind, value, source_record_id) -> str:
    return _id("pr_", EXPECTATION_VERSION, decision_id, kind, value, source_record_id)


@dataclass(frozen=True)
class PredictionComparison:
    comparison_id: str
    prediction_id: str
    decision_id: str
    chat_id: int
    status: ObservationStatus
    relation: Relation
    outcome_ids: tuple[str, ...]
    observed_outcome_types: tuple[str, ...]
    evaluated_at: datetime
    reason: str
    metadata: Mapping[str, Any]
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self):
        s = object.__setattr__
        st, rel = _enum(ObservationStatus, "status", self.status), _enum(Relation, "relation", self.relation)
        s(self, "status", st), s(self, "relation", rel)
        s(self, "chat_id", _int("chat_id", self.chat_id))
        s(self, "evaluated_at", _dt("evaluated_at", self.evaluated_at))
        s(self, "outcome_ids", tuple(self.outcome_ids)), s(self, "observed_outcome_types", tuple(self.observed_outcome_types))
        if self.contract_version != CONTRACT_VERSION:
            raise ContractError("unsupported contract_version")
        if self.reason not in REASONS:
            raise ContractError(f"unknown reason {self.reason!r}")
        s(self, "metadata", _freeze(self.metadata))
        if not isinstance(self.metadata, Mapping):
            raise ContractError("metadata must be a mapping")
        if (st is ObservationStatus.OBSERVED) != bool(self.outcome_ids):
            raise ContractError("outcome_ids are present exactly when OBSERVED")
        if rel is not Relation.NOT_COMPARABLE and st is not ObservationStatus.OBSERVED:
            raise ContractError("a relation exists only for OBSERVED")
        if self.comparison_id != make_comparison_id(self.prediction_id, st.value, self.outcome_ids, self.evaluated_at.isoformat()):
            raise ContractError("comparison_id does not match content")

    def to_dict(self) -> dict:
        return {"contract_version": self.contract_version, "comparison_id": self.comparison_id,
                "prediction_id": self.prediction_id, "decision_id": self.decision_id, "chat_id": self.chat_id,
                "status": self.status.value, "relation": self.relation.value, "outcome_ids": list(self.outcome_ids),
                "observed_outcome_types": list(self.observed_outcome_types), "evaluated_at": self.evaluated_at.isoformat(),
                "reason": self.reason, "metadata": _thaw(self.metadata)}

    @classmethod
    def from_dict(cls, d: Mapping) -> "PredictionComparison":
        keys = ("contract_version", "comparison_id", "prediction_id", "decision_id", "chat_id", "status", "relation",
                "outcome_ids", "observed_outcome_types", "evaluated_at", "reason", "metadata")
        if not isinstance(d, Mapping) or set(d) != set(keys):
            raise ContractError("comparison: exact keys required")
        for k in ("outcome_ids", "observed_outcome_types"):
            if not isinstance(d[k], (list, tuple)):
                raise ContractError(f"{k} must be a list")
        return cls(**{k: d[k] for k in keys})


def make_comparison_id(prediction_id, status, outcome_ids, evaluated_at_iso) -> str:
    return _id("pc_", prediction_id, status, list(outcome_ids), evaluated_at_iso)
