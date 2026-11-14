"""Response Critic dialogue-critic-1: deterministic, rule-based, shadow only. NOT ground truth, NOT an LLM.

critique(...) reads the response / client text TRANSIENTLY and returns only scores, reason codes, a confidence and a template-based
candidate. Nothing it returns contains client or response text (the original response is referenced by hash + length).
Scores: 0..100 per dimension, HIGHER IS BETTER (pressure=100 means no pressure, response_risk=100 means no risk). Every
reason code has explicit per-dimension effects (REASON_EFFECTS) so a score can always be explained.
A heavier LLM critic would belong in an offline batch pass with its own timeout; this online path stays pure CPU."""
import re

from copilot_learning import contract as C
from copilot_shadow import signals as S

BASELINE = 70
MIN_CONFIDENCE = 0.5            # below this the critique is flagged LOW_CONFIDENCE and unusable for patterns/examples
MAX_WORDS = 90                  # TOO_LONG threshold for a chat message
REPEAT_JACCARD = 0.8
OBJECTION_CLASSES = ("PRICING_OBJECTION", "TECHNICAL_OBJECTION", "TRUST_CONCERN", "NOT_NOW", "NOT_INTERESTED", "PAYMENT_PROBLEM")
EARLY_STAGES = ("new", "visited", "click", "conversation", "registration_started")
POST_STAGES = ("deposited", "converted", "deposit")
ACK = S._rx(["entiendo", "comprendo", "tienes razon", "es normal", "lamento", "gracias por", "claro que", "понимаю", "сожалею"])
PRESSURE = S._rx(["ahora mismo", "ya mismo", "ultima oportunidad", "no pierdas", "urgente", "solo hoy", "hoy mismo",
                  "date prisa", "ultimo lugar", "se acaba", "срочно", "последний шанс"])
Q_TOPIC = {"TRUST": S._rx(["confi*", "segur*", "tranquil*"]), "PAYMENT": S._rx(["pag*", "deposit*", "metodo*", "monto", "tarjeta*"]),
           "NEED": S._rx(["necesit*", "busc*", "lograr", "objetivo", "para que"]), "VALUE": S._rx(["gan*", "esper*", "resultado*"]),
           "OBJECTION": S._rx(["frena*", "preocup*", "detiene*", "pasa"]), "READINESS": S._rx(["list*", "cuando", "momento", "empez*"]),
           "TECHNICAL": S._rx(["error", "problema", "paso", "intent*"])}
STOP = frozenset("de la el en y a que es un una lo por con para me te se mi tu su al del las los si no ya pero como mas muy".split())

# reason -> {dimension: delta}; explicit and auditable
REASON_EFFECTS = {
    "TOO_EARLY_OFFER": {"stage_fit": -30, "pressure": -15, "response_risk": -20, "state_alignment": -10},
    "WRONG_STAGE": {"stage_fit": -35, "state_alignment": -20, "response_risk": -10},
    "IGNORED_OBJECTION": {"objection_fit": -40, "relevance": -15, "trust": -15, "response_risk": -15},
    "NO_QUESTION": {"question_quality": -30},
    "REPETITIVE": {"clarity": -25, "relevance": -10, "trust": -10},
    "TOO_LONG": {"clarity": -25},
    "TOO_PUSHY": {"pressure": -40, "trust": -20, "response_risk": -20},
    "GOOD_STATE_ALIGNMENT": {"state_alignment": 20, "stage_fit": 10},
    "GOOD_OBJECTION_HANDLING": {"objection_fit": 25, "trust": 15, "relevance": 10},
    "GOOD_NEXT_QUESTION": {"question_quality": 25, "relevance": 10},
}
CANDIDATE_TEMPLATES = {      # template text only: no client data, always flagged for human review
    "ASK_NEED_CLARIFICATION": "¿Qué es lo que más te gustaría lograr con esto?",
    "ASK_TRUST_CONCERN": "¿Qué te daría más tranquilidad para sentirte seguro con esto?",
    "ASK_PAYMENT_BARRIER": "¿Qué te preocupa más del pago: el método o el monto?",
    "ASK_VALUE_EXPECTATION": "¿Qué resultado esperas para que valga la pena para ti?",
    "ASK_OBJECTION_DETAIL": "¿Qué es lo que más te frena en este momento?",
    "ASK_READINESS": "¿Cuándo te sentirías listo para dar el siguiente paso?",
    "ASK_TECHNICAL_BLOCKER": "¿En qué paso exacto te aparece el problema?",
}
REMEDY = {"NO_QUESTION": "append_question", "TOO_EARLY_OFFER": "replace_offer_with_question",
          "WRONG_STAGE": "replace_offer_with_question", "IGNORED_OBJECTION": "acknowledge_then_ask",
          "TOO_PUSHY": "remove_pressure", "TOO_LONG": "shorten", "REPETITIVE": "vary_wording"}


def _tokens(text) -> frozenset:
    return frozenset(w for w in S.norm(text).replace("?", " ").replace("¿", " ").split() if len(w) > 2 and w not in STOP)


def _jaccard(a, b) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


_SENT = re.compile(r"(?<=[.!?])\s+")


def _statements(resp: str) -> str:
    """Declarative sentences only: a question that mentions 'pago' is not a payment call-to-action."""
    return " ".join(x for x in _SENT.split(resp) if "?" not in x)


