"""COPILOT Decision Ledger (phase 1): append-only record of what production did per message.
Observer only. Disabled unless COPILOT_DECISION_LEDGER_ENABLED is true. No DDL at import."""
from copilot_decisions.contract import UNKNOWN, ProductionDecision
from copilot_decisions.recorder import ledger_enabled, record_production_decision

__all__ = ["UNKNOWN", "ProductionDecision", "ledger_enabled", "record_production_decision"]
