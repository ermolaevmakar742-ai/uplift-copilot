"""Meaning Guard: critical tokens must survive any change byte-for-byte."""
import re
from collections import Counter
from .models import GuardResult

_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"']+|\b[\w.-]+\.(?:com|net|org|io|app|me|co|bet|br|mx)(?:/[^\s<>\"']*)?\b", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{3,}")
_MONEY = re.compile(r"(?:R\$|S/\.?|US\$|\$|€)\s?\d[\d.,]*|\d[\d.,]*\s?(?:USD|MXN|COP|PEN|BRL|USDT|EUR|pesos|soles|reales|dólares|dolares|dólar|dolar|reais|mil|k|%)\b|\d[\d.,]*%", re.I)
_NUMBER = re.compile(r"\d[\d.,:/-]*\d|\d")
_CODE = re.compile(r"\b(?=[A-Z0-9_-]*\d)[A-Z0-9][A-Z0-9_-]{3,}\b|\b[A-Z]{4,}\b")
_CURRENCY = re.compile(r"\b(?:USD|MXN|COP|PEN|BRL|USDT|EUR)\b")


def protected_tokens(text: str) -> Counter:
    """Multiset of tokens whose change would alter meaning/offer/CTA target."""
    t = text or ""
    c: Counter = Counter()
    for kind, rx in (("url", _URL), ("email", _EMAIL), ("handle", _HANDLE), ("money", _MONEY),
                     ("number", _NUMBER), ("code", _CODE), ("cur", _CURRENCY)):
        for m in rx.findall(t):
            c[(kind, m.strip().rstrip(".,"))] += 1
    return c


def check(original: str, candidate: str) -> GuardResult:
    """ok == candidate preserves every protected token and the question/statement shape."""
    if candidate == original:
        return GuardResult(True, ())
    before, after = protected_tokens(original), protected_tokens(candidate)
    violations = []
    for tok in sorted((before - after).keys()):
        violations.append("missing:%s:%s" % tok)
    for tok in sorted((after - before).keys()):
        violations.append("added:%s:%s" % tok)
    if ("?" in (original or "")) != ("?" in (candidate or "")):
        violations.append("shape:question_changed")
    if not (candidate or "").strip() and (original or "").strip():
        violations.append("empty_result")
    return GuardResult(not violations, tuple(violations))
