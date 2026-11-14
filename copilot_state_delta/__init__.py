"""COPILOT ΔState (phase 5I): immutable comparison of the client state before and after an observed action.
Pure and read-only: no DB, no network, no LLM, no clock. A missing value is UNKNOWN, never UNCHANGED."""
from copilot_state_delta.contract import (CONTRACT_VERSION, FIELDS, FieldDelta, FieldStatus, StateDelta, StateSnapshot)
from copilot_state_delta.compute import compute_state_delta, pair_consecutive_decisions

__all__ = ["CONTRACT_VERSION", "FIELDS", "FieldDelta", "FieldStatus", "StateDelta", "StateSnapshot",
           "compute_state_delta", "pair_consecutive_decisions"]
