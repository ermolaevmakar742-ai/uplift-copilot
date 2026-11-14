"""Psychology Engine: Signals -> PsychologicalAssessment. HARD-only, pure and deterministic (no clock, no randomness, no I/O, no LLM,
no network, no main.py). It returns a STRATEGY, never reply text. Phase 1 consumers only observe the result (shadow)."""
from copilot_psychology.classifier import Classification, classify
from copilot_psychology.contracts import (ENGINE_VERSION, STRATEGY_VERSION, ContractError, PsychologicalAssessment, Signals, State, stable_hash)
from copilot_psychology.registry import REGISTRY, KnowledgeRegistry
from copilot_psychology.safety import check_assessment, constrain, flags_for, vulnerable
from copilot_psychology.strategy import ALWAYS_AVOID, plan_for

ACTIVE_REGIME = "hard"


def is_active(regime) -> bool:
    """HARD only (the existing production regime value). Every other regime, and an unknown one, is not active."""
    return isinstance(regime, str) and regime.strip().lower() == ACTIVE_REGIME


def _why(c: Classification, state: State) -> tuple[str, ...]:
    if c.uncertainty.value == "insufficient_data":
        return (f"insufficient_data: {c.usable} usable signal(s), no informative cue",)
    if c.conflicting:
        return (f"conflicting signals among cues {list(c.cues)}: no mechanism is asserted",)
    word = "likely" if c.uncertainty.value == "likely" else "possible"
    return (f"{word} {state.value} based on cues {list(c.cues)}",)


def assess(signals: Signals, *, regime, registry: KnowledgeRegistry = REGISTRY) -> PsychologicalAssessment | None:
    """None when the regime is not HARD (the engine is not active). Otherwise a validated assessment."""
    if not is_active(regime):
        return None
    c = classify(signals)
    state, mechanisms, rec, alt = plan_for(c.resistance)
    rec, alt = constrain(signals, c, rec, alt)
    asserted = state not in (State.UNKNOWN, State.INSUFFICIENT_DATA) and not vulnerable(signals)
    if not asserted:
        mechanisms = ()
    cards = registry.for_resistance(c.resistance.value, rec) if asserted else ()
    flags = flags_for(signals, c, sensitive_dropped=False)
    snapshot = {k: v for k, v in signals.to_dict().items() if v is not None}
    trust = signals.trust_signal if isinstance(signals.trust_signal, (int, float)) and not isinstance(signals.trust_signal, bool) else None
    a = PsychologicalAssessment(
        assessment_id=stable_hash([snapshot, registry.fingerprint(), ENGINE_VERSION, STRATEGY_VERSION, ACTIVE_REGIME], "pa_"),
        client_state=snapshot, detected_mechanisms=tuple(mechanisms), relevant_principles=tuple(x.key for x in cards),
        psychological_state=state, uncertainty=c.uncertainty, trust_signal=trust, resistance_type=c.resistance,
        recommended_strategy=rec, alternative_strategy=alt, avoid_strategies=ALWAYS_AVOID, confidence=c.confidence,
        evidence_level=registry.weakest_level(cards), knowledge_version=registry.version, knowledge_fingerprint=registry.fingerprint(),
        engine_version=ENGINE_VERSION, strategy_version=STRATEGY_VERSION, safety_flags=flags, why=_why(c, state))
    check_assessment(a)
    return a
