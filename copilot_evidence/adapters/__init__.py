"""Read-only adapters: existing COPILOT structures -> canonical Evidence (Phase 2B).
Pure, deterministic, no I/O. Bad/incomplete input is skipped with a reason, never invented.
NOT wired into production."""
from copilot_evidence.adapters.base import AdapterResult, Skipped
from copilot_evidence.adapters.client_state import adapt_client_state
from copilot_evidence.adapters.production import adapt_production_decision
from copilot_evidence.adapters.shadow import adapt_shadow_row

__all__ = ["AdapterResult", "Skipped", "adapt_client_state",
           "adapt_production_decision", "adapt_shadow_row"]
