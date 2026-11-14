"""Minimal, safe deterministic layer. NOT a rewriter.

* safe_cleanup(): whitespace / repeated-punctuation hygiene only.
* validate(): FLAGS obvious unnatural constructions, foreign-locale markers, language
  mismatch, over-politeness and repeated templates. It never rewrites text.
Real localization happens inside the existing LLM call via the language policy prompt.
"""
import re
from difflib import SequenceMatcher
from typing import Any, List, Mapping, Optional, Sequence

from . import registry
from .detect import detect_language, find_markers, tokenize
from .models import GeoProfile

_SPACE_RUN = re.compile(r"[ \t ]{2,}")
_NL_RUN = re.compile(r"\n{3,}")
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([,;!?])")
_REPEAT_QE = re.compile(r"([!?])\1+")
_DOTS = re.compile(r"\.{4,}")
_POLITE = re.compile(r"\b(por favor|gracias|disculpa|disculpe|perdón|lamento|con gusto|obrigad[oa]|desculpa|por gentileza)\b", re.I)


def safe_cleanup(text: str) -> str:
    if not text:
        return text
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = _SPACE_RUN.sub(" ", t)
    t = _SPACE_BEFORE_PUNCT.sub(r"\1", t)
    t = _REPEAT_QE.sub(r"\1", t)
    t = _DOTS.sub("...", t)
    t = _NL_RUN.sub("\n\n", t)
    return t.strip()


def _recent_bot_messages(history: Optional[Sequence[Mapping[str, Any]]], n: int = 3) -> List[str]:
    out: List[str] = []
    for m in reversed(history or []):
        if (m or {}).get("role") in ("assistant", "bot"):
            c = (m or {}).get("content")
            if isinstance(c, str) and c:
                out.append(c)
            if len(out) >= n:
                break
    return out


def _norm(s: str) -> str:
    return " ".join(tokenize(s))


def validate(
    text: str,
    profile: GeoProfile,
    history: Optional[Sequence[Mapping[str, Any]]] = None,
) -> List[str]:
    flags: List[str] = []
    if not text:
        return flags

    for rule in profile.forbidden:
        if re.search(rule.pattern, text, re.I):
            flags.append("forbidden:%s" % rule.id)

    # distinctive markers of OTHER locales (strong ones only)
    for other in registry.all_profiles():
        if other.code == profile.code:
            continue
        strong = {m: w for m, w in other.lexical_markers.items() if w >= 1.0}
        if strong and find_markers(text, strong):
            flags.append("foreign_marker:%s" % other.code)

    detected = detect_language(text)
    if detected != "unknown" and detected != profile.language:
        flags.append("lang_mismatch:%s->%s" % (detected, profile.language))

    if len(_POLITE.findall(text)) >= 3:
        flags.append("over_polite")

    cur = _norm(text)
    for prev in _recent_bot_messages(history):
        p = _norm(prev)
        if not p or not cur:
            continue
        if cur == p or SequenceMatcher(None, cur, p).ratio() >= 0.85:
            flags.append("repeat_template")
            break
        a, b = cur.split()[:3], p.split()[:3]
        if len(a) == 3 and a == b:
            flags.append("repeat_opening")
            break
    return flags
