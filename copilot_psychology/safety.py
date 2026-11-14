"""Safety layer: the last step before an assessment leaves the engine.
  * vulnerability signals (minor / self-exclusion / financial distress) => persuasion suppressed: SLOW_DOWN, nothing that pushes
  * low confidence / conflicting signals => no mechanism is asserted, only a diagnostic or neutral strategy
  * wording must stay hedged: absolute claims about the client are rejected
  * prohibited tactics can never appear as a recommendation; they are always listed in avoid_strategies
  * sensitive personal characteristics (region, etc.) are not inputs at all"""
import re

from copilot_psychology.classifier import Classification
from copilot_psychology.contracts import Avoid, ContractError, Resistance, SafetyFlag, Signals, State, Strategy, Uncertainty

SAFE_UNDER_UNCERTAINTY = {Strategy.ASK_DIAGNOSTIC_QUESTION, Strategy.CONTINUE_NORMAL_FLOW, Strategy.CLARIFY, Strategy.SLOW_DOWN}
ABSOLUTE = re.compile(r"\b(definitely|certainly|surely|always|never|obviously|for sure|is afraid|точно|определённо|определенно|наверняка|боится)\b", re.I)
# strategy labels that would be prohibited tactics if they ever existed in the enum: the enum must stay free of them
PROHIBITED_STRATEGY_WORDS = ("urgency", "scarcity", "social_proof", "guilt", "pressure", "fake", "fabricat", "false")


def vulnerable(s: Signals) -> bool:
    return any(v is True for v in (s.risk_minor, s.risk_self_exclusion, s.financial_distress))


def flags_for(s: Signals, c: Classification, *, sensitive_dropped: bool) -> tuple[SafetyFlag, ...]:
    out = []
    if vulnerable(s):
        out += [SafetyFlag.VULNERABLE_PERSON_SIGNAL, SafetyFlag.PERSUASION_SUPPRESSED]
    if c.conflicting:
        out.append(SafetyFlag.CONFLICTING_SIGNALS)
    if c.uncertainty is Uncertainty.INSUFFICIENT_DATA or (c.confidence is not None and c.confidence < 0.4):
        out.append(SafetyFlag.LOW_CONFIDENCE)
    if c.reactance_risk:
        out.append(SafetyFlag.REACTANCE_RISK)
    if sensitive_dropped:
        out.append(SafetyFlag.SENSITIVE_INPUT_IGNORED)
    return tuple(sorted(set(out), key=lambda f: f.value))


def constrain(s: Signals, c: Classification, rec: Strategy, alt: Strategy | None):
    """-> (recommended, alternative). Applied after the plan table."""
    if vulnerable(s):
        return Strategy.SLOW_DOWN, Strategy.CONTINUE_NORMAL_FLOW
    if c.reactance_risk and rec not in (Strategy.SLOW_DOWN,):
        alt = Strategy.SLOW_DOWN
    if c.conflicting or c.uncertainty is Uncertainty.INSUFFICIENT_DATA:
        if rec not in SAFE_UNDER_UNCERTAINTY:
            rec = Strategy.ASK_DIAGNOSTIC_QUESTION
        if alt is not None and alt not in SAFE_UNDER_UNCERTAINTY:
            alt = Strategy.CONTINUE_NORMAL_FLOW
    return rec, alt


def check_wording(lines) -> None:
    for line in lines:
        if ABSOLUTE.search(line):
            raise ContractError(f"unhedged wording: {line!r}")


def check_assessment(a) -> None:
    """Raises ContractError on any violated safety invariant (used by the engine and by tests)."""
    check_wording(a.why)
    for st in (a.recommended_strategy, a.alternative_strategy):
        if st is not None and any(w in st.value.lower() for w in PROHIBITED_STRATEGY_WORDS):
            raise ContractError(f"prohibited strategy {st}")
    if set(Avoid) - set(a.avoid_strategies):
        raise ContractError("avoid_strategies must always list every prohibited tactic")
    if a.psychological_state in (State.UNKNOWN, State.INSUFFICIENT_DATA) and a.detected_mechanisms:
        raise ContractError("no mechanism may be asserted for an unknown / insufficient state")
    if a.resistance_type in (Resistance.CONFLICTING, Resistance.UNKNOWN) and a.recommended_strategy not in SAFE_UNDER_UNCERTAINTY:
        raise ContractError("uncertain state needs a diagnostic or neutral strategy")
    if SafetyFlag.VULNERABLE_PERSON_SIGNAL in a.safety_flags and a.recommended_strategy is not Strategy.SLOW_DOWN:
        raise ContractError("vulnerable-person signal requires SLOW_DOWN")
    if a.confidence is not None and a.confidence > 0.8:
        raise ContractError("rule-based confidence is capped")
