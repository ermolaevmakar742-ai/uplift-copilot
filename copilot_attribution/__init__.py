"""COPILOT Attribution v1 (phase 5D): immutable AttributionResult contract + offline pure rules.
Analytical only: no DB, no network, no LLM, no clock, not wired into production. No causal claims."""
from copilot_attribution.contract import (AttributionConfidence, AttributionPolicy, AttributionResult, CONTRACT_VERSION,
                                       DecisionRef, Interruption, InterruptionKind, Method, OutcomePolicy,
                                       PolicyKind, Relationship)
from copilot_attribution.rules import DEFAULT_POLICY, attribute_all, attribute_outcome

__all__ = ["AttributionConfidence", "AttributionPolicy", "AttributionResult", "CONTRACT_VERSION", "DEFAULT_POLICY",
           "DecisionRef", "Interruption", "InterruptionKind", "Method", "OutcomePolicy", "PolicyKind", "Relationship",
           "attribute_all", "attribute_outcome"]
