"""Shadow Decision Contract: ids, dedup key, state reference. Pure, no I/O, no PII.

Chain: SHADOW RECOMMENDATION -> PRODUCTION ACTION -> OUTCOME, all on one decision_id.
Instrumentation only: no uplift, no causal claims.
"""
import hashlib
import json

CONTRACT_VERSION = "shadow-contract-4"   # v4: + canonical production action (map_decision v2); v3: research fields
DECISION_POINT_REPLY = "reply"          # the only live decision point today
SESSION_GAP_HOURS = 6.0                 # a gap >= this starts a new conversation session


def _h(*parts, n=16) -> str:
    return hashlib.sha1("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()[:n]


def client_id(chat_id) -> str:
    """Pseudonymous, stable client id (the raw Telegram id is not repeated here)."""
    return "cli_" + _h("ares-shadow-client", int(chat_id))


def session_id(chat_id, turns) -> str:
    """Session = run of turns without a >=SESSION_GAP_HOURS gap; id from the session's first turn.
    No timestamps/ids available -> a stable per-chat 'unknown' session (never random)."""
    first = None
    prev_ts = None
    for t in turns or ():
        ts = getattr(t, "ts", None)
        if first is None or (prev_ts is not None and ts is not None
                             and (ts - prev_ts).total_seconds() / 3600 >= SESSION_GAP_HOURS):
            first = t
        if ts is not None:
            prev_ts = ts
    if first is None or (getattr(first, "id", None) is None and getattr(first, "ts", None) is None):
        return "ses_" + _h("nosession", int(chat_id), n=12)
    ts = getattr(first, "ts", None)
    return "ses_" + _h(int(chat_id), getattr(first, "id", None), ts.isoformat() if ts else None, n=12)


def dedup_key(client, session, decision_point, message_id) -> str:
    """Same client + session + decision point + triggering message == the same event."""
    return "dk_" + _h(client, session, decision_point, message_id, n=24)


def state_ref(snapshot) -> str:
    """Content hash of the (already PII-free) snapshot: lets later analysis detect state change
    without copying the payload again."""
    return "st_" + hashlib.sha1(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()[:20]
