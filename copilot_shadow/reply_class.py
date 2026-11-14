"""Reply Classification v1 (shadow only): WHAT KIND of reply the client just sent.

Not Intent: intent says what the client wants overall (client_state.conversation.intent); the reply class says how
this one message behaves (e.g. intent=ready, reply=PRICING_OBJECTION). Deterministic and rule-based: reuses the
shared lexicons in copilot_shadow.signals (via signals.annotate) plus a few extra cues defined here. The message text
is read transiently; the output carries only category names, objection type, confidence and reason codes.
confidence is rule agreement, NOT a probability. Below MIN_CONFIDENCE the result is OTHER.
Idea of "confidence + low-confidence fallback" is borrowed from AutoSDR's reply classifier; no code or prompts reused.
"""
from copilot_shadow import signals as S

CLASSIFIER_VERSION = "reply-class-1"
MIN_CONFIDENCE = 0.5
AMBIGUITY_MARGIN = 0.05
AMBIGUITY_PENALTY = 0.15

CATEGORIES = ("POSITIVE", "MEETING", "PRICING_OBJECTION", "TECHNICAL_OBJECTION", "NOT_INTERESTED", "NOT_NOW",
              "REFERRAL", "QUESTION", "SUPPORT", "PAYMENT_PROBLEM", "TRUST_CONCERN", "OTHER")
# tie-break order when weights are equal (more specific / more costly-to-miss first)
_PRIORITY = ("NOT_INTERESTED", "PAYMENT_PROBLEM", "TRUST_CONCERN", "TECHNICAL_OBJECTION", "PRICING_OBJECTION",
             "MEETING", "REFERRAL", "NOT_NOW", "SUPPORT", "POSITIVE", "QUESTION", "OTHER")
OBJECTION_TYPE = {"PRICING_OBJECTION": "price", "TECHNICAL_OBJECTION": "technical", "TRUST_CONCERN": "trust",
                  "NOT_NOW": "timing", "NOT_INTERESTED": "disinterest", "PAYMENT_PROBLEM": "payment"}

MEETING = S._rx(["llamada", "llamame", "llamar", "videollamada", "reunion", "agendar", "agenda", "cita",
                 "hablamos por", "nos vemos", "zoom", "meet", "call", "созвон*", "встреч*", "позвони*"])
REFERRAL = S._rx(["mi amigo", "mi amiga", "mi primo", "mi prima", "mi hermano", "mi hermana", "te paso con",
                  "conoces a", "recomend*", "le interesa", "a un amigo", "referido*", "мой друг", "моя подруга",
                  "знакомому", "посоветую"])
SUPPORT = S._rx(["ayuda", "ayudame", "soporte", "no entiendo", "olvide mi contrasena", "mi cuenta", "problema con",
                 "помощь", "помоги*", "поддержк*", "не понимаю"])
PRICE_OBJ = S._rx(["muy caro", "es caro", "es mucho", "no tengo dinero", "no tengo plata", "no tengo lana",
                   "no tengo para", "no me alcanza", "sale caro", "дорого", "нет денег", "не хватает"])
PAY_FAIL = S._rx(["no se acredito", "no se refleja", "no me aparece el saldo", "rechazaron", "rechazada",
                  "me cobraron", "no llego el deposito", "no pasa el pago", "no pasa la tarjeta", "pago fallido",
                  "платеж не прошел", "не прошел платеж", "не дошли деньги"])
STOP = S._rx(["no me escribas", "borrame", "dejame de escribir", "stop", "unsubscribe", "не пиши*"])


def _add(cands: list, cat: str, w: float, reason: str) -> None:
    cands.append((cat, w, reason))


def classify(text) -> dict:
    """Classify ONE client message. Pure; returns no text."""
    t = S.norm(text)
    f = S.annotate([S.Turn("user", text if isinstance(text, str) else "")])[0] if t.strip() else None
    cands: list = []
    if f is not None:
        if STOP.search(t):
            _add(cands, "NOT_INTERESTED", 0.9, "stop_request")
        if f.refusal:
            _add(cands, "NOT_INTERESTED", 0.85, "refusal_cue")
        pay_fail = bool(PAY_FAIL.search(t))
        if pay_fail:
            _add(cands, "PAYMENT_PROBLEM", 0.9, "payment_failure_cue")
        elif (f.payment or f.pay_method) and f.tech:
            _add(cands, "PAYMENT_PROBLEM", 0.85, "payment_failure_cue")
        if f.trust:
            _add(cands, "TRUST_CONCERN", 0.8, "trust_cue")
        if f.tech and not (f.payment or f.pay_method):
            _add(cands, "TECHNICAL_OBJECTION", 0.75, "technical_cue")
        if PRICE_OBJ.search(t):
            _add(cands, "PRICING_OBJECTION", 0.8, "price_objection_cue")
        if MEETING.search(t):
            _add(cands, "MEETING", 0.75, "meeting_cue")
        if REFERRAL.search(t):
            _add(cands, "REFERRAL", 0.7, "referral_cue")
        if f.deferral and not f.refusal:
            _add(cands, "NOT_NOW", 0.75, "deferral_cue")
        if SUPPORT.search(t):
            _add(cands, "SUPPORT", 0.7, "support_cue")
        if f.strong_intent:
            _add(cands, "POSITIVE", 0.85, "strong_intent_cue")
        if f.deposit_done and not pay_fail:
            _add(cands, "POSITIVE", 0.85, "deposit_reported")
        if f.interest and not f.question:
            _add(cands, "POSITIVE", 0.65, "interest_cue")
        if f.affirm and f.words <= 3:
            _add(cands, "POSITIVE", 0.55, "short_affirmation")
        if f.question:
            _add(cands, "QUESTION", 0.65 if (f.interest or f.value or f.pay_amount or f.payment) else 0.6,
                 "question_cue")
    if not cands:
        return _out("OTHER", None, 0.3, ["no_cue"])
    best: dict = {}
    for cat, w, why in cands:
        if cat not in best or w > best[cat][0]:
            best[cat] = (w, why)
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], _PRIORITY.index(kv[0])))
    top_cat, (top_w, top_why) = ranked[0]
    reasons = [top_why]
    conf = top_w
    if len(ranked) > 1:
        sec_cat, (sec_w, _) = ranked[1]
        if top_w - sec_w <= AMBIGUITY_MARGIN:
            conf -= AMBIGUITY_PENALTY
            reasons.append("ambiguous_with:" + sec_cat)
    # objection_type: strongest objection-capable candidate that clears the threshold (may differ from the class)
    obj = next((OBJECTION_TYPE[c] for c, (w, _) in ranked if c in OBJECTION_TYPE and w >= MIN_CONFIDENCE), None)
    conf = round(max(0.0, min(0.95, conf)), 2)
    if conf < MIN_CONFIDENCE:
        return _out("OTHER", obj, conf, ["low_confidence", "suppressed:" + top_cat])
    return _out(top_cat, obj, conf, reasons)


def _out(cls: str, obj, conf: float, reasons: list) -> dict:
    return {"classification": cls, "objection_type": obj, "confidence": conf, "reason_codes": reasons,
            "classifier_version": CLASSIFIER_VERSION, "confidence_type": S.CONFIDENCE_TYPE}
