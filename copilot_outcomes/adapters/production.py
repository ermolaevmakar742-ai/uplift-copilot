"""Production funnel event -> Outcome. Existing structured format: `funnel_events` row (id, user_id, old_stage,
new_stage, source, event, created_at), written by main.set_user_stage.

Mapped:  new_stage == "lost"  -> DROPPED   (an operator/system recorded the lead as lost; main only sets it via
         the admin stage change). metadata keeps source/event so the nature of the record stays visible.
Skipped: source == "postback" (the same fact is already an Outcome from the postback adapter: no double count),
         every other stage change (no canonical outcome type / a manual stage label is not a confirmed fact).
GAP: there is no structured production event for HUMAN_HANDOFF (manual_mode toggles are not logged) and
REGISTRATION only exists as a postback. Nothing is invented for them.
observed_at = created_at of the row (never now); outcome_id = "production:funnel_event:<id>"; chat_id = user_id.
"""
from copilot_outcomes.adapters.base import AdapterResult, aware_dt, as_int, get, make, skip, stable_id
from copilot_outcomes.contracts import OutcomeSource, OutcomeType


def adapt_production_event(data) -> AdapterResult:
    try:
        new_stage = get(data, "new_stage")
        new_stage = new_stage.strip().lower() if isinstance(new_stage, str) else None
        if not new_stage:
            return skip("funnel_event", "missing_new_stage")
        src = get(data, "source")
        if src == "postback":
            return skip(new_stage, "already_represented_by_postback_outcome")
        if new_stage != "lost":
            return skip(new_stage, "no_outcome_type_for_stage_change")
        chat_id, fid = as_int(get(data, "user_id")), stable_id(get(data, "id"))
        if chat_id is None:
            return skip(new_stage, "missing_chat_id")
        if fid is None:
            return skip(new_stage, "missing_stable_event_id")
        ts = aware_dt(get(data, "created_at"))
        if ts is None:
            return skip(new_stage, "missing_or_unreliable_timestamp")
        meta = {"source": src if isinstance(src, str) else None, "event": get(data, "event")}
        return make(new_stage, outcome_id=f"production:funnel_event:{fid}", decision_id=get(data, "decision_id"),
                    chat_id=chat_id, observed_at=ts, outcome_type=OutcomeType.DROPPED,
                    value={"old_stage": get(data, "old_stage"), "new_stage": new_stage},
                    confidence=get(data, "confidence"), source=OutcomeSource.PRODUCTION, metadata=meta)
    except Exception as e:  # fail-safe
        return skip("funnel_event", f"adapter_error:{type(e).__name__}")
