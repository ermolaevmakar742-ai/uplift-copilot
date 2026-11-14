"""Canonical Evidence Contract (evidence.v1).

Immutable, JSON-serialisable, validated at construction. No I/O, no persistence,
no fusion math. Dependency-light: stdlib + copilot_contracts.stages only.

confidence  = how sure the SOURCE is that the observation reflects this signal.
reliability = how far we trust the source/method itself.
Both are plain floats in [0.0, 1.0], or None when the source provides no numeric
value (adapters never invent one); they are never combined here.

Provenance is mandatory. source_id may be None (explicit unknown); it is never
auto-generated. evidence_id is supplied by the caller, never invented here.
Do not put PII (names, phones, emails, message text) in value/metadata.
"""
import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from copilot_contracts.stages import Stage

EVIDENCE_CONTRACT_VERSION = "evidence.v1"


class EvidenceSource(str, Enum):
    PRODUCTION = "production"
    CLIENT_STATE = "client_state"
    SHADOW = "shadow"
    EXTERNAL = "external"      # evidence supplied by the deployment itself (CRM exports, partner feeds) — never scraped


class EvidenceType(str, Enum):
    STAGE = "STAGE"
    INTENT = "INTENT"
    SENTIMENT = "SENTIMENT"
    MOMENTUM = "MOMENTUM"
    CONTRADICTION = "CONTRADICTION"
    BLOCKER = "BLOCKER"
    PAYMENT_SIGNAL = "PAYMENT_SIGNAL"
    TRUST_SIGNAL = "TRUST_SIGNAL"
    EXTERNAL_SIGNAL = "EXTERNAL_SIGNAL"
    CLIENT_FACT = "CLIENT_FACT"
    ACTION = "ACTION"   # what the SYSTEM did; never a fact about the client
    UNKNOWN = "UNKNOWN"


class ContractError(ValueError):
    """Evidence/provenance failed validation."""


def _freeze(v: Any) -> Any:
    if isinstance(v, Mapping):
        out = {}
        for k, x in v.items():
            if not isinstance(k, str):
                raise ContractError("mapping keys must be str")
            out[k] = _freeze(x)
        return MappingProxyType(out)
    if isinstance(v, (list, tuple)):
        return tuple(_freeze(x) for x in v)
    if v is None or isinstance(v, (str, bool, int)):
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ContractError("non-finite float not allowed")
        return v
    raise ContractError(f"non JSON-compatible value: {type(v).__name__}")


def _thaw(v: Any) -> Any:
    if isinstance(v, Mapping):
        return {k: _thaw(x) for k, x in v.items()}
    if isinstance(v, tuple):
        return [_thaw(x) for x in v]
    return v


def _unit(name: str, v: Any) -> float | None:
    if v is None:
        return None     # explicit "source provides no value"; never a made-up default
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ContractError(f"{name} must be a number")
    f = float(v)
    if not math.isfinite(f) or not 0.0 <= f <= 1.0:
        raise ContractError(f"{name} must be within [0.0, 1.0]")
    return f


def _enum(cls, name: str, v: Any):
    if isinstance(v, cls):
        return v
    try:
        return cls(v)
    except (ValueError, TypeError):
        raise ContractError(f"invalid {name}: {v!r}") from None


def _dt(name: str, v: Any, optional: bool = False) -> datetime | None:
    if v is None and optional:
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            raise ContractError(f"{name} is not an ISO datetime") from None
    if not isinstance(v, datetime) or v.tzinfo is None:
        raise ContractError(f"{name} must be a timezone-aware datetime")
    return v


def _int(name: str, v: Any, optional: bool = False) -> int | None:
    if v is None and optional:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ContractError(f"{name} must be an int")
    return v


