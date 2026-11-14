"""Public entry points. Fail-safe: any internal error returns the input untouched."""
import hashlib
import json
import logging
from typing import Any, Mapping, Optional, Sequence

from . import profiles as _profiles  # noqa: F401  (registers built-in profiles)
from . import registry
from .antitranslationese import safe_cleanup, validate
from .config import LanguageConfig, get_config
from .detect import detect_language
from .geo import resolve_geo
from .guard import check
from .models import LocalizationRequest, LocalizationResult
from .prompt import build_policy_block

log = logging.getLogger("bot.language")
_TRUNC = 300


def localize(req: LocalizationRequest, cfg: Optional[LanguageConfig] = None) -> LocalizationResult:
    """Pure computation, no side effects. Zero LLM calls."""
    cfg = cfg or get_config()
    original = req.text or ""
    res = resolve_geo(req.declared_profile, req.history, req.geo_hint, cfg)
    profile = registry.get(res.code) or registry.get("es-MX")

    candidate = safe_cleanup(original)
    guard = check(original, candidate)
    flags = list(res.flags)
    reverted = False
    if not guard.ok:
        candidate, reverted = original, True
        flags.append("meaning_guard_revert")

    flags.extend(validate(original, profile, req.history))
    policy = build_policy_block(profile, res, req.stage, req.regime, req.history)
    detected = detect_language(original)
    repair = any(f.startswith("lang_mismatch") for f in flags) or sum(f.startswith("forbidden:") for f in flags) >= 2

    return LocalizationResult(
        original=original, text=candidate, geo_profile=profile.code, language=profile.language,
        detected_language=detected, geo_source=res.source, confidence=res.confidence, flags=flags,
        meaning_guard={"ok": guard.ok, "violations": list(guard.violations)},
        reverted=reverted, changed=(candidate != original), repair_suggested=repair,
        policy_block_chars=len(policy),
    )


def apply_prompt(
    system: str,
    stage: Optional[str] = None,
    regime: Optional[str] = None,
    history: Optional[Sequence[Mapping[str, Any]]] = None,
    declared_profile: Optional[Mapping[str, Any]] = None,
    cfg: Optional[LanguageConfig] = None,
) -> str:
    """Append the LANGUAGE POLICY block. Identity unless LANGUAGE_LAYER is enabled."""
    try:
        cfg = cfg or get_config()
        if not cfg.active:
            return system
        res = resolve_geo(declared_profile, history, None, cfg)
        profile = registry.get(res.code)
        block = build_policy_block(profile, res, stage, regime, history) if profile else ""
        return system + block if block else system
    except Exception:  # never break generation
        log.exception("language_layer.apply_prompt failed; prompt unchanged")
        return system


def _log(cfg: LanguageConfig, r: LocalizationResult, user_ref: Any, stage: Any, regime: Any) -> None:
    rec = {
        "v": 1,
        "mode": "active" if cfg.active else "shadow",
        "user": hashlib.sha256(str(user_ref).encode()).hexdigest()[:10],
        "stage": stage, "regime": regime,
        "geo": r.geo_profile, "language": r.language, "detected_language": r.detected_language,
        "geo_source": r.geo_source, "confidence": r.confidence,
        "flags": r.flags, "meaning_guard": r.meaning_guard,
        "reverted": r.reverted, "changed": r.changed, "repair_suggested": r.repair_suggested,
        "policy_block_chars": r.policy_block_chars,
        "original": r.original[:_TRUNC],
        "localized": (r.text[:_TRUNC] if r.changed else None),
    }
    log.info("LANGLAYER %s", json.dumps(rec, ensure_ascii=False))


def process_reply(
    reply: str,
    user_ref: Any = None,
    stage: Optional[str] = None,
    regime: Optional[str] = None,
    history: Optional[Sequence[Mapping[str, Any]]] = None,
    declared_profile: Optional[Mapping[str, Any]] = None,
    cfg: Optional[LanguageConfig] = None,
) -> str:
    """Returns the text to send. With defaults (flag off) it returns `reply` itself, doing no work."""
    try:
        cfg = cfg or get_config()
        if not cfg.observing or not isinstance(reply, str):
            return reply
        r = localize(LocalizationRequest(reply, stage, regime, history, declared_profile), cfg)
        _log(cfg, r, user_ref, stage, regime)
        return r.text if cfg.active else reply
    except Exception:
        log.exception("language_layer.process_reply failed; reply unchanged")
        return reply
