"""Attribution v1 contract. Pure: stdlib + the existing pure contracts of copilot_outcomes / copilot_evidence.

AttributionResult describes how ONE observed Outcome relates to the production decisions known for its chat.
It is a separate analytical object: it never changes the Outcome, the Decision, Client State, stages or replies.

Relationship (closed set)
  ATTRIBUTED      observed strictly AFTER a decision and linked by DIRECT (explicit decision_id) or TEMPORAL rules
  NOT_ATTRIBUTED  no eligible decision (none in chat, outside the window, decision interrupted)
  PRE_DECISION    observed before, or exactly at, the decision time (never attributable)
  AMBIGUOUS       several decisions are equally eligible: NO winner is chosen (candidates are listed)
  INELIGIBLE      structurally not attributable (unsupported/direct-only outcome type, chat mismatch, before reply done)
  UNKNOWN         cannot be decided from what is known (window unknown for this outcome type, explicit id unverifiable)
Method: DIRECT | TEMPORAL | NONE. Invariant: method != NONE  <=>  relationship == ATTRIBUTED.
Confidence is a typed category, never a score: DIRECT -> HIGH, TEMPORAL -> LOW, everything else -> NONE.
  (It is about the LINK only; the Outcome's own source confidence is neither read nor combined.)
`reason` is a code from a closed vocabulary, valid only for its relationship. All datetimes are timezone-aware;
a naive datetime is a ContractError (never silently converted). attribution_id is a deterministic content hash.
Immutable, strict to_dict/from_dict, no pickle.
"""
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from copilot_evidence.contracts import ContractError, _dt, _enum, _freeze, _int, _thaw
from copilot_outcomes.contracts import OutcomeType

CONTRACT_VERSION = "attribution.v1"
CONTRACT_VERSION_V2 = "attribution.v2"      # temporal window starts at the SENT response boundary (phase 5G)
SUPPORTED_VERSIONS = (CONTRACT_VERSION, CONTRACT_VERSION_V2)


class Relationship(str, Enum):
    ATTRIBUTED = "ATTRIBUTED"
    NOT_ATTRIBUTED = "NOT_ATTRIBUTED"
    PRE_DECISION = "PRE_DECISION"
    AMBIGUOUS = "AMBIGUOUS"
    INELIGIBLE = "INELIGIBLE"
    UNKNOWN = "UNKNOWN"


class Method(str, Enum):
    DIRECT = "DIRECT"
    TEMPORAL = "TEMPORAL"
    NONE = "NONE"


class AttributionConfidence(str, Enum):
    HIGH = "HIGH"        # DIRECT: exact decision_id verified
    LOW = "LOW"          # TEMPORAL: unique eligible decision by time only
    NONE = "NONE"        # no attribution


class InterruptionKind(str, Enum):
    """Intervening bot actions that exist as timestamped events today (ai_activity). Manual mode has no event."""
    FOLLOWUP = "FOLLOWUP"
    FIXED_TRIGGER = "FIXED_TRIGGER"
    INSULT_BLOCK = "INSULT_BLOCK"


class PolicyKind(str, Enum):
    WINDOW = "WINDOW"            # temporal attribution allowed within window_seconds
    UNKNOWN = "UNKNOWN"          # window cannot be determined from the audit: temporal -> UNKNOWN (no guess)
    DIRECT_ONLY = "DIRECT_ONLY"  # never temporal; only an explicit decision_id can attribute


REASONS: Mapping[Relationship, frozenset] = MappingProxyType({
    Relationship.ATTRIBUTED: frozenset({"explicit_decision_id_verified", "temporal_unique_eligible_decision"}),
    Relationship.NOT_ATTRIBUTED: frozenset({"no_decision_in_chat", "outside_window", "decision_interrupted"}),
    Relationship.PRE_DECISION: frozenset({"observed_before_decision", "observed_at_decision_time"}),
    Relationship.AMBIGUOUS: frozenset({"multiple_eligible_decisions"}),
    Relationship.INELIGIBLE: frozenset({"outcome_type_not_supported", "outcome_type_not_temporally_attributable",
                                        "explicit_decision_chat_mismatch", "before_reply_completed"}),
    Relationship.UNKNOWN: frozenset({"window_unknown_for_outcome_type", "explicit_decision_unknown"}),
})


# v2 adds one reason: temporal attribution is unavailable when a candidate decision has no SENT response boundary.
REASONS_V2: Mapping[Relationship, frozenset] = MappingProxyType({
    rel: (frozenset(r | {"no_sent_response_boundary"}) if rel is Relationship.UNKNOWN else r)
    for rel, r in REASONS.items()})
