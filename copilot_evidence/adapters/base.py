"""Shared adapter plumbing: result type, safe Evidence construction, tiny parsers."""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from copilot_evidence.contracts import Evidence


@dataclass(frozen=True)
class Skipped:
    what: str
    reason: str


@dataclass(frozen=True)
class AdapterResult:
    evidence: tuple[Evidence, ...] = ()
    skipped: tuple[Skipped, ...] = ()


class Collector:
    """Accumulates Evidence/skips; one bad item never breaks the batch."""

    def __init__(self):
        self.evidence: list[Evidence] = []
        self.skipped: list[Skipped] = []

    def skip(self, what: str, reason: str) -> None:
        self.skipped.append(Skipped(what, reason))

    def add(self, what: str, **fields) -> None:
        try:
            self.evidence.append(Evidence(**fields))
        except Exception as e:  # ContractError or malformed field types
            self.skip(what, f"invalid_evidence:{type(e).__name__}:{e}"[:160])

    def result(self) -> AdapterResult:
        return AdapterResult(tuple(self.evidence), tuple(self.skipped))


def parse_dt(v: Any, assume_utc: bool = False) -> datetime | None:
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return None
    if not isinstance(v, datetime):
        return None
    if v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc) if assume_utc else None
    return v


def as_int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def num(v: Any) -> float | None:
    """Pass a numeric value through unchanged; anything else -> None (contract then decides)."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def failed(exc: Exception) -> AdapterResult:
    return AdapterResult((), (Skipped("adapter", f"adapter_error:{type(exc).__name__}"),))
