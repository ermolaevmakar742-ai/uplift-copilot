"""GEO resolver. Explicit hints and declared profile data are primary; lexical signals are
low-confidence; fallback es-MX. The resolver NEVER uses externally collected data about the
user: only the geo_hint / declared profile fields the user provided and their own messages.

Stateless on purpose: nothing is persisted, so a client is never switched "forever"
because of one or two words. Lexical evidence must be strong across the recent window.
"""
from typing import Any, List, Mapping, Optional, Sequence

from . import registry
from .config import LanguageConfig, get_config
from .detect import marker_score
from .models import GeoResolution

LEXICAL_MIN_SCORE = 3.0        # e.g. 3 strong distinct markers, never 1-2 words
LEXICAL_MIN_MARGIN = 1.5       # over the runner-up locale
LEXICAL_SWITCH_CONFIDENCE = 0.5
_UNKNOWN_REGIONS = {"", "??", "?", "UNKNOWN", "NONE", "NULL", "N/A"}


def _client_texts(history: Optional[Sequence[Mapping[str, Any]]], window: int) -> List[str]:
    out: List[str] = []
    for m in reversed(history or []):
        if (m or {}).get("role") == "user":
            c = (m or {}).get("content")
            if isinstance(c, str) and c:
                out.append(c)
            if len(out) >= window:
                break
    return out


def resolve_geo(
    declared_profile: Optional[Mapping[str, Any]] = None,
    history: Optional[Sequence[Mapping[str, Any]]] = None,
    geo_hint: Optional[str] = None,
    cfg: Optional[LanguageConfig] = None,
) -> GeoResolution:
    cfg = cfg or get_config()
    flags: List[str] = []

    hinted = registry.get(geo_hint)
    if hinted:
        return GeoResolution(hinted.code, hinted.language, "hint", 1.0)

    # 1) user-declared region (profile field the user provided themselves)
    region = None
    if isinstance(declared_profile, Mapping):
        region = declared_profile.get("region")
    region_s = str(region).strip() if region is not None else ""
    if region_s.upper() not in _UNKNOWN_REGIONS:
        code = registry.code_for_country(region_s)
        if code:
            p = registry.get(code)
            return GeoResolution(p.code, p.language, "declared", 0.9)
        flags.append("geo_unsupported:%s" % region_s.upper()[:8])

    # 2) lexical detection from the client's recent messages: low confidence, strong evidence required
    texts = _client_texts(history, cfg.lexical_window)
    if texts:
        scores = []
        for p in registry.all_profiles():
            if p.lexical_markers:
                s, n = marker_score(texts, p.lexical_markers)
                scores.append((s, n, p))
        scores.sort(key=lambda x: x[0], reverse=True)
        if scores:
            top = scores[0]
            runner = scores[1][0] if len(scores) > 1 else 0.0
            conf = min(0.7, top[0] / 6.0)
            if top[0] >= LEXICAL_MIN_SCORE and top[0] - runner >= LEXICAL_MIN_MARGIN and conf >= LEXICAL_SWITCH_CONFIDENCE:
                return GeoResolution(top[2].code, top[2].language, "lexical", round(conf, 2), tuple(flags))
            if top[0] > 0:
                flags.append("lexical_weak")

    # 3) default
    default = registry.get(cfg.default_geo) or registry.get("es-MX")
    return GeoResolution(default.code, default.language, "default", 0.3, tuple(flags))
