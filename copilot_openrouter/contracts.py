"""Contracts. `OpenRouterKeyStatus` is the ONLY shape that leaves the package: normalised, safe, immutable. No raw provider response, no key."""
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum

CONTRACT_VERSION = 2
PROVIDER = "openrouter"


class KeyState(str, Enum):
    HEALTHY = "HEALTHY"
    LIMITED = "LIMITED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNAUTHORIZED = "UNAUTHORIZED"
    RATE_LIMITED = "RATE_LIMITED"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


@dataclass(frozen=True)
class OpenRouterKeyStatus:
    slot: str
    configured: bool
    status: str
    balance: float | None = None            # ACCOUNT credits left = total_credits - total_usage (from /credits); None = not reported (never a fake 0, never limit_remaining)
    limit: float | None = None
    limit_remaining: float | None = None    # per-KEY cap remaining; None when the key has no limit
    total_credits: float | None = None
    total_usage: float | None = None
    usage_daily: float | None = None
    usage_weekly: float | None = None
    usage_monthly: float | None = None
    retrieved_at: datetime | None = None
    error_code: str | None = None
    error_message_safe: str | None = None
    balance_source: str | None = None       # "credits" when balance came from /api/v1/credits, else None
    balance_error_code: str | None = None   # why balance is unknown while the key itself is fine (e.g. credits_forbidden)
    provider: str = PROVIDER
    contract_version: int = CONTRACT_VERSION

    def to_dict(self) -> dict:
        d = asdict(self)
        d["retrieved_at"] = self.retrieved_at.isoformat() if self.retrieved_at else None
        return d


class FetchError(Exception):
    """Safe machine code only (never a body, URL with credentials, or key)."""
    def __init__(self, state: KeyState, code: str):
        super().__init__(code)
        self.state, self.code = state, code