@dataclass(frozen=True)
class Provenance:
    source: EvidenceSource
    source_id: str | None
    contract_version: str = EVIDENCE_CONTRACT_VERSION

    def __post_init__(self):
        object.__setattr__(self, "source", _enum(EvidenceSource, "provenance.source", self.source))
        if self.source_id is not None and (not isinstance(self.source_id, str) or not self.source_id.strip()):
            raise ContractError("source_id must be a non-empty str or None")
        if self.contract_version != EVIDENCE_CONTRACT_VERSION:
            raise ContractError(f"unsupported contract_version: {self.contract_version!r}")

    def to_dict(self) -> dict:
        return {"source": self.source.value, "source_id": self.source_id,
                "contract_version": self.contract_version}

    @classmethod
    def from_dict(cls, d: Mapping) -> "Provenance":
        if not isinstance(d, Mapping):
            raise ContractError("provenance must be a mapping")
        for k in ("source", "source_id", "contract_version"):
            if k not in d:
                raise ContractError(f"provenance missing {k}")
        return cls(source=d["source"], source_id=d["source_id"], contract_version=d["contract_version"])


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    chat_id: int
    message_id: int | None
    created_at: datetime
    source: EvidenceSource
    evidence_type: EvidenceType
    value: Any
    confidence: float | None
    reliability: float | None
    observed_at: datetime | None
    stage: Stage
    provenance: Provenance
    metadata: Mapping[str, Any]
    contract_version: str = EVIDENCE_CONTRACT_VERSION

    def __post_init__(self):
        s = object.__setattr__
        if not isinstance(self.evidence_id, str) or not self.evidence_id.strip():
            raise ContractError("evidence_id must be a non-empty str")
        s(self, "chat_id", _int("chat_id", self.chat_id))
        s(self, "message_id", _int("message_id", self.message_id, optional=True))
        s(self, "created_at", _dt("created_at", self.created_at))
        s(self, "source", _enum(EvidenceSource, "source", self.source))
        s(self, "evidence_type", _enum(EvidenceType, "evidence_type", self.evidence_type))
        s(self, "value", _freeze(self.value))
        s(self, "confidence", _unit("confidence", self.confidence))
        s(self, "reliability", _unit("reliability", self.reliability))
        s(self, "observed_at", _dt("observed_at", self.observed_at, optional=True))
        s(self, "stage", _enum(Stage, "stage", self.stage))
        if not isinstance(self.provenance, Provenance):
            raise ContractError("provenance is required")
        if self.provenance.source is not self.source:
            raise ContractError("provenance.source must equal source")
        if self.metadata is None:
            raise ContractError("metadata must be a mapping")
        meta = _freeze(self.metadata)
        if not isinstance(meta, Mapping):
            raise ContractError("metadata must be a mapping")
        s(self, "metadata", meta)
        if self.contract_version != EVIDENCE_CONTRACT_VERSION:
            raise ContractError(f"unsupported contract_version: {self.contract_version!r}")

    def to_dict(self) -> dict:
        return {
            "contract_version": self.contract_version,
            "evidence_id": self.evidence_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "created_at": self.created_at.isoformat(),
            "source": self.source.value,
            "evidence_type": self.evidence_type.value,
            "value": _thaw(self.value),
            "confidence": self.confidence,
            "reliability": self.reliability,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "stage": self.stage.value,
            "provenance": self.provenance.to_dict(),
            "metadata": _thaw(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "Evidence":
        if not isinstance(d, Mapping):
            raise ContractError("evidence must be a mapping")
        keys = ("contract_version", "evidence_id", "chat_id", "message_id", "created_at", "source",
                "evidence_type", "value", "confidence", "reliability", "observed_at", "stage",
                "provenance", "metadata")
        missing = [k for k in keys if k not in d]
        if missing:
            raise ContractError(f"missing fields: {missing}")
        extra = set(d) - set(keys)
        if extra:
            raise ContractError(f"unknown fields: {sorted(extra)}")
        return cls(
            evidence_id=d["evidence_id"], chat_id=d["chat_id"], message_id=d["message_id"],
            created_at=d["created_at"], source=d["source"], evidence_type=d["evidence_type"],
            value=d["value"], confidence=d["confidence"], reliability=d["reliability"],
            observed_at=d["observed_at"], stage=d["stage"],
            provenance=Provenance.from_dict(d["provenance"]), metadata=d["metadata"],
            contract_version=d["contract_version"],
        )
