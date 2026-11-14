"""Postback -> Outcome. Existing format (main.PostbackRequest / PostbackEvent): user_id, event, event_id,
payload, amount, timestamp (UNIX) [DB row: created_at]. There is NO decision_id in that format, so
decision_id is None unless the input explicitly carries one; nothing is generated or looked up.

Mapped (only what the event literally states):
  deposit_attempt -> PAYMENT_ATTEMPT          deposit_success -> DEPOSIT
  register_complete -> REGISTRATION
One event -> one Outcome. Skipped with reason: visit, register_start (start != registration), card_added,
withdraw_request/withdraw_success (no outcome type), reject (ambiguous: what was rejected?), unknown events.
PAYMENT_SUCCESS / PAYMENT_FAILED have no postback event today (gap): they are never inferred.
observed_at = the event's own timestamp (never now); outcome_id = "postback:<user_id>:<event>:<event_id>"
(same triple production uses for postback dedup). chat_id = user_id. confidence: only a valid optional
"confidence" key is passed through, otherwise None. `payload` (free text, may hold PII) is NEVER copied;
only whitelisted fields are kept.
"""
from copilot_outcomes.adapters.base import (AdapterResult, aware_dt, amount, as_int, get, make, skip, stable_id,
                                         unix_ts)
from copilot_outcomes.contracts import OutcomeSource, OutcomeType

EVENT_TO_OUTCOME = {
    "deposit_attempt": OutcomeType.PAYMENT_ATTEMPT,
    "deposit_success": OutcomeType.DEPOSIT,
    "register_complete": OutcomeType.REGISTRATION,
}
_NO_OUTCOME = {
    "visit": "visit_is_not_an_outcome_type",
    "register_start": "registration_started_is_not_registration",
    "card_added": "no_outcome_type_for_card_added",
    "withdraw_request": "no_outcome_type_for_withdrawals",
    "withdraw_success": "no_outcome_type_for_withdrawals",
    "reject": "ambiguous_rejection",
}


def adapt_postback(data) -> AdapterResult:
    try:
        event = get(data, "event")
        event = event.strip() if isinstance(event, str) else None
        if not event:
            return skip("postback", "missing_event")
        otype = EVENT_TO_OUTCOME.get(event)
        if otype is None:
            return skip(event, _NO_OUTCOME.get(event, "unknown_event"))
        chat_id, eid = as_int(get(data, "user_id")), stable_id(get(data, "event_id"))
        if chat_id is None:
            return skip(event, "missing_chat_id")
        if eid is None:
            return skip(event, "missing_stable_event_id")
        ts = unix_ts(get(data, "timestamp")) if get(data, "timestamp") is not None else aware_dt(get(data, "created_at"))
        if ts is None:
            return skip(event, "missing_or_unreliable_timestamp")
        amt = amount(get(data, "amount"))
        meta = {"event": event, "event_id": eid}
        if get(data, "payload") is not None:
            meta["payload_dropped"] = True
        return make(event, outcome_id=f"postback:{chat_id}:{event}:{eid}", decision_id=get(data, "decision_id"),
                    chat_id=chat_id, observed_at=ts, outcome_type=otype,
                    value=None if amt is None else {"amount": amt}, confidence=get(data, "confidence"),
                    source=OutcomeSource.POSTBACK, metadata=meta)
    except Exception as e:  # fail-safe
        return skip("postback", f"adapter_error:{type(e).__name__}")
