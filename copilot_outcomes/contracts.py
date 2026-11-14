"""Outcome contract (outcome.v1): an OBSERVED event that happened after production acted.

An Outcome is an observation, never an inference: LLM output is not an Outcome source. It carries no
attribution and no causal field; how it relates to a decision is decided by the joiner and expressed only
as ATTR_EXPLICIT / ATTR_FALLBACK (observational, not causal).

  decision_id  optional exact link ("pd_..." Ledger id, shape-checked only). None = not linked.
  confidence   float in [0, 1] or None when the source gives none (never invented, never adjusted).
  outcome_id   supplied by the caller, never generated here.
Distinct on purpose: REGISTRATION != DEPOSIT, PAYMENT_ATTEMPT != PAYMENT_SUCCESS.
Immutable and JSON-serialisable (to_dict/from_dict, strict, no pickle). Stdlib + copilot_evidence.contracts helpers.
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from copilot_evidence.contracts import ContractError, _dt, _enum, _freeze, _int, _thaw, _unit

OUTCOME_CONTRACT_VERSION = "outcome.v1"
DEFAULT_OUTCOME_WINDOW_SECONDS = 86400          # contract-level default (24h); the joiner never hardcodes one
DECISION_ID_PREFIX = "pd_"                      # shape of Production Decision Ledger ids

ATTR_EXPLICIT = "explicitly_linked"             # outcome carries the exact decision_id
ATTR_FALLBACK = "temporal_chat_fallback"        # same chat, observed after, inside the window: NOT causal


class OutcomeType(str, Enum):
    DEPOSIT = "DEPOSIT"
    REGISTRATION = "REGISTRATION"
    PAYMENT_ATTEMPT = "PAYMENT_ATTEMPT"
    PAYMENT_SUCCESS = "PAYMENT_SUCCESS"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"
    DROPPED = "DROPPED"
    CONTINUED = "CONTINUED"
    UNKNOWN = "UNKNOWN"


class OutcomeSource(str, Enum):
    POSTBACK = "POSTBACK"
    TELEGRAM_EVENT = "TELEGRAM_EVENT"
    PRODUCTION = "PRODUCTION"
    SYSTEM = "SYSTEM"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Outcome:
    outcome_id: str
    decision_id: str | None
    chat_id: int
    observed_at: datetime
    outcome_type: OutcomeType
    value: Any
    confidence: float | None
    source: OutcomeSource
    metadata: Mapping[str, Any]
    contract_version: str = OUTCOME_CONTRACT_VERSION

    def __post_init__(self):
        s = object.__setattr__
        if not isinstance(self.outcome_id, str) or not self.outcome_id.strip():
            raise ContractError("outcome_id must be a non-empty str")
        d = self.decision_id
        if d is not None and not (isinstance(d, str) and d.startswith(DECISION_ID_PREFIX)
                                  and len(d) > len(DECISION_ID_PREFIX)):
            raise ContractError("decision_id must be None or a Ledger id ('pd_...')")
        s(self, "chat_id", _int("chat_id", self.chat_id))
        s(self, "observed_at", _dt("observed_at", self.observed_at))
        s(self, "outcome_type", _enum(OutcomeType, "outcome_type", self.outcome_type))
        s(self, "value", _freeze(self.value))
        s(self, "confidence", _unit("confidence", self.confidence))
        s(self, "source", _enum(OutcomeSource, "source", self.source))
        if self.metadata is None:
            raise ContractError("metadata must be a mapping")
        meta = _freeze(self.metadata)
        if not isinstance(meta, Mapping):
            raise ContractError("metadata must be a mapping")
        s(self, "metadata", meta)
        if self.contract_version != OUTCOME_CONTRACT_VERSION:
            raise ContractError(f"unsupported contract_version: {self.contract_version!r}")

    def to_dict(self) -> dict:
        return {
            "contract_version": self.contract_version, "outcome_id": self.outcome_id,
            "decision_id": self.decision_id, "chat_id": self.chat_id,
            "observed_at": self.observed_at.isoformat(), "outcome_type": self.outcome_type.value,
            "value": _thaw(self.value), "confidence": self.confidence, "source": self.source.value,
            "metadata": _thaw(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "Outcome":
        if not isinstance(d, Mapping):
            raise ContractError("outcome must be a mapping")
        keys = ("contract_version", "outcome_id", "decision_id", "chat_id", "observed_at", "outcome_type",
                "value", "confidence", "source", "metadata")
        missing = [k for k in keys if k not in d]
        extra = sorted(set(d) - set(keys))
        if missing or extra:
            raise ContractError(f"outcome: missing={missing} unknown={extra}")
        return cls(outcome_id=d["outcome_id"], decision_id=d["decision_id"], chat_id=d["chat_id"],
                   observed_at=d["observed_at"], outcome_type=d["outcome_type"], value=d["value"],
                   confidence=d["confidence"], source=d["source"], metadata=d["metadata"],
                   contract_version=d["contract_version"])
