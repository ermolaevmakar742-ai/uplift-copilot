"""Conversation momentum: transparent rule-based scorer (not a model).

score = clamp(sum of fixed signal weights, -1, 1). Every fired signal is
returned so the score can be audited. confidence is rule COVERAGE
(how many user turns the rules saw, halved when no rule fired), not a
probability.
"""
from copilot_shadow import signals as S

VERSION = "momentum-1"
RISING_AT, FALLING_AT = 0.2, -0.2

WEIGHTS = {
    # positive
    "asked_question": 0.15,
    "substantive_reply": 0.15,
    "need_disclosed": 0.20,
    "asked_payment_question": 0.25,
    "asked_value_question": 0.20,
    "requested_next_step": 0.30,
    "engaged_after_cta": 0.30,
    "returned_after_pause": 0.20,
    # negative
    "one_word_reply": -0.20,
    "ignored_cta": -0.35,
    "deferred_after_cta": -0.30,
    "refusal": -0.50,
    "repeated_objection": -0.30,
    "engagement_drop": -0.25,
}


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def compute(fs: list) -> dict:
    ui = S.last_user_index(fs)
    if ui is None:
        return _empty("no_user_turns")
    cur = fs[ui]
    users = [i for i, f in enumerate(fs) if f.role == "user"]
    earlier_users = [fs[i] for i in users if i < ui]
    pos, neg = [], []

    if cur.question:
        pos.append("asked_question")
    if cur.words >= 8:
        pos.append("substantive_reply")
    if cur.need or any(f.need for f in earlier_users):
        pos.append("need_disclosed")
    if cur.payment and cur.question:
        pos.append("asked_payment_question")
    if cur.value and cur.question:
        pos.append("asked_value_question")
    if cur.strong_intent:
        pos.append("requested_next_step")

    r = S.reaction(fs, ui)
    if r == "engaged":
        pos.append("engaged_after_cta")
    elif r == "ignored":
        neg.append("ignored_cta")
    elif r == "deferred":
        neg.append("deferred_after_cta")

    if ui > 0:
        g = S.gap_hours(fs[ui - 1], cur)
        if g is not None and g >= S.PAUSE_HOURS:
            pos.append("returned_after_pause")

    if ui > 0 and cur.words <= 2 and not cur.question and not cur.affirm and not cur.strong_intent:
        neg.append("one_word_reply")
    if cur.refusal:
        neg.append("refusal")
    if (cur.trust or cur.refusal) and any(f.trust or f.refusal for f in earlier_users):
        neg.append("repeated_objection")
    if len(earlier_users) >= 2:
        recent = [f.words for f in (earlier_users[-1:] + [cur])]
        before = [f.words for f in earlier_users[:-1]]
        if before and _mean(before) >= 6 and _mean(recent) <= 0.5 * _mean(before):
            neg.append("engagement_drop")

    score = max(-1.0, min(1.0, sum(WEIGHTS[k] for k in pos + neg)))
    direction = "RISING" if score >= RISING_AT else "FALLING" if score <= FALLING_AT else "STABLE"
    coverage = min(len(users), 4) / 4
    conf = coverage if (pos or neg) else coverage * 0.5
    return {
        "version": VERSION,
        "score": round(score, 2),
        "direction": direction,
        "confidence": round(conf, 2),
        "confidence_type": S.CONFIDENCE_TYPE,
        "positive_signals": pos,
        "negative_signals": neg,
        "user_turns_seen": len(users),
    }


def _empty(why: str) -> dict:
    return {"version": VERSION, "score": 0.0, "direction": "STABLE", "confidence": 0.0,
            "confidence_type": S.CONFIDENCE_TYPE, "positive_signals": [], "negative_signals": [],
            "user_turns_seen": 0, "note": why}
