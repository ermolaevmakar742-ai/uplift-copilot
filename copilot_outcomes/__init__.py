"""COPILOT Outcomes (phase 4A): observational Outcome contract + deterministic read-only joiner.
Pure: no DB, no network, no LLM, not wired into production. No causal claims, only observation."""
from copilot_outcomes.contracts import (ATTR_EXPLICIT, ATTR_FALLBACK, DEFAULT_OUTCOME_WINDOW_SECONDS,
                                     OUTCOME_CONTRACT_VERSION, Outcome, OutcomeSource, OutcomeType)
from copilot_outcomes.joiner import AttributedOutcome, DecisionRef, OutcomeSet, OutcomeWindow, join, join_all

__all__ = ["ATTR_EXPLICIT", "ATTR_FALLBACK", "DEFAULT_OUTCOME_WINDOW_SECONDS", "OUTCOME_CONTRACT_VERSION",
           "AttributedOutcome", "DecisionRef", "Outcome", "OutcomeSet", "OutcomeSource", "OutcomeType",
           "OutcomeWindow", "join", "join_all"]
