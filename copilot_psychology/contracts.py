"""Contracts: versions, vocabularies, KnowledgeCard, Signals, PsychologicalAssessment. Frozen, strict to_dict/from_dict (unknown keys rejected)."""
import hashlib
import json
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any

KNOWLEDGE_VERSION = "psychology.v1"
ENGINE_VERSION = "psychology-engine.v1"
STRATEGY_VERSION = "strategy.v1"
CONTRACT_VERSION = "psychology-assessment.v1"


class ContractError(ValueError):
    pass


class EvidenceLevel(str, Enum):
    """How well a principle is supported. Ordered strongest -> weakest; an assessment is only as strong as its weakest principle."""
    ESTABLISHED = "established"      # replicated / meta-analysed in the stated population
    MIXED = "mixed"                  # conflicting or heterogeneous results, effect size contested
    THEORETICAL = "theoretical"      # coherent framework, limited direct empirical tests
    HYPOTHESIS = "hypothesis"        # untested or refuted in the relevant sense; never used as a fact

    @property
    def rank(self) -> int:
        return ("established", "mixed", "theoretical", "hypothesis").index(self.value)


class Domain(str, Enum):
    COGNITIVE = "cognitive_psychology"
    BEHAVIORAL_ECONOMICS = "behavioral_economics"
    PERSUASION = "persuasion"
    NEGOTIATION = "negotiation"
    CONSUMER = "consumer_decision_making"
    CONVERSATION = "conversation_analysis"
    TRUST = "trust_uncertainty"
    OBJECTION = "objection_handling"
    MANIPULATION = "manipulation_dark_psychology"


class Role(str, Enum):
    STRATEGY_INPUT = "strategy_input"          # may inform a recommended strategy
    RECOGNITION_ONLY = "recognition_only"      # used to recognise pressure patterns / risk, never to choose a tactic
    NOT_ADOPTED = "not_adopted"                # recorded so that it is NOT used (popular but unsupported claim)


class SourceType(str, Enum):
    META_ANALYSIS = "meta_analysis"
    PEER_REVIEWED = "peer_reviewed"
    REVIEW = "review"
    BOOK = "book"
    PRACTITIONER = "practitioner"


class Strategy(str, Enum):
    CLARIFY = "CLARIFY"
    REDUCE_UNCERTAINTY = "REDUCE_UNCERTAINTY"
    BUILD_TRUST = "BUILD_TRUST"
    ASK_DIAGNOSTIC_QUESTION = "ASK_DIAGNOSTIC_QUESTION"
    ADDRESS_OBJECTION = "ADDRESS_OBJECTION"
    SIMPLIFY_CHOICE = "SIMPLIFY_CHOICE"
    PROVIDE_EVIDENCE = "PROVIDE_EVIDENCE"
    REPAIR_TRUST = "REPAIR_TRUST"
    SLOW_DOWN = "SLOW_DOWN"
    CONTINUE_NORMAL_FLOW = "CONTINUE_NORMAL_FLOW"


class Avoid(str, Enum):
    """Tactics the engine never recommends; they are listed per assessment so that the response layer can see them."""
    FALSE_URGENCY = "false_urgency"
    FABRICATED_SCARCITY = "fabricated_scarcity"
    FABRICATED_SOCIAL_PROOF = "fabricated_social_proof"
    FALSE_CLAIMS = "false_claims"
    EXPLOITING_SENSITIVE_TRAITS = "exploiting_sensitive_traits"
    PRESSURE_AFTER_REFUSAL = "pressure_after_refusal"
    GUILT_TRIPPING = "guilt_tripping"


class State(str, Enum):
    INSUFFICIENT_DATA = "insufficient_data"
    UNKNOWN = "unknown"
    TRUST_DEFICIT = "trust_deficit"
    PURCHASE_UNCERTAINTY = "purchase_uncertainty"
    TECHNICAL_OBSTACLE = "technical_obstacle"
    TIMING_DEFERRAL = "timing_deferral"
    UNSPECIFIED_RESISTANCE = "unspecified_resistance"


