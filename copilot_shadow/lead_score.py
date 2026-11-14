"""Lead Score v1 (shadow only): deterministic 0..100 reading of how far along / how engaged a lead looks.

Pure function of already-computed COPILOT signals; same input -> same output; no ML, no I/O, no randomness.
It is an OBSERVATION written to the shadow record. It does not feed any production decision.
Weights are explicit constants below; every component reports its points and every non-zero rule a reason code.
"""

SCORER_VERSION = "lead-score-1"

# ---- explicit weights (max points per component) ----
# production stage value -> points (max 25). Keyed on production strings (this layer may not import the
# analytical stage contract); unknown values score 0 and say so in reason_codes.
STAGE_POINTS = {"new": 0, "visited": 3, "click": 8, "conversation": 8, "registration_started": 10, "registered": 12,
                "registration": 12, "qualified": 12, "objection": 10, "hesitation": 8, "offer": 18, "card_added": 18,
                "action": 20, "deposit_attempt": 20, "deposited": 25, "deposit": 25, "converted": 25,
                "lost": 0, "blocked": 0, "rejected": 0}
INTENT_POINTS = {"ready": 10, "question": 5, "greeting": 2, "objection": 3, "statement": 1, "unknown": 0}   # max 10
REPLY_POINTS = {"POSITIVE": 15, "MEETING": 15, "REFERRAL": 12, "PAYMENT_PROBLEM": 8, "QUESTION": 8,
                "PRICING_OBJECTION": 6, "TECHNICAL_OBJECTION": 6, "TRUST_CONCERN": 4, "SUPPORT": 4,
                "NOT_NOW": 3, "OTHER": 2, "NOT_INTERESTED": 0}                                            # max 15, x confidence
SENTIMENT_MAX = 10          # x sentiment in [0,1]
MOMENTUM_MAX = 15           # x (score+1)/2, momentum score in [-1,1]
ENGAGEMENT_PER_TURN, ENGAGEMENT_MAX = 2, 10
DEPOSIT_POINTS, PAYMENT_POINTS = 15, 8
EVIDENCE_MAX = 5            # x mean confidence of resolved canonical fields
CONTRADICTION_PENALTY = 10
CANONICAL_CONFLICT_PENALTY, CANONICAL_CONFLICT_CAP = 3, 6
BANDS = ((75, "HOT"), (50, "WARM"), (25, "COOL"), (0, "COLD"))


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def band(score: int) -> str:
    return next(name for floor, name in BANDS if score >= floor)


def score(inp: dict) -> dict:
    """inp keys (all optional): stage (production string), intent, sentiment, reply_classification (dict from
    reply_class), momentum (dict), risk (dict), deposit_reported (bool), payment_signal (bool),
    contradiction (dict with 'detected'), canonical_state (dict field->decision), user_turns (int)."""
    inp = inp or {}
    comp: dict = {}
    reasons: list = []

    key = str(inp.get("stage")).strip().lower() if inp.get("stage") else ""
    if key in STAGE_POINTS:
        comp["stage"] = STAGE_POINTS[key]
        reasons.append("stage:" + key)
    else:
        comp["stage"] = 0
        reasons.append("stage_unmapped")

    intent = inp.get("intent")
    comp["intent"] = INTENT_POINTS.get(intent, 0)
    if comp["intent"]:
        reasons.append("intent:" + str(intent))

    rc = inp.get("reply_classification") or {}
    cls, rconf = rc.get("classification"), _num(rc.get("confidence")) or 0.0
    comp["reply"] = round(REPLY_POINTS.get(cls, 0) * _clamp(rconf, 0.0, 1.0), 1)
    if comp["reply"]:
        reasons.append("reply:" + cls.lower())

    sent = _num(inp.get("sentiment"))
    comp["sentiment"] = round(SENTIMENT_MAX * _clamp(sent, 0.0, 1.0), 1) if sent is not None else 0.0
    if sent is None:
        reasons.append("sentiment_unavailable")

    mom = (inp.get("momentum") or {})
    ms = _num(mom.get("score"))
    comp["momentum"] = round(MOMENTUM_MAX * (_clamp(ms, -1.0, 1.0) + 1) / 2, 1) if ms is not None else 0.0
    if ms is None:
        reasons.append("momentum_unavailable")
    elif mom.get("direction") in ("RISING", "FALLING"):
        reasons.append("momentum:" + mom["direction"].lower())

    turns = inp.get("user_turns")
    turns = turns if isinstance(turns, int) and not isinstance(turns, bool) else (mom.get("user_turns_seen") or 0)
    comp["engagement"] = _clamp(turns * ENGAGEMENT_PER_TURN, 0, ENGAGEMENT_MAX)

    if inp.get("deposit_reported"):
        comp["payment"] = DEPOSIT_POINTS
        reasons.append("deposit_reported")
    elif inp.get("payment_signal"):
        comp["payment"] = PAYMENT_POINTS
        reasons.append("payment_signal")
    else:
        comp["payment"] = 0

    canon = inp.get("canonical_state") or {}
    resolved = [c.get("confidence") or 0.0 for c in canon.values() if isinstance(c, dict) and c.get("status") == "resolved"]
    comp["evidence"] = round(EVIDENCE_MAX * (sum(resolved) / len(resolved)), 1) if resolved else 0.0

    pen = 0
    if (inp.get("contradiction") or {}).get("detected"):
        pen += CONTRADICTION_PENALTY
        reasons.append("contradiction_detected")
    conflicts = sum(1 for c in canon.values() if isinstance(c, dict) and c.get("status") == "unresolved"
                    and c.get("contradiction"))
    if conflicts:
        pen += min(CANONICAL_CONFLICT_CAP, CANONICAL_CONFLICT_PENALTY * conflicts)
        reasons.append("evidence_conflict")
    comp["penalty"] = -pen

    raw = sum(comp.values())
    risk = inp.get("risk") or {}
    if risk.get("self_exclusion") or risk.get("minor"):
        total, b = 0, "EXCLUDED"
        reasons.append("risk_excluded")
    else:
        total = int(round(_clamp(raw, 0, 100)))
        b = band(total)
    return {"lead_score": total, "score_band": b, "score_components": comp, "reason_codes": reasons,
            "scorer_version": SCORER_VERSION}
