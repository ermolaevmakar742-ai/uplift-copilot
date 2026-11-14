"""Profile registry. New GEOs register themselves; the core never changes."""
from typing import Dict, List, Optional

from .models import GeoProfile

_PROFILES: Dict[str, GeoProfile] = {}
_COUNTRY_INDEX: Dict[str, str] = {}


def register(profile: GeoProfile) -> GeoProfile:
    _PROFILES[profile.code] = profile
    for c in profile.country_codes:
        _COUNTRY_INDEX[c.upper()] = profile.code
    return profile


def get(code: Optional[str]) -> Optional[GeoProfile]:
    if not code:
        return None
    return _PROFILES.get(code)


def supported() -> List[str]:
    return sorted(_PROFILES)


def all_profiles() -> List[GeoProfile]:
    return [_PROFILES[c] for c in supported()]


def code_for_country(region: Optional[str]) -> Optional[str]:
    if not region:
        return None
    return _COUNTRY_INDEX.get(str(region).strip().upper())
