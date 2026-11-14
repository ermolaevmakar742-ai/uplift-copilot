"""Pure ΔState computation. No clock, no IO. Nothing is invented: missing state stays UNKNOWN."""
from datetime import datetime
from typing import Any, Iterable, Mapping

from copilot_state_delta.contract import (FIELDS, FieldDelta, StateDelta, StateSnapshot, compare, compute_id)


def compute_state_delta(before: StateSnapshot, after: StateSnapshot, *, chat_id: int, anchor_decision_id: str | None = None,
                        metadata: Mapping | None = None) -> StateDelta:
    fields = tuple(FieldDelta(f, compare(getattr(before, f), getattr(after, f)), getattr(before, f), getattr(after, f))
                   for f in FIELDS)
    return StateDelta(compute_id(chat_id, anchor_decision_id, before.to_dict(), after.to_dict()), chat_id,
                      anchor_decision_id, before, after, fields, dict(metadata or {}))


def pair_consecutive_decisions(decisions: Iterable[Mapping], boundary_end: Mapping[str, datetime]):
    """ΔState across an observed action: before = the state embedded in decision D_n, after = the state embedded in the next
    decision D_{n+1} of the same chat, only when D_n's reply is known to have been SENT (boundary_end[decision_id]) and
    D_{n+1} was created strictly after it. Each decision is a mapping with decision_id, chat_id, created_at, state (dict|None).
    Returns (deltas, skipped) with skipped = [(decision_id, reason)]; both deterministically ordered. The state of
    D_{n+1} is the state known when that later decision was made; nothing about the future of D_{n+1} is used."""
    by_chat: dict[int, list[Mapping]] = {}
    for d in decisions:
        by_chat.setdefault(d["chat_id"], []).append(d)
    deltas, skipped = [], []
    for chat in sorted(by_chat):
        seq = sorted(by_chat[chat], key=lambda d: (d["created_at"], d["decision_id"]))
        for cur, nxt in zip(seq, seq[1:]):
            end = boundary_end.get(cur["decision_id"])
            if end is None:
                skipped.append((cur["decision_id"], "no_sent_response_boundary"))
            elif not nxt["created_at"] > end:
                skipped.append((cur["decision_id"], "next_decision_before_reply_completed"))
            else:
                deltas.append(compute_state_delta(
                    StateSnapshot.from_client_state(cur.get("state"), cur["created_at"]),
                    StateSnapshot.from_client_state(nxt.get("state"), nxt["created_at"]),
                    chat_id=chat, anchor_decision_id=cur["decision_id"],
                    metadata={"after_decision_id": nxt["decision_id"], "boundary_sent_at": end.isoformat()}))
    return tuple(deltas), tuple(sorted(skipped))
