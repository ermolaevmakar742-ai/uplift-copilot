"""Telegram dialog event -> CONTINUED. Existing format: a `dialogs` row / Telegram message with an id
(id | message_id), user_id (chat), role and created_at (| date).

Only an observed USER message becomes CONTINUED ("the conversation went on"); assistant rows and rows without a
role are skipped. The message CONTENT IS NEVER READ OR COPIED: no text means no interpretation, so a message
can never become DEPOSIT/PAYMENT/REGISTRATION here. Whether it came after a decision is the joiner's job.
observed_at = the event's own timestamp (never now); outcome_id = "telegram:<chat_id>:<message id>";
confidence None (or a valid optional "confidence" key, passed through); decision_id None unless provided.
"""
from copilot_outcomes.adapters.base import AdapterResult, aware_dt, as_int, get, make, skip, stable_id
from copilot_outcomes.contracts import OutcomeSource, OutcomeType


def adapt_telegram_event(data) -> AdapterResult:
    try:
        role = get(data, "role")
        if role != "user":
            return skip("telegram", "not_a_user_message" if role in ("assistant", "system") else "ambiguous_or_missing_role")
        chat_id = as_int(get(data, "user_id"))
        chat_id = as_int(get(data, "chat_id")) if chat_id is None else chat_id
        mid = stable_id(get(data, "message_id") if get(data, "message_id") is not None else get(data, "id"))
        if chat_id is None:
            return skip("telegram", "missing_chat_id")
        if mid is None:
            return skip("telegram", "missing_stable_message_id")
        ts = aware_dt(get(data, "created_at") if get(data, "created_at") is not None else get(data, "date"))
        if ts is None:
            return skip("telegram", "missing_or_unreliable_timestamp")
        return make("telegram", outcome_id=f"telegram:{chat_id}:{mid}", decision_id=get(data, "decision_id"),
                    chat_id=chat_id, observed_at=ts, outcome_type=OutcomeType.CONTINUED, value=None,
                    confidence=get(data, "confidence"), source=OutcomeSource.TELEGRAM_EVENT,
                    metadata={"message_id": mid, "content_dropped": True})
    except Exception as e:  # fail-safe
        return skip("telegram", f"adapter_error:{type(e).__name__}")