class Resistance(str, Enum):
    NONE_DETECTED = "none_detected"
    TRUST_CONCERN = "trust_concern"
    PRICE_UNCERTAINTY = "price_uncertainty"
    PAYMENT_METHOD_UNCERTAINTY = "payment_method_uncertainty"
    TECHNICAL_ISSUE = "technical_issue"
    DEFERRAL = "deferral"
    UNSPECIFIED = "unspecified"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


class Uncertainty(str, Enum):
    INSUFFICIENT_DATA = "insufficient_data"
    CONFLICTING_SIGNALS = "conflicting_signals"
    POSSIBLE = "possible"
    LIKELY = "likely"


class SafetyFlag(str, Enum):
    VULNERABLE_PERSON_SIGNAL = "vulnerable_person_signal"
    CONFLICTING_SIGNALS = "conflicting_signals"
    LOW_CONFIDENCE = "low_confidence"
    PERSUASION_SUPPRESSED = "persuasion_suppressed"
    REACTANCE_RISK = "reactance_risk"
    SENSITIVE_INPUT_IGNORED = "sensitive_input_ignored"


def stable_hash(obj: Any, prefix: str, n: int = 24) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:n]


@dataclass(frozen=True)
class KnowledgeCard:
    principle_id: str
    version: int
    domain: Domain
    principle: str
    mechanism: str
    description: str
    applicability: tuple[str, ...]
    contraindications: tuple[str, ...]
    expected_effect: str
    candidate_strategies: tuple[Strategy, ...]
    evidence_level: EvidenceLevel
    population: str
    source: str
    source_type: SourceType
    limitations: tuple[str, ...]
    role: Role = Role.STRATEGY_INPUT

    @property
    def key(self) -> str:
        return f"{self.principle_id}@{self.version}"

    def to_dict(self) -> dict:
        return {"principle_id": self.principle_id, "version": self.version, "domain": self.domain.value, "principle": self.principle,
                "mechanism": self.mechanism, "description": self.description, "applicability": list(self.applicability),
                "contraindications": list(self.contraindications), "expected_effect": self.expected_effect,
                "candidate_strategies": [s.value for s in self.candidate_strategies], "evidence_level": self.evidence_level.value,
                "population": self.population, "source": self.source, "source_type": self.source_type.value,
                "limitations": list(self.limitations), "role": self.role.value}

    def card_hash(self) -> str:
        return stable_hash(self.to_dict(), "kc_", 16)

    def validate(self) -> None:
        for name in ("principle_id", "principle", "mechanism", "description", "expected_effect", "population", "source"):
            v = getattr(self, name)
            if not isinstance(v, str) or not v.strip():
                raise ContractError(f"{self.principle_id!r}: {name} must be a non-empty string")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise ContractError(f"{self.principle_id}: version must be an int >= 1")
        if not self.limitations or not all(isinstance(x, str) and x.strip() for x in self.limitations):
            raise ContractError(f"{self.key}: at least one limitation is required")
        if not self.applicability or not self.contraindications:
            raise ContractError(f"{self.key}: applicability and contraindications are required")
        if self.role is Role.STRATEGY_INPUT and not self.candidate_strategies:
            raise ContractError(f"{self.key}: a strategy_input card needs candidate_strategies")
        if self.role is not Role.STRATEGY_INPUT and self.candidate_strategies:
            raise ContractError(f"{self.key}: {self.role.value} cards must not propose strategies")
        if self.role is Role.STRATEGY_INPUT and self.evidence_level is EvidenceLevel.HYPOTHESIS:
            raise ContractError(f"{self.key}: a hypothesis can never be a strategy input")
        if self.domain is Domain.MANIPULATION and self.role is Role.STRATEGY_INPUT:
            raise ContractError(f"{self.key}: manipulation cards are recognition / risk only")


