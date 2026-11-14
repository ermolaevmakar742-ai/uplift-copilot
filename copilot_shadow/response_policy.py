"""Response Policy v1 (shadow only): "should we answer NOW at all?".

Pure, deterministic, no I/O. Output is a recommendation record; nothing here
blocks, delays or alters a real reply. ESCALATE/REVIEW is only an observation
label (maps to the existing manual_mode/HUMAN_HANDOFF concept).
"""
RESPONSE_POLICY_VERSION = "response_policy-1"
RESPOND, WAIT, ESCALATE = "RESPOND", "WAIT", "ESCALATE_REVIEW"

HIGH_MOMENTUM = 0.5
HIGH_FREQ_MSGS = 4          # client messages inside FREQ_WINDOW_S
FREQ_WINDOW_S = 60.0
RECENT_CLIENT_S = 8.0       # client still typing / just wrote
BASE_WAIT_S = 20
MAX_WAIT_S = 120


def recommend(sig: dict) -> dict:
    """sig keys (all optional): last_client_question, last_client_words, last_client_age_s,
    last_valeria_asked (bool: Valeria's last message ended with a question / CTA),
    waiting_for_client (bool), recent_client_msgs (int in FREQ_WINDOW_S), momentum (dict),
    contradiction (dict or canonical-state contradiction bool), next_best_question (dict),
    stage, buying_signal (bool), risk (dict), manual_mode (bool)."""
    sig = sig or {}
    mom = sig.get("momentum") or {}
    score = mom.get("score") if isinstance(mom.get("score"), (int, float)) else 0.0
    ctr = sig.get("contradiction")
    ctr_on = bool(ctr.get("detected")) if isinstance(ctr, dict) else bool(ctr)
    risk = sig.get("risk") or {}
    nbq = sig.get("next_best_question") or {}
    supporting = {"momentum_score": score, "stage": sig.get("stage"),
                  "recent_client_msgs": sig.get("recent_client_msgs"),
                  "last_client_age_s": sig.get("last_client_age_s"),
                  "next_question_target": nbq.get("target_unknown")}

    def out(policy, conf, reasons, wait=None):
        return {"version": RESPONSE_POLICY_VERSION, "response_policy": policy,
                "confidence": round(conf, 2), "reason_codes": reasons,
                "suggested_wait_seconds": wait if policy == WAIT else None,
                "supporting_signals": supporting, "shadow_only": True}

    if risk.get("self_exclusion") or risk.get("minor") or sig.get("manual_mode"):
        return out(ESCALATE, 0.9, ["risk_or_manual_review"])

    resp, wait = [], []
    if sig.get("last_client_question"):
        resp.append("client_question_requires_response")
    if sig.get("buying_signal"):
        resp.append("buying_signal")
    if score >= HIGH_MOMENTUM:
        resp.append("high_momentum")

    freq = sig.get("recent_client_msgs") or 0
    age = sig.get("last_client_age_s")
    if freq >= HIGH_FREQ_MSGS:
        wait.append("message_frequency_too_high")
    if isinstance(age, (int, float)) and age < RECENT_CLIENT_S and not sig.get("last_client_question"):
        wait.append("recent_client_message")
    if sig.get("waiting_for_client"):
        wait.append("already_waiting_for_client")
    if ctr_on and not sig.get("last_client_question"):
        wait.append("contradiction_requires_pause")
    words = sig.get("last_client_words")
    if isinstance(words, int) and words <= 1 and not sig.get("last_client_question") and not sig.get("buying_signal"):
        wait.append("low_information_value")
    if sig.get("followup") and sig.get("waiting_for_client"):
        wait.append("followup_should_wait")

    # an explicit question / buying signal outweighs soft waits; hard waits need no active reason
    if resp and not ({"already_waiting_for_client"} & set(wait)):
        return out(RESPOND, min(0.95, 0.6 + 0.1 * len(resp)), resp + [w for w in wait if w == "message_frequency_too_high"])
    if wait:
        secs = min(MAX_WAIT_S, BASE_WAIT_S * (1 + len(wait)) + (20 if "message_frequency_too_high" in wait else 0))
        return out(WAIT, min(0.9, 0.5 + 0.1 * len(wait)), wait, secs)
    return out(RESPOND, 0.5, ["default_respond"])


def signals_from_turns(fs: list, now=None) -> dict:
    """Derive timing/frequency signals from shadow signals.F turns (flags only, no text)."""
    users = [f for f in fs if f.role == "user"]
    if not users:
        return {}
    cur = users[-1]
    last_val = next((f for f in reversed(fs) if f.role == "assistant"), None)
    sig = {"last_client_question": cur.question, "last_client_words": cur.words,
           "buying_signal": bool(cur.strong_intent or cur.deposit_done),
           "last_valeria_asked": bool(last_val and (last_val.question or last_val.cta))}
    ts = [f.ts for f in users if f.ts is not None]
    if ts:
        if now is not None:                       # age is only meaningful against a real "now"
            sig["last_client_age_s"] = max(0.0, (now - ts[-1]).total_seconds())
        sig["recent_client_msgs"] = sum(1 for t in ts if (ts[-1] - t).total_seconds() <= FREQ_WINDOW_S)
    # Valeria's last message is newer than the client's and asked something -> we are waiting
    if last_val is not None and fs.index(last_val) > fs.index(cur):
        sig["waiting_for_client"] = sig["last_valeria_asked"]
    return sig
