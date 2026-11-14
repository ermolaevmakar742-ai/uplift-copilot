"""Deterministic rule classifier over already-structured Signals. It reports CUES (each a named, weighted observation) and aggregates them
per resistance type. It never reads text, history, region or personal traits, and the regime label is NOT a cue (routing label only).
Confidence is rule agreement x signal coverage, capped; it is NOT a probability and carries no conversion claim."""
from dataclasses import dataclass

from copilot_contracts.stages import Stage, map_production_stage, normalize_stage
from copilot_psychology.contracts import Resistance, Signals, Uncertainty

CONFLICT_MARGIN = 0.15      # two resistances this close => conflicting, no mechanism is chosen
MIN_SCORE = 0.3             # below this a resistance is not reported
MAX_CONFIDENCE = 0.8        # rule-based: never claims certainty
MIN_USABLE = 2
CORROBORATION_CAP = 0.2     # extra support for the top specific cause from generic objection cues
HIGH_TRUST, LOW_TRUST, LOW_SENTIMENT = 0.7, 0.4, 0.3

# (cue_id, resistance, weight): the weights are rule strengths, not estimated effects
CUE_WEIGHTS = {
    "blocker_trust_concern": (Resistance.TRUST_CONCERN, 0.5), "trigger_fraud": (Resistance.TRUST_CONCERN, 0.4),
    "trust_signal_low": (Resistance.TRUST_CONCERN, 0.3),
    "blocker_amount_hesitation": (Resistance.PRICE_UNCERTAINTY, 0.5), "trigger_how_much": (Resistance.PRICE_UNCERTAINTY, 0.3),
    "blocker_method_uncertainty": (Resistance.PAYMENT_METHOD_UNCERTAINTY, 0.5),
    "blocker_technical": (Resistance.TECHNICAL_ISSUE, 0.5),
    "blocker_deferral": (Resistance.DEFERRAL, 0.4), "trigger_busy": (Resistance.DEFERRAL, 0.4),
    "intent_objection": (Resistance.UNSPECIFIED, 0.3), "objection_text_markers": (Resistance.UNSPECIFIED, 0.2), "stage_objection": (Resistance.UNSPECIFIED, 0.2),
}
_BLOCKER_CUES = {"TRUST_CONCERN": "blocker_trust_concern", "PAYMENT_AMOUNT_HESITATION": "blocker_amount_hesitation",
                 "PAYMENT_METHOD_UNCERTAINTY": "blocker_method_uncertainty", "TECHNICAL_ISSUE": "blocker_technical", "DEFERRAL": "blocker_deferral"}


@dataclass(frozen=True)
class Classification:
    usable: int
    cues: tuple[str, ...]
    scores: tuple[tuple[str, float], ...]
    resistance: Resistance
    uncertainty: Uncertainty
    confidence: float | None
    conflicting: bool
    reactance_risk: bool
    trust_high: bool


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def usable_signals(s: Signals) -> int:
    n = 0
    n += 1 if s.stage and normalize_stage(s.stage) is not Stage.UNKNOWN else 0
    n += 1 if s.intent and s.intent != "unknown" else 0
    n += 1 if _num(s.sentiment) is not None else 0
    n += sum(1 for v in (s.trigger, s.objection_signal, s.blocker, s.payment_signal, s.momentum) if isinstance(v, str) and v)
    n += 1 if _num(s.trust_signal) is not None else 0
    n += 1 if isinstance(s.contradiction, bool) else 0
    return n


def classify(s: Signals) -> Classification:
    usable = usable_signals(s)
    cues: list[str] = []
    if s.blocker in _BLOCKER_CUES:
        cues.append(_BLOCKER_CUES[s.blocker])
    if s.trigger == "fraud":
        cues.append("trigger_fraud")
    if s.trigger == "how_much":
        cues.append("trigger_how_much")
    if s.trigger in ("driving", "working"):
        cues.append("trigger_busy")
    trust = _num(s.trust_signal)
    if trust is not None and trust < LOW_TRUST:
        cues.append("trust_signal_low")
    if s.intent == "objection":
        cues.append("intent_objection")
    if s.objection_signal == "hard":
        cues.append("objection_text_markers")
    if s.stage and normalize_stage(s.stage) is Stage.OBJECTION:
        cues.append("stage_objection")
    scores: dict[Resistance, float] = {}
    for c in cues:
        r, w = CUE_WEIGHTS[c]
        scores[r] = min(1.0, scores.get(r, 0.0) + w)
    # the generic "unspecified" cues corroborate a specific cause instead of competing with it
    generic = scores.get(Resistance.UNSPECIFIED, 0.0)
    specific = {r: v for r, v in scores.items() if r is not Resistance.UNSPECIFIED and v >= MIN_SCORE}
    if specific:
        top_r = sorted(specific, key=lambda r: (-specific[r], r.value))[0]
        specific[top_r] = min(1.0, specific[top_r] + min(CORROBORATION_CAP, generic / 2))
        ranked = sorted(specific.items(), key=lambda x: (-x[1], x[0].value))
    else:
        ranked = sorted(((r, v) for r, v in scores.items() if v >= MIN_SCORE), key=lambda x: (-x[1], x[0].value))
    trust_high = trust is not None and trust >= HIGH_TRUST
    sent = _num(s.sentiment)
    reactance = bool(s.momentum == "FALLING" and sent is not None and sent <= LOW_SENTIMENT)
    conflicting = False
    if usable < MIN_USABLE or not cues:
        res, unc, conf = Resistance.UNKNOWN, Uncertainty.INSUFFICIENT_DATA, None
    else:
        if s.contradiction is True:
            conflicting = True
        if len(ranked) >= 2 and ranked[0][1] - ranked[1][1] < CONFLICT_MARGIN:
            conflicting = True
        if ranked and ranked[0][0] is Resistance.TRUST_CONCERN and trust_high:
            conflicting = True                            # the trust signal itself says the opposite
        if not ranked:
            res, unc, conf = Resistance.UNKNOWN, Uncertainty.INSUFFICIENT_DATA, None
        elif conflicting:
            res, unc, conf = Resistance.CONFLICTING, Uncertainty.CONFLICTING_SIGNALS, 0.2
        else:
            top = ranked[0][1]
            conf = round(min(MAX_CONFIDENCE, top) * min(1.0, usable / 3), 2)
            res = ranked[0][0]
            unc = Uncertainty.LIKELY if conf >= 0.6 else Uncertainty.POSSIBLE if conf >= 0.3 else Uncertainty.INSUFFICIENT_DATA
            if unc is Uncertainty.INSUFFICIENT_DATA:
                res, conf = Resistance.UNKNOWN, None
    return Classification(usable, tuple(cues), tuple((r.value, round(v, 2)) for r, v in sorted(scores.items(), key=lambda x: x[0].value)),
                          res, unc, conf, conflicting, reactance, trust_high)
