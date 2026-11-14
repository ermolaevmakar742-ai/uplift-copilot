"""Contracts. No secret value is ever stored here: a slot only knows the NAME of the env var that would hold its key and whether it is present."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Slot(str, Enum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    EMERGENCY = "EMERGENCY"


ORDER = (Slot.PRIMARY, Slot.SECONDARY, Slot.EMERGENCY)


class FailureKind(str, Enum):
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    PROVIDER_5XX = "PROVIDER_5XX"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    OVERLOAD = "OVERLOAD"
    LATENCY_THRESHOLD = "LATENCY_THRESHOLD"     # a slow SUCCESS: informs health for NEW requests, never re-runs the request
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class Health(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    OVERLOADED = "OVERLOADED"
    UNAVAILABLE = "UNAVAILABLE"

    @property
    def routable(self) -> bool:
        return self in (Health.HEALTHY, Health.DEGRADED)


class Shared(str, Enum):
    """Do two keys of this provider share one rate-limit pool? Unknown must never be treated as extra capacity."""
    SHARED = "true"
    INDEPENDENT = "false"
    UNKNOWN = "unknown"


class ProviderError(Exception):
    def __init__(self, kind: FailureKind, detail: str = ""):
        super().__init__(f"{kind.value}: {detail}")
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class SlotConfig:
    slot: Slot
    provider: str | None
    model: str | None            # None = inherit the model chosen by the existing selection for this request (same-model by construction)
    key_slot: str                # credential slot identifier, e.g. "primary"; must be unique per slot
    key_env: str                 # NAME of the env var that would hold the key (never the value)
    key_present: bool            # presence only
    enabled: bool = True
    timeout_s: float = 90.0
    max_concurrency: int = 10


@dataclass(frozen=True)
class Thresholds:
    degraded_latency_ms: int = 8000
    overloaded_latency_ms: int = 20000
    degraded_error_rate: float = 0.25
    unavailable_error_rate: float = 0.6
    min_events: int = 4
    consecutive_failures_unavailable: int = 3
    rate_limit_cooldown_s: float = 30.0
    unavailable_cooldown_s: float = 60.0
    window_events: int = 20
    window_s: float = 120.0


@dataclass(frozen=True)
class RouterConfig:
    router_enabled: bool = False
    failover_enabled: bool = False
    dry_run: bool = True
    slots: tuple[SlotConfig, ...] = ()
    limits_shared_across_keys: Shared = Shared.UNKNOWN
    emergency_allow_different_model: bool = False
    thresholds: Thresholds = field(default_factory=Thresholds)

    def slot(self, s: Slot) -> SlotConfig | None:
        return next((c for c in self.slots if c.slot is s), None)

    @property
    def capacity_scaling_guaranteed(self) -> bool:
        """Two keys give more capacity only when the provider says limits are per key. unknown / shared => NOT guaranteed."""
        return self.limits_shared_across_keys is Shared.INDEPENDENT


@dataclass(frozen=True)
class RoutingDecision:
    slot: Slot | None            # None = no slot can run the request (controlled failure)
    reason: str
    use_router: bool             # False = router disabled: the caller keeps the legacy path untouched
    model: str | None = None
    provider: str | None = None
    key_slot: str | None = None


@dataclass(frozen=True)
class Attempt:
    slot: Slot
    ok: bool
    latency_ms: int | None
    failure: FailureKind | None = None


@dataclass(frozen=True)
class LedgerMetadata:
    """Future Decision Ledger metadata (goes into the existing `metadata` JSON; no schema change). Not wired in phase 1."""
    llm_provider: str | None
    llm_model: str | None
    llm_key_slot: str | None
    fallback_used: bool
    fallback_reason: str | None
    primary_latency_ms: int | None
    secondary_latency_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        return {"llm_provider": self.llm_provider, "llm_model": self.llm_model, "llm_key_slot": self.llm_key_slot, "fallback_used": self.fallback_used,
                "fallback_reason": self.fallback_reason, "primary_latency_ms": self.primary_latency_ms, "secondary_latency_ms": self.secondary_latency_ms}


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    value: Any
    slot: Slot | None
    attempts: tuple[Attempt, ...]
    failover_used: bool
    reason: str
    metadata: LedgerMetadata