def _features(resp: str, prev_assistant) -> dict:
    n = S.norm(_statements(resp))
    n_all = S.norm(resp)
    words = len(n_all.split())
    toks = _tokens(resp)
    questions = [q for q in resp.replace("¿", "").split("?")[:-1]] if "?" in resp else []
    return {"words": words, "has_question": "?" in resp, "cta": bool(S.CTA.search(n)), "pay_cta": bool(S.PAY_CTA.search(n)),
            "pressure": bool(PRESSURE.search(n_all)), "cta_count": len(S.CTA.findall(n)), "ack": bool(ACK.search(n)),
            "repeat": any(_jaccard(toks, _tokens(p)) >= REPEAT_JACCARD for p in (prev_assistant or ())[-3:]),
            "question_text": S.norm(questions[-1]) if questions else ""}


def critique(*, response_text, client_text=None, prev_assistant=(), stage=None, reply_classification=None,
             next_best_question=None, state_ref=None, decision_id=None) -> dict:
    """Pure. Inputs are read, never stored or mutated. Returns the critic record (no raw text)."""
    resp = response_text if isinstance(response_text, str) else ""
    rc = reply_classification or {}
    cls, rconf = rc.get("classification"), rc.get("confidence") or 0.0
    st = stage.strip().lower() if isinstance(stage, str) and stage.strip() else None
    f = _features(resp, prev_assistant)
    negative, positive, evaluated = [], [], set()

    if resp.strip():
        evaluated |= {"clarity"}
        if f["words"] > MAX_WORDS:
            negative.append("TOO_LONG")
        if f["repeat"]:
            negative.append("REPETITIVE")
        if f["pressure"] or f["cta_count"] >= 2:
            negative.append("TOO_PUSHY")
        evaluated |= {"pressure", "response_risk"}
        if not f["has_question"] and st not in POST_STAGES:
            negative.append("NO_QUESTION")
        evaluated.add("question_quality")
    if st is not None and resp.strip():
        evaluated |= {"stage_fit", "state_alignment"}
        resisting = cls in OBJECTION_CLASSES and rconf >= 0.6
        if f["cta"] and st in EARLY_STAGES and (resisting or cls in (None, "OTHER", "QUESTION", "SUPPORT")):
            negative.append("TOO_EARLY_OFFER")
        if (f["pay_cta"] and st in EARLY_STAGES) or (f["cta"] and st in POST_STAGES):
            negative.append("WRONG_STAGE")
        if not {"TOO_EARLY_OFFER", "WRONG_STAGE"} & set(negative):
            positive.append("GOOD_STATE_ALIGNMENT")
    if cls in OBJECTION_CLASSES and rconf >= 0.6 and resp.strip():
        evaluated |= {"objection_fit", "relevance"}
        if f["ack"]:
            positive.append("GOOD_OBJECTION_HANDLING")
        elif f["cta"]:
            negative.append("IGNORED_OBJECTION")
    nbq = next_best_question or {}
    target = nbq.get("target_unknown")
    if f["has_question"] and nbq.get("recommended") and target in Q_TOPIC and Q_TOPIC[target].search(f["question_text"]):
        positive.append("GOOD_NEXT_QUESTION")
    if client_text and resp.strip():
        evaluated.add("relevance")
        evaluated.add("trust") if (f["ack"] or "IGNORED_OBJECTION" in negative) else None

    reasons = list(dict.fromkeys(negative + positive))
    scores = {d: BASELINE for d in C.DIMENSIONS if d != "overall_quality"}
    for r in reasons:
        for dim, delta in REASON_EFFECTS[r].items():
            scores[dim] += delta
    scores = {d: max(0, min(100, v)) for d, v in scores.items()}
    rated = [d for d in scores if d in evaluated] or []
    overall = round(sum(scores[d] for d in rated) / len(rated)) if rated else None
    scores["overall_quality"] = overall
    confidence = round(len(rated) / len(scores), 2) if rated else 0.0
    usable = confidence >= MIN_CONFIDENCE and overall is not None
    if not usable:
        reasons.append("LOW_CONFIDENCE")
    return {"critic_version": C.CRITIC_VERSION, "decision_id": decision_id, "state_ref": state_ref, "stage": st,
            "reply_class": cls, "scores": scores, "reasons": reasons, "confidence": confidence, "usable": usable,
            "confidence_type": "rule_based_coverage", "evaluated_dimensions": sorted(rated),
            "response_ref": C.response_ref(resp), "response_features": {k: f[k] for k in ("words", "has_question", "cta", "pressure", "ack", "repeat")},
            "candidate_better_response": candidate(reasons, nbq, decision_id, state_ref, st, C.response_ref(resp)) if usable else None}


def candidate(reasons, nbq, decision_id, state_ref, stage, original_ref):
    """Shadow artifact ONLY (never sent): a TEMPLATE-based suggestion tied to the decision. Contains no client or original text."""
    neg = [r for r in reasons if r in REMEDY]
    intent = (nbq or {}).get("question_intent")
    if not neg or intent not in CANDIDATE_TEMPLATES:
        return None
    remedy = REMEDY[neg[0]]
    return {"critic_version": C.CRITIC_VERSION, "decision_id": decision_id, "state_ref": state_ref, "stage": stage,
            "remedy": remedy, "template_id": intent, "text": CANDIDATE_TEMPLATES[intent], "reason_codes": neg,
            "original_response_ref": original_ref, "shadow_only": True, "needs_human_review": True}