REASONS_BY_VERSION = MappingProxyType({CONTRACT_VERSION: REASONS, CONTRACT_VERSION_V2: REASONS_V2})


@dataclass(frozen=True)
class DecisionRef:
    """What attribution needs to know about a production decision."""
    decision_id: str
    chat_id: int
    created_at: datetime
    reply_completed_at: datetime | None = None    # not available in production today (audit S1); None = unknown

    def __post_init__(self):
        if not (isinstance(self.decision_id, str) and self.decision_id.strip()):
            raise ContractError("decision_id must be a non-empty str")
        object.__setattr__(self, "chat_id", _int("chat_id", self.chat_id))
        object.__setattr__(self, "created_at", _dt("created_at", self.created_at))
        rc = _dt("reply_completed_at", self.reply_completed_at, optional=True)
        if rc is not None and rc < self.created_at:
            raise ContractError("reply_completed_at must not precede created_at")
        object.__setattr__(self, "reply_completed_at", rc)

    @classmethod
    def of(cls, d: Any) -> "DecisionRef":
        if isinstance(d, DecisionRef):
            return d
        get = d.get if isinstance(d, Mapping) else lambda k: getattr(d, k, None)
        return cls(get("decision_id"), get("chat_id"), get("created_at"), get("reply_completed_at"))


@dataclass(frozen=True)
class Interruption:
    kind: InterruptionKind
    chat_id: int
    observed_at: datetime
    source_decision_id: str | None = None      # set when the interruption IS a recorded decision: it never interrupts itself

    def __post_init__(self):
        if self.source_decision_id is not None and not (isinstance(self.source_decision_id, str) and self.source_decision_id):
            raise ContractError("source_decision_id must be a non-empty str or None")
        object.__setattr__(self, "kind", _enum(InterruptionKind, "kind", self.kind))
        object.__setattr__(self, "chat_id", _int("chat_id", self.chat_id))
        object.__setattr__(self, "observed_at", _dt("observed_at", self.observed_at))


@dataclass(frozen=True)
class OutcomePolicy:
    kind: PolicyKind
    window_seconds: int | float | None
    reason: str                     # why this window / why unknown / why direct-only (documented, never empty)

    def __post_init__(self):
        object.__setattr__(self, "kind", _enum(PolicyKind, "kind", self.kind))
        if not (isinstance(self.reason, str) and self.reason.strip()):
            raise ContractError("policy reason is required")
        w = self.window_seconds
        if self.kind is PolicyKind.WINDOW:
            if isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w) or w <= 0:
                raise ContractError("WINDOW policy needs a finite window_seconds > 0")
        elif w is not None:
            raise ContractError(f"{self.kind.value} policy must not carry a window")


@dataclass(frozen=True)
class AttributionPolicy:
    """Explicit per-outcome-type rules. A type that is absent is INELIGIBLE (never an implicit default window)."""
    outcome_policies: Mapping[OutcomeType, OutcomePolicy]

    def __post_init__(self):
        items = {}
        for k, v in dict(self.outcome_policies).items():
            if not isinstance(v, OutcomePolicy):
                raise ContractError("policy values must be OutcomePolicy")
            items[_enum(OutcomeType, "outcome_type", k)] = v
        object.__setattr__(self, "outcome_policies", MappingProxyType(items))


def compute_id(decision_id, outcome_id, relationship, method, candidates, version=CONTRACT_VERSION) -> str:
    raw = json.dumps([version, decision_id, outcome_id, relationship, method, candidates],
                     sort_keys=True, separators=(",", ":"))
    return "at_" + hashlib.sha256(raw.encode()).hexdigest()[:20]