@dataclass(frozen=True)
class Signals:
    """ONLY already-structured signals. No raw text, no history, no region / personal characteristics. None = unknown (never guessed)."""
    stage: str | None = None              # canonical Stage value
    intent: str | None = None
    sentiment: float | None = None
    complexity: float | None = None
    messages_count: int | None = None
    trigger: str | None = None
    objection_signal: str | None = None
    blocker: str | None = None
    trust_signal: float | None = None     # 0..1 when a real trust signal exists
    payment_signal: str | None = None
    contradiction: bool | None = None
    momentum: str | None = None           # RISING / STABLE / FALLING
    risk_minor: bool | None = None
    risk_self_exclusion: bool | None = None
    financial_distress: bool | None = None

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class PsychologicalAssessment:
    assessment_id: str
    client_state: dict
    detected_mechanisms: tuple[str, ...]
    relevant_principles: tuple[str, ...]
    psychological_state: State
    uncertainty: Uncertainty
    trust_signal: float | None
    resistance_type: Resistance
    recommended_strategy: Strategy
    alternative_strategy: Strategy | None
    avoid_strategies: tuple[Avoid, ...]
    confidence: float | None
    evidence_level: EvidenceLevel | None
    knowledge_version: str
    knowledge_fingerprint: str
    engine_version: str
    strategy_version: str
    safety_flags: tuple[SafetyFlag, ...]
    why: tuple[str, ...]
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self):
        if self.confidence is not None and not (isinstance(self.confidence, (int, float)) and 0.0 <= self.confidence <= 1.0):
            raise ContractError("confidence must be in [0, 1] or None")
        if self.psychological_state in (State.INSUFFICIENT_DATA, State.UNKNOWN) and self.confidence is not None and self.confidence >= 0.6:
            raise ContractError("an unknown / insufficient state cannot carry high confidence")

    def to_dict(self) -> dict:
        return {"assessment_id": self.assessment_id, "client_state": dict(self.client_state), "detected_mechanisms": list(self.detected_mechanisms),
                "relevant_principles": list(self.relevant_principles), "psychological_state": self.psychological_state.value,
                "uncertainty": self.uncertainty.value, "trust_signal": self.trust_signal, "resistance_type": self.resistance_type.value,
                "recommended_strategy": self.recommended_strategy.value,
                "alternative_strategy": self.alternative_strategy.value if self.alternative_strategy else None,
                "avoid_strategies": [a.value for a in self.avoid_strategies], "confidence": self.confidence,
                "evidence_level": self.evidence_level.value if self.evidence_level else None, "knowledge_version": self.knowledge_version,
                "knowledge_fingerprint": self.knowledge_fingerprint, "engine_version": self.engine_version, "strategy_version": self.strategy_version,
                "safety_flags": [f.value for f in self.safety_flags], "why": list(self.why), "contract_version": self.contract_version}

    @classmethod
    def from_dict(cls, d: Any) -> "PsychologicalAssessment":
        if not isinstance(d, dict):
            raise ContractError("dict expected")
        want = {"assessment_id", "client_state", "detected_mechanisms", "relevant_principles", "psychological_state", "uncertainty", "trust_signal",
                "resistance_type", "recommended_strategy", "alternative_strategy", "avoid_strategies", "confidence", "evidence_level",
                "knowledge_version", "knowledge_fingerprint", "engine_version", "strategy_version", "safety_flags", "why", "contract_version"}
        if set(d) != want:
            raise ContractError(f"assessment keys mismatch: extra={sorted(set(d) - want)} missing={sorted(want - set(d))}")
        try:
            return cls(d["assessment_id"], dict(d["client_state"]), tuple(d["detected_mechanisms"]), tuple(d["relevant_principles"]),
                       State(d["psychological_state"]), Uncertainty(d["uncertainty"]), d["trust_signal"], Resistance(d["resistance_type"]),
                       Strategy(d["recommended_strategy"]), Strategy(d["alternative_strategy"]) if d["alternative_strategy"] else None,
                       tuple(Avoid(a) for a in d["avoid_strategies"]), d["confidence"],
                       EvidenceLevel(d["evidence_level"]) if d["evidence_level"] else None, d["knowledge_version"], d["knowledge_fingerprint"],
                       d["engine_version"], d["strategy_version"], tuple(SafetyFlag(f) for f in d["safety_flags"]), tuple(d["why"]), d["contract_version"])
        except (ValueError, TypeError, KeyError) as e:
            raise ContractError(str(e))
