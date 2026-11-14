"""COPILOT Shadow Decision — OBSERVATION layer (pre-uplift).

Read-only observer: for each live message that reached the production LLM
reply, compute a transparent rule-based "what would we choose" record and
persist it. Never sends, never mutates client state, never feeds back into
production. Disabled unless COPILOT_SHADOW_ENABLED is true.
"""
from copilot_shadow.hook import schedule_shadow, shadow_enabled

__all__ = ["schedule_shadow", "shadow_enabled"]
