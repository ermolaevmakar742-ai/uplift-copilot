"""Next Best Question: which single UNKNOWN is most worth resolving next.

Returns an intent label only (no question text, no LLM, nothing is sent).
Taxonomy of unknowns: TRUST PAYMENT NEED VALUE OBJECTION READINESS TECHNICAL NONE.
"""
from copilot_shadow import signals as S

VERSION = "next-question-1"
UNKNOWNS = ("TRUST", "PAYMENT", "NEED", "VALUE", "OBJECTION", "READINESS", "TECHNICAL", "NONE")
INTENT = {"TRUST": "ASK_TRUST_CONCERN", "PAYMENT": "ASK_PAYMENT_BARRIER", "NEED": "ASK_NEED_CLARIFICATION",
          "VALUE": "ASK_VALUE_EXPECTATION", "OBJECTION": "ASK_OBJECTION_DETAIL",
          "READINESS": "ASK_READINESS", "TECHNICAL": "ASK_TECHNICAL_BLOCKER", "NONE": "NONE"}


def recommend(fs: list, contradiction: dict | None, momentum: dict | None, path: dict | None) -> dict:
    cand: list[dict] = []   # in priority order
    path = path or {}
    ctr = contradiction or {}
    ctype = ctr.get("type") if ctr.get("detected") else None
    blocker, bconf = path.get("blocker"), path.get("blocker_confidence") or 0.0
    node, nconf = path.get("current_node"), path.get("transition_confidence") or 0.0

    def add(target, conf, rule):
        cand.append({"target_unknown": target, "question_intent": INTENT[target],
                     "confidence": round(conf, 2), "rule": rule})

    if blocker == "TECHNICAL_ISSUE":
        add("TECHNICAL", bconf, "blocker_technical_issue")
    if blocker in ("PAYMENT_METHOD_UNCERTAINTY", "PAYMENT_AMOUNT_HESITATION"):
        add("PAYMENT", bconf, f"blocker_{blocker.lower()}")
    elif ctype == "PAYMENT_INTENT_MISMATCH":
        add("PAYMENT", ctr.get("confidence", 0.0), "contradiction_payment_intent_mismatch")
    if blocker == "TRUST_CONCERN":
        add("TRUST", bconf, "blocker_trust_concern")
    elif ctype == "TRUST_INTENT_MISMATCH":
        add("TRUST", ctr.get("confidence", 0.0), "contradiction_trust_intent_mismatch")
    elif node == "TRUST" and nconf >= 0.5:
        add("TRUST", nconf, "current_node_trust")
    recent = [f for f in fs if f.role == "user"][-3:]
    cur_deferral = bool(recent and recent[-1].deferral)
    if node == "OBJECTION" and blocker != "DEFERRAL" and not cur_deferral and nconf >= 0.5:
        add("OBJECTION", nconf, "current_node_objection_unspecified")
    if blocker == "DEFERRAL":
        add("READINESS", bconf, "blocker_deferral")
    elif cur_deferral:
        add("READINESS", nconf or 0.5, "deferral_in_current_turn")
    elif ctype in ("INTENT_BEHAVIOR_MISMATCH", "MOMENTUM_INTENT_MISMATCH"):
        add("READINESS", ctr.get("confidence", 0.0), f"contradiction_{ctype.lower()}")
    if any(f.value for f in recent):
        add("VALUE", 0.5, "value_question_asked")
    if node == "INTEREST" and nconf >= 0.5 and not any(f.need for f in recent):
        add("NEED", nconf, "interest_without_need_disclosure")

    # de-duplicate by target, keep first (= highest priority) occurrence
    seen, uniq = set(), []
    for c in cand:
        if c["target_unknown"] not in seen:
            seen.add(c["target_unknown"]); uniq.append(c)
    if not uniq:
        return {"version": VERSION, "recommended": False, "question_intent": "NONE",
                "target_unknown": "NONE", "confidence": 0.0, "confidence_type": S.CONFIDENCE_TYPE,
                "reason": "insufficient_evidence", "candidates": []}
    top = uniq[0]
    return {"version": VERSION, "recommended": True, "question_intent": top["question_intent"],
            "target_unknown": top["target_unknown"], "confidence": top["confidence"],
            "confidence_type": S.CONFIDENCE_TYPE, "reason": "rule:" + top["rule"], "candidates": uniq}