@dataclass(frozen=True)
class AttributionResult:
    attribution_id: str
    decision_id: str | None
    outcome_id: str
    relationship: Relationship
    method: Method
    confidence: AttributionConfidence
    observed_at: datetime                       # of the outcome (unchanged)
    decision_created_at: datetime | None        # of the attributed decision (None unless ATTRIBUTED)
    reason: str
    metadata: Mapping[str, Any]
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self):
        s = object.__setattr__
        if not (isinstance(self.outcome_id, str) and self.outcome_id.strip()):
            raise ContractError("outcome_id must be a non-empty str")
        rel = _enum(Relationship, "relationship", self.relationship)
        meth = _enum(Method, "method", self.method)
        conf = _enum(AttributionConfidence, "confidence", self.confidence)
        s(self, "relationship", rel), s(self, "method", meth), s(self, "confidence", conf)
        s(self, "observed_at", _dt("observed_at", self.observed_at))
        s(self, "decision_created_at", _dt("decision_created_at", self.decision_created_at, optional=True))
        if self.contract_version not in SUPPORTED_VERSIONS:
            raise ContractError(f"unsupported contract_version: {self.contract_version!r}")
        if self.reason not in REASONS_BY_VERSION[self.contract_version][rel]:
            raise ContractError(f"reason {self.reason!r} is not valid for {rel.value}")
        meta = _freeze(self.metadata if self.metadata is not None else None)
        if not isinstance(meta, Mapping):
            raise ContractError("metadata must be a mapping")
        s(self, "metadata", meta)
        attributed = rel is Relationship.ATTRIBUTED
        if attributed != (meth is not Method.NONE):
            raise ContractError("method must be DIRECT/TEMPORAL exactly when ATTRIBUTED")
        want = {Method.DIRECT: AttributionConfidence.HIGH, Method.TEMPORAL: AttributionConfidence.LOW,
                Method.NONE: AttributionConfidence.NONE}[meth]
        if conf is not want:
            raise ContractError(f"confidence must be {want.value} for method {meth.value}")
        if attributed:
            if not (isinstance(self.decision_id, str) and self.decision_id.strip()) or self.decision_created_at is None:
                raise ContractError("ATTRIBUTED requires decision_id and decision_created_at")
            if not self.observed_at > self.decision_created_at:
                raise ContractError("an outcome observed at or before the decision can never be ATTRIBUTED")
            if (meth is Method.DIRECT) != (self.reason == "explicit_decision_id_verified"):
                raise ContractError("reason does not match method")
        elif self.decision_id is not None or self.decision_created_at is not None:
            raise ContractError("only ATTRIBUTED results carry decision_id / decision_created_at")
        cands = meta.get("candidate_decision_ids")
        if rel is Relationship.AMBIGUOUS:
            if not (isinstance(cands, tuple) and len(cands) >= 2 and len(set(cands)) == len(cands)
                    and list(cands) == sorted(cands) and all(isinstance(c, str) for c in cands)):
                raise ContractError("AMBIGUOUS requires >=2 sorted unique metadata.candidate_decision_ids")
        elif cands is not None:
            raise ContractError("candidate_decision_ids only belongs to AMBIGUOUS")
        expected = compute_id(self.decision_id, self.outcome_id, rel.value, meth.value,
                              list(cands) if cands is not None else None, self.contract_version)
        if self.attribution_id != expected:
            raise ContractError("attribution_id does not match content")

    def to_dict(self) -> dict:
        return {
            "contract_version": self.contract_version, "attribution_id": self.attribution_id,
            "decision_id": self.decision_id, "outcome_id": self.outcome_id,
            "relationship": self.relationship.value, "method": self.method.value,
            "confidence": self.confidence.value, "observed_at": self.observed_at.isoformat(),
            "decision_created_at": self.decision_created_at.isoformat() if self.decision_created_at else None,
            "reason": self.reason, "metadata": _thaw(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "AttributionResult":
        if not isinstance(d, Mapping):
            raise ContractError("attribution result must be a mapping")
        keys = ("contract_version", "attribution_id", "decision_id", "outcome_id", "relationship", "method",
                "confidence", "observed_at", "decision_created_at", "reason", "metadata")
        missing, extra = [k for k in keys if k not in d], sorted(set(d) - set(keys))
        if missing or extra:
            raise ContractError(f"attribution result: missing={missing} unknown={extra}")
        return cls(attribution_id=d["attribution_id"], decision_id=d["decision_id"], outcome_id=d["outcome_id"],
                   relationship=d["relationship"], method=d["method"], confidence=d["confidence"],
                   observed_at=d["observed_at"], decision_created_at=d["decision_created_at"], reason=d["reason"],
                   metadata=d["metadata"], contract_version=d["contract_version"])


def make_result(outcome_id: str, relationship: Relationship, method: Method, reason: str, *, observed_at: datetime,
                decision: DecisionRef | None = None, metadata: Mapping | None = None,
                contract_version: str = CONTRACT_VERSION) -> AttributionResult:
    meta = dict(metadata or {})
    cands = meta.get("candidate_decision_ids")
    conf = {Method.DIRECT: AttributionConfidence.HIGH, Method.TEMPORAL: AttributionConfidence.LOW,
            Method.NONE: AttributionConfidence.NONE}[method]
    did = decision.decision_id if decision else None
    return AttributionResult(
        attribution_id=compute_id(did, outcome_id, relationship.value, method.value,
                                  list(cands) if cands is not None else None, contract_version),
        decision_id=did, outcome_id=outcome_id, relationship=relationship, method=method, confidence=conf,
        observed_at=observed_at, decision_created_at=decision.created_at if decision else None, reason=reason,
        metadata=meta, contract_version=contract_version)
