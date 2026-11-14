"""Read-only adapters: existing COPILOT events -> canonical Outcome (phase 4B). Pure; not wired into production."""
from copilot_outcomes.adapters.base import AdapterResult, Skipped, skip
from copilot_outcomes.adapters.postback import adapt_postback
from copilot_outcomes.adapters.production import adapt_production_event
from copilot_outcomes.adapters.telegram import adapt_telegram_event

ADAPTERS = {"postback": adapt_postback, "telegram": adapt_telegram_event, "production": adapt_production_event}


def adapt(source: str, data) -> AdapterResult:
    """Dispatch by source name; an unsupported source is rejected (skipped), never guessed."""
    fn = ADAPTERS.get(source) if isinstance(source, str) else None
    return fn(data) if fn else skip(str(source), "unsupported_source")


__all__ = ["ADAPTERS", "AdapterResult", "Skipped", "adapt", "adapt_postback", "adapt_production_event",
           "adapt_telegram_event"]
