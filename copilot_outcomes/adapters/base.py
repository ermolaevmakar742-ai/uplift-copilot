"""Shared Outcome-adapter plumbing. Adapters are pure: they read a mapping (or an object with the same
attributes), never mutate it, never touch time/network/LLM/DB, and skip (with a reason) instead of guessing."""
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from copilot_outcomes.contracts import Outcome


@dataclass(frozen=True)
class Skipped:
    what: str
    reason: str


@dataclass(frozen=True)
class AdapterResult:
    outcomes: tuple[Outcome, ...] = ()
    skipped: tuple[Skipped, ...] = ()


def skip(what: str, reason: str) -> AdapterResult:
    return AdapterResult((), (Skipped(what, reason),))


def make(what: str, **fields) -> AdapterResult:
    """Build one Outcome through the contract; any validation failure becomes a skip."""
    try:
        return AdapterResult((Outcome(**fields),), ())
    except Exception as e:
        return skip(what, f"invalid_outcome:{type(e).__name__}:{e}"[:160])


def get(data: Any, key: str) -> Any:
    if isinstance(data, dict) or hasattr(data, "keys"):
        return data.get(key)
    return getattr(data, key, None)


def as_int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def unix_ts(v: Any) -> datetime | None:
    """Source UNIX timestamp (int, > 0) -> aware UTC datetime. Anything else -> None (never 'now')."""
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        return None
    try:
        return datetime.fromtimestamp(v, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def aware_dt(v: Any) -> datetime | None:
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return None
    return v if isinstance(v, datetime) and v.tzinfo is not None else None


def amount(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        return None
    return v


def stable_id(v: Any) -> str | None:
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, str) and v.strip() and len(v) <= 128 and not any(c.isspace() for c in v):
        return v
    return None
