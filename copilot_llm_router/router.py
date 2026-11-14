"""Slot selection (pure policy, no I/O). NORMAL -> PRIMARY; PRIMARY not routable -> SECONDARY; SECONDARY not routable -> EMERGENCY; none -> controlled failure.
A slot is eligible when: enabled, its key is present (dry-run simulates presence), it can serve the request's model (same-model rule),
and (for non-primary slots) failover is enabled and the SAME_MODEL_FAILOVER configuration is valid. Router disabled => the legacy path, untouched."""
from typing import Callable

from copilot_llm_router.config import same_model, validate
from copilot_llm_router.contracts import ORDER, Health, RouterConfig, RoutingDecision, Slot, SlotConfig
from copilot_llm_router.health import HealthTracker


class LLMRouter:
    def __init__(self, config: RouterConfig, health: HealthTracker | None = None, clock: Callable[[], float] | None = None):
        import time
        self.config = config
        self.clock = clock or time.monotonic
        self.health = health or HealthTracker(config.slots, config.thresholds, self.clock)
        self.config_problems = validate(config)

    def _eligible(self, cfg: SlotConfig, request_model: str | None) -> str | None:
        """None = eligible, otherwise the reason it is not."""
        c = self.config
        if not cfg.enabled:
            return "slot_disabled"
        if not cfg.key_present and not c.dry_run:
            return "key_missing"
        if not same_model(cfg, request_model, c.slot(Slot.PRIMARY)):
            return "model_mismatch"
        if cfg.slot is not Slot.PRIMARY:
            if not c.failover_enabled:
                return "failover_disabled"
            if cfg.slot is Slot.SECONDARY and self.config_problems:
                return "incompatible_config"
            if cfg.slot is Slot.EMERGENCY and self.config_problems and any(not p.startswith("emergency") and not p.startswith("secondary") for p in self.config_problems):
                return "incompatible_config"
        return None

    def choose(self, request_model: str | None = None, *, excluded=()) -> RoutingDecision:
        c = self.config
        if not c.router_enabled:
            return RoutingDecision(None, "router_disabled", use_router=False)
        notes = []
        for slot in ORDER:
            cfg = c.slot(slot)
            if cfg is None or slot in excluded:
                continue
            why = self._eligible(cfg, request_model)
            if why:
                notes.append(f"{slot.value}:{why}")
                continue
            h = self.health.state(slot)
            if not c.failover_enabled and slot is Slot.PRIMARY:
                return self._decision(cfg, "primary_only_failover_disabled", request_model)     # no failover configured: PRIMARY regardless of health
            if h.routable:
                return self._decision(cfg, "primary_healthy" if slot is Slot.PRIMARY and not excluded else f"{slot.value.lower()}_selected", request_model,
                                      suffix=notes + ([f"{slot.value}:{h.value}"] if h is Health.DEGRADED else []))
            notes.append(f"{slot.value}:{h.value}")
        return RoutingDecision(None, "all_slots_unavailable:" + ",".join(notes), use_router=True)

    def _decision(self, cfg: SlotConfig, reason: str, request_model, suffix=()):
        extra = [n for n in suffix if n]
        return RoutingDecision(cfg.slot, reason + (":" + ",".join(extra) if extra else ""), True, cfg.model or request_model, cfg.provider, cfg.key_slot)
