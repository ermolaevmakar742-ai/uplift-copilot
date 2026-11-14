"""Data models. Profiles are declarative; adding a GEO means adding one profile file."""
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Tuple


@dataclass(frozen=True)
class ForbiddenRule:
    id: str
    pattern: str          # regex, matched case-insensitively
    reason: str


@dataclass(frozen=True)
class GeoProfile:
    code: str                                   # "es-CO"
    language: str                               # "es" | "pt"
    label: str
    country_codes: Tuple[str, ...] = ()         # ISO-2 + common names, upper-case
    prompt_override: bool = True                # False -> base persona already is this locale
    informal_pronoun: str = ""
    formal_pronoun: str = ""
    address_rule: str = ""
    vocabulary: Mapping[str, str] = field(default_factory=dict)   # concept -> preferred wording
    patterns: Tuple[str, ...] = ()              # common conversational patterns
    forbidden: Tuple[ForbiddenRule, ...] = ()   # unnatural for this locale (flag-only)
    cta: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    tone_rules: Tuple[str, ...] = ()
    examples: Tuple[str, ...] = ()
    lexical_markers: Mapping[str, float] = field(default_factory=dict)  # distinctive words -> weight

    def derive(self, **overrides: Any) -> "GeoProfile":
        """New profile inheriting every field not overridden (es-UY from es-AR, etc.)."""
        return replace(self, **overrides)


@dataclass(frozen=True)
class GeoResolution:
    code: str
    language: str
    source: str            # declared | hint | lexical | default
    confidence: float
    flags: Tuple[str, ...] = ()


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    violations: Tuple[str, ...] = ()


@dataclass
class LocalizationRequest:
    text: str
    stage: Optional[str] = None
    regime: Optional[str] = None
    history: Optional[List[Mapping[str, Any]]] = None     # [{"role","content"}, ...]
    declared_profile: Optional[Mapping[str, Any]] = None  # user-provided profile fields (region, language) — first-party only
    geo_hint: Optional[str] = None                        # explicit profile code, wins over resolver


@dataclass
class LocalizationResult:
    original: str
    text: str                       # what would be sent (== original when reverted/unchanged)
    geo_profile: str
    language: str                   # target language
    detected_language: str          # language the reply actually looks like
    geo_source: str
    confidence: float
    flags: List[str] = field(default_factory=list)
    meaning_guard: Dict[str, Any] = field(default_factory=dict)
    reverted: bool = False
    changed: bool = False
    repair_suggested: bool = False
    policy_block_chars: int = 0
