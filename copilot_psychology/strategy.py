"""Strategy layer: resistance -> strategy labels (NOT reply text). The existing COPILOT response generation stays responsible for wording.
Selection rules (strategy.v1) are a pure table; knowledge cards supply the supporting principles and the evidence level, they do not choose."""
from copilot_psychology.contracts import Avoid, Resistance, State, Strategy

# resistance -> (state, mechanisms, recommended, alternative)
PLAN = {
    Resistance.TRUST_CONCERN: (State.TRUST_DEFICIT, ("trust_assessment", "perceived_risk"), Strategy.BUILD_TRUST, Strategy.PROVIDE_EVIDENCE),
    Resistance.PRICE_UNCERTAINTY: (State.PURCHASE_UNCERTAINTY, ("uncertainty_reduction",), Strategy.REDUCE_UNCERTAINTY, Strategy.SIMPLIFY_CHOICE),
    Resistance.PAYMENT_METHOD_UNCERTAINTY: (State.PURCHASE_UNCERTAINTY, ("uncertainty_reduction", "cognitive_load"), Strategy.CLARIFY, Strategy.SIMPLIFY_CHOICE),
    Resistance.TECHNICAL_ISSUE: (State.TECHNICAL_OBSTACLE, ("conversational_repair",), Strategy.CLARIFY, Strategy.ASK_DIAGNOSTIC_QUESTION),
    Resistance.DEFERRAL: (State.TIMING_DEFERRAL, ("reactance_risk",), Strategy.SLOW_DOWN, Strategy.CONTINUE_NORMAL_FLOW),
    Resistance.UNSPECIFIED: (State.UNSPECIFIED_RESISTANCE, ("resistance_unclassified",), Strategy.ASK_DIAGNOSTIC_QUESTION, Strategy.ADDRESS_OBJECTION),
    # no mechanism is asserted when the cause is not established: ask first
    Resistance.CONFLICTING: (State.UNKNOWN, (), Strategy.ASK_DIAGNOSTIC_QUESTION, Strategy.CONTINUE_NORMAL_FLOW),
    Resistance.UNKNOWN: (State.INSUFFICIENT_DATA, (), Strategy.CONTINUE_NORMAL_FLOW, None),
}
ALWAYS_AVOID = (Avoid.FALSE_URGENCY, Avoid.FABRICATED_SCARCITY, Avoid.FABRICATED_SOCIAL_PROOF, Avoid.FALSE_CLAIMS,
                Avoid.EXPLOITING_SENSITIVE_TRAITS, Avoid.PRESSURE_AFTER_REFUSAL, Avoid.GUILT_TRIPPING)


def plan_for(resistance: Resistance):
    return PLAN[resistance]
