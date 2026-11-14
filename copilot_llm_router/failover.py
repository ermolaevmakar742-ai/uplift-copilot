"""Request execution with ONE controlled failover. The provider call is INJECTED (`call(slot_config, model) -> (value, latency_ms|None)` or a raised
error), so this module performs no network I/O and knows nothing about what is being asked.

  attempt 1 on the routed slot -> failure -> (failover enabled) attempt 2 on the next eligible slot -> failure -> controlled failure.
Never a third attempt, never a loop. A request already running is never moved between keys: health changes only affect NEW requests.
Capacity is reserved synchronously in choose+acquire (no await between), so concurrent requests see each other's load."""
import asyncio
from typing import Any, Awaitable, Callable

from copilot_llm_router.contracts import Attempt, ExecutionResult, FailureKind, LedgerMetadata, ProviderError, Slot, SlotConfig
from copilot_llm_router.router import LLMRouter

MAX_ATTEMPTS = 2
FAILOVER_ON = frozenset(FailureKind) - {FailureKind.LATENCY_THRESHOLD}     # a slow SUCCESS is not a failure
CallFn = Callable[[SlotConfig, "str | None"], Awaitable[tuple]]


def classify(exc: BaseException) -> FailureKind:
    """Duck-typed (no provider library imported): ProviderError, asyncio/builtin timeouts, `status`/`status_code` attributes, connection-like class names."""
    if isinstance(exc, ProviderError):
        return exc.kind
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return FailureKind.TIMEOUT
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        if status == 429:
            return FailureKind.RATE_LIMIT
        if status == 529:
            return FailureKind.OVERLOAD
        if 500 <= status < 600:
            return FailureKind.PROVIDER_5XX
    name = type(exc).__name__.lower()
    if "connect" in name or isinstance(exc, (ConnectionError, OSError)):
        return FailureKind.CONNECTION_ERROR
    return FailureKind.UNKNOWN_ERROR


def _meta(attempts: list[Attempt], cfgs: dict, model, fallback_reason) -> LedgerMetadata:
    final = next((a for a in reversed(attempts) if a.ok), attempts[-1] if attempts else None)
    cfg = cfgs.get(final.slot) if final else None
    lat = lambda s: next((a.latency_ms for a in attempts if a.slot is s), None)
    used_fallback = bool(final and final.slot is not Slot.PRIMARY)
    return LedgerMetadata(llm_provider=cfg.provider if cfg else None, llm_model=(cfg.model if cfg and cfg.model else model),
                          llm_key_slot=cfg.key_slot if cfg else None, fallback_used=used_fallback,
                          fallback_reason=fallback_reason if used_fallback else None,
                          primary_latency_ms=lat(Slot.PRIMARY), secondary_latency_ms=lat(Slot.SECONDARY))


async def execute(router: LLMRouter, call: CallFn, *, request_model: str | None = None) -> ExecutionResult:
    cfg = router.config
    cfgs = {c.slot: c for c in cfg.slots}
    if not cfg.router_enabled:
        return ExecutionResult(False, None, None, (), False, "router_disabled", _meta([], cfgs, request_model, None))   # the caller keeps the legacy path
    attempts: list[Attempt] = []
    tried: set[Slot] = set()
    fallback_reason: str | None = None
    for n in range(MAX_ATTEMPTS):
        d = router.choose(request_model, excluded=tried)
        if d.slot is None:
            return ExecutionResult(False, None, None, tuple(attempts), n > 0, d.reason, _meta(attempts, cfgs, request_model, fallback_reason))
        if n == 0 and d.slot is not Slot.PRIMARY:
            fallback_reason = "primary_" + router.health.state(Slot.PRIMARY).value.lower()
        slot_cfg = cfgs[d.slot]
        tried.add(d.slot)
        router.health.acquire(d.slot)                              # reserved before the first await
        t0 = router.clock()
        try:
            value, latency = await asyncio.wait_for(call(slot_cfg, d.model), slot_cfg.timeout_s)
            latency = int(latency) if latency is not None else int((router.clock() - t0) * 1000)
            if latency >= cfg.thresholds.degraded_latency_ms:
                router.health.record_slow(d.slot, latency)
            else:
                router.health.record_success(d.slot, latency)
            attempts.append(Attempt(d.slot, True, latency))
            return ExecutionResult(True, value, d.slot, tuple(attempts), n > 0, d.reason, _meta(attempts, cfgs, d.model, fallback_reason))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            kind = classify(e)
            router.health.record_failure(d.slot, kind, getattr(e, "detail", "") or type(e).__name__)
            attempts.append(Attempt(d.slot, False, None, kind))
            fallback_reason = kind.value
            if kind not in FAILOVER_ON or not cfg.failover_enabled:
                return ExecutionResult(False, None, None, tuple(attempts), False, "failed_no_failover:" + kind.value, _meta(attempts, cfgs, d.model, fallback_reason))
        finally:
            router.health.release(d.slot)
    return ExecutionResult(False, None, None, tuple(attempts), True, "controlled_failure_after_failover", _meta(attempts, cfgs, request_model, fallback_reason))
