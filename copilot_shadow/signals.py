"""Shared, deterministic text/behaviour signals for the shadow analyzers.

Rule-based lexicons (es / ru / minimal en). No LLM, no randomness. All
matching runs on accent-stripped lowercase text. Raw text never leaves this
module's callers' memory: analyzers output only signal names and message ids.
"""
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

WINDOW_TURNS = 12          # last N turns considered (6 pairs)
PAUSE_HOURS = 6.0          # gap that counts as "returned after pause"
CONFIDENCE_TYPE = "rule_based_coverage"   # NOT a probability, NOT a trained model


def norm(s) -> str:
    s = unicodedata.normalize("NFKD", s if isinstance(s, str) else "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _rx(terms) -> "re.Pattern":
    parts = []
    for t in terms:
        t = norm(t)
        stem = t.endswith("*")
        t = t.rstrip("*")
        body = r"\s+".join(re.escape(w) for w in t.split())
        parts.append(body + (r"\w*" if stem else ""))
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)")


AFFIRM = _rx(["si", "dale", "vamos", "ok", "okay", "listo", "claro", "sale", "bueno", "perfecto",
              "de acuerdo", "me late", "esta bien", "да", "ок", "давай", "хорошо", "yes", "sure"])
STRONG_INTENT = _rx([
    "quiero empezar", "quiero comenzar", "quiero hacerlo", "quiero registrarme", "quiero depositar",
    "quiero entrar", "quiero probar", "como empiezo", "como empezar", "como le hago",
    "que hago primero", "donde me registro", "estoy listo", "estoy lista", "vamos a hacerlo",
    "mandame el link", "pasame el link", "хочу начать", "как начать", "готов", "готова"])
DEFERRAL = _rx(["luego", "despues", "mas tarde", "manana", "ahorita no", "lo pienso", "lo voy a pensar",
                "dejame pensar*", "ya te aviso", "te aviso", "otro dia", "en otro momento", "no ahorita",
                "потом", "позже", "завтра", "подумаю"])
REFUSAL = _rx(["no quiero", "no me interesa", "dejame en paz", "no insistas", "no gracias",
               "не хочу", "не интересно", "отстань"])
TRUST = _rx(["estafa*", "fraude*", "scam", "me van a robar", "es un robo", "no confio", "desconfio",
             "tengo miedo", "me da miedo", "es seguro", "es confiable", "es legal", "garantia*",
             "es real", "me estafaron", "мошенник*", "обман*", "развод", "боюсь", "не довер*",
             "опасно", "безопасно", "надежно"])
PAYMENT = _rx(["deposit*", "transferencia*", "transferir", "tarjeta*", "oxxo", "spei", "paypal", "usdt",
               "cripto*", "metodo* de pago", "forma* de pago", "como pago", "como pagar", "donde pago",
               "pagar con", "minimo", "comision*", "retiro*", "retirar", "banco",
               "депозит*", "оплат*", "перевод*", "карт*"])
PAY_METHOD = _rx(["metodo* de pago", "forma* de pago", "tarjeta*", "oxxo", "spei", "paypal", "usdt",
                  "cripto*", "transferencia*", "como pago", "como pagar", "como deposito",
                  "como depositar", "donde pago", "pagar con", "no tengo tarjeta", "как оплат*"])
PAY_AMOUNT = _rx(["minimo", "cuanto cuesta", "cuanto es el", "cuanto tengo que", "cuanto debo",
                  "no tengo dinero", "no tengo para", "no tengo plata", "no tengo lana", "no me alcanza",
                  "muy caro", "es mucho", "comision*", "дорого", "не хватает", "сколько нужно"])
DEPOSIT_DONE = _rx(["ya deposite", "ya hice el deposito", "ya hice mi deposito", "ya hice el pago",
                    "ya hice mi pago", "ya hice la transferencia", "acabo de depositar",
                    "acabo de transferir", "ya pague", "ya transferi", "deposito hecho",
                    "ya recargue", "я внес*", "я оплатил*", "пополнил*"])
TECH = _rx(["no puedo registrarme", "no puedo entrar", "no puedo ingresar", "no puedo abrir",
            "no puedo depositar", "no me deja", "no me llega", "no funciona", "error", "no abre",
            "no carga", "no encuentro", "no tengo cuenta", "no tengo app", "не получается",
            "не работает", "ошибка"])
