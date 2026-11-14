"""Observable mismatch between a stated intent signal and later behaviour.

Not a judgement about the person: it only reports that two observable
signals in the transcript disagree. Output carries signal names and
dialog ids/timestamps, never message text.
"""
from copilot_shadow import signals as S

VERSION = "contradiction-1"
# priority when several types fire
_ORDER = ("PAYMENT_INTENT_MISMATCH", "TRUST_INTENT_MISMATCH",
          "INTENT_BEHAVIOR_MISMATCH", "MOMENTUM_INTENT_MISMATCH")


def _none(why: str, intent="NONE", commit="UNKNOWN") -> dict:
    return {"version": VERSION, "detected": False, "type": "NONE", "confidence": 0.0,
            "confidence_type": S.CONFIDENCE_TYPE, "intent_signal": intent,
            "behavioral_commitment": commit, "evidence": [], "signals": [], "note": why}


def _high_intent_indices(fs):
    out = []
    for i, f in enumerate(fs):
        if f.role != "user":
            continue
        if f.trust or f.refusal or f.deferral:
            continue          # a turn that itself carries an objection is not an intent anchor
        if f.strong_intent or (f.affirm and S.cta_before(fs, i) is not None):
            out.append(i)
    return out


def detect(fs: list, momentum: dict | None = None) -> dict:
    hi = _high_intent_indices(fs)
    if not hi:
        return _none("no_high_intent_signal")
    t_h = hi[-1]                                   # latest high-intent turn
    intent_f = fs[t_h]
    obs: dict[str, list] = {}                      # type -> evidence list
    signals: list[str] = []

    def add(kind, f, name):
        obs.setdefault(kind, []).append(S.ev(f, name))
        if name not in signals:
            signals.append(name)

    last_reaction = S.reaction(fs, t_h)
    for i in range(t_h + 1, len(fs)):
        f = fs[i]
        if f.role != "user":
            continue
        r = S.reaction(fs, i)
        c = S.cta_before(fs, i)
        if r:
            last_reaction = r
        if r in ("ignored", "deferred"):
            if c is not None and c.pay_cta:
                add("PAYMENT_INTENT_MISMATCH", f, f"{r}_payment_cta_after_intent")
            else:
                add("INTENT_BEHAVIOR_MISMATCH", f, f"{r}_cta_after_intent")
        if f.pay_amount:
            add("PAYMENT_INTENT_MISMATCH", f, "payment_hesitation_after_intent")
        if f.trust:
            add("TRUST_INTENT_MISMATCH", f, "trust_concern_after_intent")
        if f.basic_question:
            add("INTENT_BEHAVIOR_MISMATCH", f, "basic_question_after_intent")
        if f.refusal:
            add("INTENT_BEHAVIOR_MISMATCH", f, "refusal_after_intent")
    if momentum and momentum.get("direction") == "FALLING":
        add("MOMENTUM_INTENT_MISMATCH", fs[S.last_user_index(fs)], "momentum_falling_after_intent")

    if not obs:
        commit = "HIGH" if last_reaction == "engaged" else "UNKNOWN"
        return _none("intent_followed_by_no_conflicting_behaviour", intent="HIGH", commit=commit)

    kind = next(k for k in _ORDER if k in obs)
    n = sum(len(v) for v in obs.values())
    evidence = [S.ev(intent_f, "high_intent")] + obs[kind]
    return {
        "version": VERSION, "detected": True, "type": kind,
        "confidence": round(min(0.85, 0.35 + 0.15 * n), 2),
        "confidence_type": S.CONFIDENCE_TYPE,
        "intent_signal": "HIGH", "behavioral_commitment": "LOW",
        "evidence": evidence, "signals": signals,
    }
