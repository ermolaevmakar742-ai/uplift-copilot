"""Cheap deterministic detectors: language of a text and distinctive-marker matching."""
import re
from typing import Dict, Iterable, List, Mapping, Tuple

_WORD = re.compile(r"[\wáéíóúñüãõçâêô]+", re.IGNORECASE | re.UNICODE)

# Function words that discriminate es from pt (words shared by both are excluded).
_ES_ONLY = frozenset("""el los las una pero muy con y es sí qué cómo tú puedes quieres hay estás algo ahora
también nos mucho porque donde cuando entonces gracias hola dinero bueno tienes""".split())
_PT_ONLY = frozenset("""o os as uma mas muito com e é não você pode quer aqui tá né então também sim bom tudo
obrigado obrigada oi dinheiro porque""".split())
# tokens present in both lists above that are ambiguous -> neutralised
_AMBIGUOUS = frozenset({"porque"})


def tokenize(text: str) -> List[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


def detect_language(text: str) -> str:
    """'es' | 'pt' | 'unknown'. Conservative: needs a clear margin."""
    toks = [t for t in tokenize(text) if t not in _AMBIGUOUS]
    if not toks:
        return "unknown"
    es = sum(1 for t in toks if t in _ES_ONLY)
    pt = sum(1 for t in toks if t in _PT_ONLY)
    if "¿" in text or "¡" in text:
        es += 1
    if es == pt:
        return "unknown"
    hi, lo = (es, pt) if es > pt else (pt, es)
    if hi < 2 and hi - lo < 1:
        return "unknown"
    if hi - lo < 1 or (hi == 1 and len(toks) > 6):
        return "unknown"
    return "es" if es > pt else "pt"


def _marker_re(marker: str) -> "re.Pattern[str]":
    return re.compile(r"(?<![\wáéíóúñüãõç])" + re.escape(marker) + r"(?![\wáéíóúñüãõç])", re.IGNORECASE)


_cache: Dict[str, "re.Pattern[str]"] = {}


def find_markers(text: str, markers: Mapping[str, float]) -> Dict[str, float]:
    """Distinct markers present in text -> weight."""
    found: Dict[str, float] = {}
    low = (text or "").lower()
    for m, w in markers.items():
        rx = _cache.get(m)
        if rx is None:
            rx = _cache[m] = _marker_re(m)
        if rx.search(low):
            found[m] = w
    return found


def marker_score(texts: Iterable[str], markers: Mapping[str, float]) -> Tuple[float, int]:
    """Each distinct marker counts once across all texts. Returns (score, distinct_count)."""
    seen: Dict[str, float] = {}
    for t in texts:
        seen.update(find_markers(t, markers))
    return sum(seen.values()), len(seen)