NEED = _rx(["necesito", "busco", "estoy buscando", "quiero ganar", "dinero extra", "ingreso*", "deuda*",
            "me hace falta", "sin trabajo", "desempleado", "нужн*", "ищу", "хочу заработать", "заработок"])
VALUE = _rx(["cuanto gano", "cuanto ganan", "cuanto se gana", "cuanto puedo ganar", "ganancia*",
             "que gano", "resultados", "rentab*", "сколько заработ*", "сколько можно"])
INTEREST = _rx(["como funciona", "de que se trata", "que es", "cuentame", "informacion", "info",
                "me interesa", "interesante", "suena bien", "explicame", "расскажи", "как работает",
                "что это"])
BASIC_QUESTION = _rx(["que es", "como funciona", "de que se trata", "como es eso", "что это",
                      "как работает"])
CTA = _rx(["registr*", "link", "enlace", "http*", "deposit*", "empez*", "te animas", "lo hacemos",
           "lo intentamos", "quieres empezar", "quieres que", "quieres intentar", "quieres probar",
           "te paso", "entra", "ingresa", "primer deposito", "оплат*", "регистр*"])
PAY_CTA = _rx(["deposit*", "pago*", "pagar", "transferencia*", "monto", "minimo", "оплат*", "депозит*"])


@dataclass(frozen=True)
class Turn:
    role: str                 # "user" | "assistant"
    text: str
    id: int | None = None     # dialogs.id when read from DB
    ts: datetime | None = None


@dataclass
class F:
    """Feature flags of one turn. Holds no text."""
    role: str
    id: int | None
    ts: datetime | None
    words: int = 0
    question: bool = False
    affirm: bool = False
    strong_intent: bool = False
    deferral: bool = False
    refusal: bool = False
    trust: bool = False
    payment: bool = False
    pay_method: bool = False
    pay_amount: bool = False
    deposit_done: bool = False
    tech: bool = False
    need: bool = False
    value: bool = False
    interest: bool = False
    basic_question: bool = False
    cta: bool = False
    pay_cta: bool = False


def annotate(turns) -> list[F]:
    out = []
    for t in turns[-WINDOW_TURNS:]:
        s = norm(t.text)
        f = F(role=t.role, id=t.id, ts=t.ts, words=len(s.split()),
              question=("?" in s or "¿" in (t.text or "")))
        if t.role == "assistant":
            f.cta = bool(CTA.search(s))
            f.pay_cta = bool(PAY_CTA.search(s))
        else:
            f.affirm = bool(AFFIRM.search(s))
            f.strong_intent = bool(STRONG_INTENT.search(s))
            f.deferral = bool(DEFERRAL.search(s))
            f.refusal = bool(REFUSAL.search(s))
            f.trust = bool(TRUST.search(s))
            f.payment = bool(PAYMENT.search(s))
            f.pay_method = bool(PAY_METHOD.search(s))
            f.pay_amount = bool(PAY_AMOUNT.search(s))
            f.deposit_done = bool(DEPOSIT_DONE.search(s))
            f.tech = bool(TECH.search(s))
            f.need = bool(NEED.search(s))
            f.value = bool(VALUE.search(s))
            f.interest = bool(INTEREST.search(s))
            f.basic_question = bool(BASIC_QUESTION.search(s))
        out.append(f)
    return out


def last_user_index(fs: list[F]) -> int | None:
    for i in range(len(fs) - 1, -1, -1):
        if fs[i].role == "user":
            return i
    return None


def cta_before(fs: list[F], i: int) -> F | None:
    """The assistant turn immediately preceding user turn i, if it carried a CTA."""
    j = i - 1
    while j >= 0 and fs[j].role == "user":
        j -= 1
    return fs[j] if j >= 0 and fs[j].cta else None


def reaction(fs: list[F], i: int) -> str | None:
    """How user turn i reacted to a preceding CTA: engaged|objection|deferred|ignored|None."""
    if cta_before(fs, i) is None:
        return None
    f = fs[i]
    if f.refusal or f.trust:
        return "objection"
    if f.deferral:
        return "deferred"
    if f.strong_intent or f.affirm or f.payment or f.tech or f.value or f.need or f.deposit_done:
        return "engaged"
    return "ignored"


def gap_hours(a: F, b: F) -> float | None:
    if a.ts is None or b.ts is None:
        return None
    return (b.ts - a.ts).total_seconds() / 3600.0


def ev(f: F, signal: str) -> dict:
    return {"dialog_id": f.id, "ts": f.ts.isoformat() if f.ts else None, "signal": signal}
