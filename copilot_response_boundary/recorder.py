"""Pure builders: facts about a send -> ResponseBoundary. No clock, no IO: `sent_at` always comes from the caller
(capture.py reads it at the send event). Never looks up or guesses a decision."""
from datetime import datetime
from typing import Any

from copilot_evidence.contracts import ContractError, _dt
from copilot_response_boundary.contract import BoundaryStatus, ResponseBoundary, compute_id


def _build(status: BoundaryStatus, *, chat_id, telegram_message_id, sent_at, decision_id, part_index, parts_total,
           timestamp_source, telegram_date, extra) -> ResponseBoundary:
    meta: dict[str, Any] = {"timestamp_source": timestamp_source,
                            "linkage": "unlinked" if decision_id is None else "decision"}
    if part_index is not None or parts_total is not None:
        meta["part_index"], meta["parts_total"] = part_index, parts_total
    if telegram_date is not None:
        meta["telegram_date"] = _dt("telegram_date", telegram_date).isoformat()
    for k, v in (extra or {}).items():
        if k in meta:
            raise ContractError(f"extra metadata may not override {k}")
        meta[k] = v
    if not isinstance(sent_at, datetime):
        raise ContractError("sent_at must be a datetime supplied by the caller (the event instant)")
    at = _dt("sent_at", sent_at)
    rid = compute_id(status.value, chat_id, telegram_message_id, decision_id, part_index, at.isoformat())
    return ResponseBoundary(rid, decision_id, chat_id, telegram_message_id, at, status, meta)


def build_sent(*, chat_id: int, telegram_message_id: int, sent_at: datetime, decision_id: str | None = None,
               part_index: int | None = None, parts_total: int | None = None, timestamp_source: str,
               telegram_date: datetime | None = None, extra: dict | None = None) -> ResponseBoundary:
    return _build(BoundaryStatus.SENT, chat_id=chat_id, telegram_message_id=telegram_message_id, sent_at=sent_at,
                  decision_id=decision_id, part_index=part_index, parts_total=parts_total,
                  timestamp_source=timestamp_source, telegram_date=telegram_date, extra=extra)


def build_failed(*, chat_id: int, failed_at: datetime, decision_id: str | None = None, part_index: int | None = None,
                 parts_total: int | None = None, timestamp_source: str, extra: dict | None = None) -> ResponseBoundary:
    return _build(BoundaryStatus.FAILED, chat_id=chat_id, telegram_message_id=None, sent_at=failed_at,
                  decision_id=decision_id, part_index=part_index, parts_total=parts_total,
                  timestamp_source=timestamp_source, telegram_date=None, extra=extra)
