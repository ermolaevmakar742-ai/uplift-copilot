"""Health tracker: deterministic, injectable clock, plain thresholds (not tuned on real data). Per slot it records active requests, a sliding window of
outcomes (latency, failures), counters and the last error, and derives HEALTHY / DEGRADED / OVERLOADED / UNAVAILABLE.

  UNAVAILABLE : disabled / not usable, or consecutive failures >= N (cooldown, then a probe is allowed again as DEGRADED)
  OVERLOADED  : active >= max_concurrency, or inside a rate-limit / overload cooldown, or recent latency >= overloaded threshold
  DEGRADED    : recent latency >= degraded threshold, or window error rate >= degraded threshold (>= min events)
  HEALTHY     : otherwise"""
from collections import deque
from typing import Callable

from copilot_llm_router.contracts import FailureKind, Health, Slot, SlotConfig, Thresholds


class _SlotState:
    def __init__(self, window: int):
        self.active = 0
        self.events: deque = deque(maxlen=window)      # (t, ok, latency_ms, FailureKind|None)
        self.error_count = self.timeout_count = self.rate_limit_count = 0
        self.consecutive_failures = 0
        self.last_error: str | None = None
        self.cooldown_until = 0.0                       # rate-limit / overload
        self.unavailable_until = 0.0


class HealthTracker:
    def __init__(self, configs, thresholds: Thresholds, clock: Callable[[], float]):
        self._cfg: dict[Slot, SlotConfig] = {c.slot: c for c in configs}
        self.th = thresholds
        self._now = clock
        self._s = {s: _SlotState(thresholds.window_events) for s in self._cfg}

    def acquire(self, slot: Slot) -> None:
        self._s[slot].active += 1

    def release(self, slot: Slot) -> None:
        st = self._s[slot]
        st.active = max(0, st.active - 1)

    def record_success(self, slot: Slot, latency_ms: int | None) -> None:
        st = self._s[slot]
        st.events.append((self._now(), True, latency_ms, None))
        st.consecutive_failures = 0

    def record_failure(self, slot: Slot, kind: FailureKind, detail: str = "") -> None:
        st, now = self._s[slot], self._now()
        st.events.append((now, False, None, kind))
        st.error_count += 1
        st.consecutive_failures += 1
        st.last_error = f"{kind.value}:{detail}"[:200] if detail else kind.value
        if kind is FailureKind.TIMEOUT:
            st.timeout_count += 1
        if kind in (FailureKind.RATE_LIMIT, FailureKind.OVERLOAD):
            st.rate_limit_count += 1
            st.cooldown_until = now + self.th.rate_limit_cooldown_s
        if st.consecutive_failures >= self.th.consecutive_failures_unavailable:
            st.unavailable_until = now + self.th.unavailable_cooldown_s

    def record_slow(self, slot: Slot, latency_ms: int) -> None:
        """A success above the degraded threshold: kept as a success event whose latency feeds the state (FailureKind.LATENCY_THRESHOLD is its label)."""
        self._s[slot].events.append((self._now(), True, latency_ms, FailureKind.LATENCY_THRESHOLD))
        self._s[slot].consecutive_failures = 0

    def _recent(self, st):
        now, w = self._now(), self.th.window_s
        return [e for e in st.events if now - e[0] <= w]

    def state(self, slot: Slot) -> Health:
        cfg, st, th, now = self._cfg[slot], self._s[slot], self.th, self._now()
        if not cfg.enabled or now < st.unavailable_until:
            return Health.UNAVAILABLE
        ev = self._recent(st)
        lats = [e[2] for e in ev if e[1] and e[2] is not None]
        recent_lat = max(lats[-3:]) if lats else 0
        if st.active >= cfg.max_concurrency or now < st.cooldown_until or recent_lat >= th.overloaded_latency_ms:
            return Health.OVERLOADED
        n_err = sum(1 for e in ev if not e[1])
        rate = n_err / len(ev) if len(ev) >= th.min_events else 0.0
        if rate >= th.unavailable_error_rate:
            return Health.UNAVAILABLE
        if recent_lat >= th.degraded_latency_ms or rate >= th.degraded_error_rate:
            return Health.DEGRADED
        return Health.HEALTHY

    def snapshot(self) -> dict:
        out = {}
        for slot, st in self._s.items():
            ev = self._recent(st)
            lats = [e[2] for e in ev if e[1] and e[2] is not None]
            out[slot.value] = {"state": self.state(slot).value, "active_requests": st.active, "recent_latency_ms": lats[-1] if lats else None,
                               "error_count": st.error_count, "timeout_count": st.timeout_count, "rate_limit_count": st.rate_limit_count,
                               "last_error": st.last_error}
        return out
