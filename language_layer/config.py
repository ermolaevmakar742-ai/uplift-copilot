"""Feature flags for the language layer. Read once from the environment.

LANGUAGE_LAYER           false (default) -> layer never touches prompt or reply.
LANGUAGE_LAYER_SHADOW    false (default) -> true: compute + log only, client output untouched.
LANGUAGE_LAYER_REPAIR    false (default) -> reserved for a future one-shot repair call (not wired).
LANGUAGE_LAYER_DEFAULT_GEO  es-MX (default) fallback locale.
"""
import os
from dataclasses import dataclass

_TRUE = {"1", "true", "yes", "on"}


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


@dataclass(frozen=True)
class LanguageConfig:
    enabled: bool = False
    shadow: bool = False
    repair: bool = False
    default_geo: str = "es-MX"
    lexical_window: int = 8

    @property
    def active(self) -> bool:
        """True only when the layer may change prompt/reply."""
        return self.enabled

    @property
    def observing(self) -> bool:
        """True when anything should be computed at all."""
        return self.enabled or self.shadow

    @classmethod
    def from_env(cls) -> "LanguageConfig":
        return cls(
            enabled=_flag("LANGUAGE_LAYER", False),
            shadow=_flag("LANGUAGE_LAYER_SHADOW", False),
            repair=_flag("LANGUAGE_LAYER_REPAIR", False),
            default_geo=(os.getenv("LANGUAGE_LAYER_DEFAULT_GEO") or "es-MX").strip() or "es-MX",
        )


_cached = None


def get_config() -> LanguageConfig:
    global _cached
    if _cached is None:
        _cached = LanguageConfig.from_env()
    return _cached


def reset_config_cache() -> None:  # tests
    global _cached
    _cached = None
