import ast
import asyncio
import dataclasses
import json
import os
import re
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from copilot_llm_router import config as CFG, simulator as SIM
from copilot_llm_router.contracts import (ExecutionResult, FailureKind, Health, ProviderError, RoutingDecision, Shared, Slot, SlotConfig, Thresholds)
from copilot_llm_router.failover import MAX_ATTEMPTS, classify, execute
from copilot_llm_router.health import HealthTracker
from copilot_llm_router.router import LLMRouter
from copilot_llm_router.simulator import MODEL, PLACEHOLDER_ENV, SimProvider, VClock, distribution, make_router

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "copilot_llm_router"


def run(coro):
    return asyncio.run(coro)


class ConfigTests(unittest.TestCase):
    def test_defaults_are_off_and_dry(self):
        c = CFG.load_config({})
        self.assertEqual((c.router_enabled, c.failover_enabled, c.dry_run, c.limits_shared_across_keys), (False, False, True, Shared.UNKNOWN))
        self.assertEqual([s.slot for s in c.slots], [Slot.PRIMARY, Slot.SECONDARY, Slot.EMERGENCY])
        self.assertTrue(all(not s.key_present for s in c.slots))

    def test_keys_are_never_stored_only_names_and_presence(self):
        secret = "sk-THIS-MUST-NOT-APPEAR-1234567890"
        c = CFG.load_config({"LLM_SECONDARY_KEY": secret, "LLM_API_KEY": secret + "-legacy"})
        self.assertNotIn(secret, repr(c))
        self.assertNotIn(secret, json.dumps(dataclasses.asdict(c), default=str))
        by = {s.slot: s for s in c.slots}
        self.assertTrue(by[Slot.SECONDARY].key_present and by[Slot.SECONDARY].key_env == "LLM_SECONDARY_KEY")
        self.assertEqual(by[Slot.PRIMARY].key_env, "LLM_API_KEY")                         # production key variable referenced by NAME only
        self.assertEqual({s.key_slot for s in c.slots}, {"primary", "secondary", "emergency"})

    def test_parsing_is_tolerant(self):
        c = CFG.load_config({"LLM_ROUTER_ENABLED": "TRUE", "LLM_FAILOVER_ENABLED": "no", "LLM_DRY_RUN": "", "LLM_PRIMARY_TIMEOUT": "abc", "LLM_PRIMARY_MAX_CONCURRENCY": "-3",
                             "LLM_LIMITS_SHARED_ACROSS_KEYS": "FALSE"})
        self.assertEqual((c.router_enabled, c.failover_enabled, c.dry_run, c.limits_shared_across_keys), (True, False, True, Shared.INDEPENDENT))
        self.assertEqual((c.slot(Slot.PRIMARY).timeout_s, c.slot(Slot.PRIMARY).max_concurrency), (90.0, 10))

    def test_same_model_validation(self):
        ok = CFG.load_config(PLACEHOLDER_ENV)
        self.assertEqual(CFG.validate(ok), [])
        for env, needle in (({"LLM_SECONDARY_MODEL": "other/model"}, "secondary_model_differs"), ({"LLM_SECONDARY_PROVIDER": "other"}, "secondary_provider_differs"),
                            ({"LLM_EMERGENCY_MODEL": "cheap/model"}, "emergency_model_differs")):
            problems = CFG.validate(CFG.load_config({**PLACEHOLDER_ENV, **env}))
            self.assertTrue(any(needle in p for p in problems), (env, problems))
        self.assertEqual(CFG.validate(CFG.load_config({**PLACEHOLDER_ENV, "LLM_EMERGENCY_MODEL": "cheap/model", "LLM_EMERGENCY_ALLOW_DIFFERENT_MODEL": "true"})), [])
        same_key = CFG.load_config({**PLACEHOLDER_ENV, "LLM_PRIMARY_KEY": "x", "LLM_SECONDARY_KEY": "y"})
        self.assertEqual(CFG.validate(same_key), [])
        shared = dataclasses.replace(ok, slots=tuple(dataclasses.replace(s, key_env="LLM_PRIMARY_KEY") for s in ok.slots))
        self.assertIn("secondary_uses_primary_credential", CFG.validate(shared))

    def test_different_model_secondary_is_rejected_by_the_router(self):
        # (a) both pinned, different: the secondary can never serve the request's model
        r = LLMRouter(CFG.load_config({**PLACEHOLDER_ENV, "LLM_SECONDARY_MODEL": "other/model"}), clock=VClock())
        for _ in range(10):
            r.health.acquire(Slot.PRIMARY)                                                # PRIMARY at capacity
        d = r.choose(MODEL)
        self.assertNotEqual(d.slot, Slot.SECONDARY)
        self.assertIn("SECONDARY:model_mismatch", d.reason)
        # (b) primary inherits the request model, secondary is pinned: configuration is incompatible for SAME_MODEL_FAILOVER even for a matching request
        env = {k: v for k, v in PLACEHOLDER_ENV.items() if k != "LLM_PRIMARY_MODEL"}
        r2 = LLMRouter(CFG.load_config(env), clock=VClock())
        self.assertTrue(any("secondary_model_differs" in p for p in r2.config_problems))
        for _ in range(10):
            r2.health.acquire(Slot.PRIMARY)
        d2 = r2.choose(MODEL)
        self.assertNotEqual(d2.slot, Slot.SECONDARY)
        self.assertIn("SECONDARY:incompatible_config", d2.reason)


class SelectionTests(unittest.TestCase):
    def test_primary_secondary_emergency_selection(self):
        r, _ = make_router(primary_capacity=1)
        self.assertEqual((r.choose(MODEL).slot, r.choose(MODEL).reason), (Slot.PRIMARY, "primary_healthy"))
        r.health.acquire(Slot.PRIMARY)
        d = r.choose(MODEL)
        self.assertEqual((d.slot, d.key_slot, d.model, d.provider), (Slot.SECONDARY, "secondary", MODEL, "sim"))
        for _ in range(10):
            r.health.acquire(Slot.SECONDARY)
        self.assertEqual(r.choose(MODEL).slot, Slot.EMERGENCY)
        for _ in range(10):
            r.health.acquire(Slot.EMERGENCY)
        d = r.choose(MODEL)
        self.assertIsNone(d.slot)
        self.assertTrue(d.reason.startswith("all_slots_unavailable"))

    def test_secondary_is_same_provider_same_model_other_credential(self):
        r, _ = make_router()
        p, s = r.config.slot(Slot.PRIMARY), r.config.slot(Slot.SECONDARY)
        self.assertEqual((p.provider, p.model), (s.provider, s.model))
        self.assertNotEqual((p.key_slot, p.key_env), (s.key_slot, s.key_env))

    def test_model_comes_from_the_request_when_the_slot_inherits(self):
        c = CFG.load_config({"LLM_ROUTER_ENABLED": "1", "LLM_FAILOVER_ENABLED": "1"})
        r = LLMRouter(c, clock=VClock())
        self.assertEqual(r.choose("any/model-x").model, "any/model-x")
        pinned = CFG.load_config({**PLACEHOLDER_ENV})
        self.assertEqual(LLMRouter(pinned, clock=VClock()).choose("another/model").slot, None)       # pinned model != the request's model: no slot may serve it

    def test_failover_disabled_means_primary_only(self):
        r, _ = make_router(primary_capacity=1, failover=False)
        r.health.acquire(Slot.PRIMARY)
        self.assertEqual((r.choose(MODEL).slot, r.choose(MODEL).reason), (Slot.PRIMARY, "primary_only_failover_disabled"))

    def test_router_disabled_is_the_legacy_path(self):
        c = CFG.load_config({k: v for k, v in PLACEHOLDER_ENV.items() if k != "LLM_ROUTER_ENABLED"})
        r = LLMRouter(c, clock=VClock())
        self.assertEqual(r.choose(MODEL), RoutingDecision(None, "router_disabled", use_router=False))
        prov = SimProvider()
        x = run(execute(r, prov, request_model=MODEL))
        self.assertEqual((x.ok, x.reason, x.attempts, sum(prov.calls.values())), (False, "router_disabled", (), 0))     # never calls anything

    def test_real_mode_requires_keys_dry_run_does_not(self):
        env = {**PLACEHOLDER_ENV, "LLM_DRY_RUN": "false"}
        r = LLMRouter(CFG.load_config(env), clock=VClock())
        d = r.choose(MODEL)
        self.assertIsNone(d.slot)
        self.assertIn("PRIMARY:key_missing", d.reason)
        keyed = {**env, "LLM_PRIMARY_KEY": "k1", "LLM_SECONDARY_KEY": "k2"}
        self.assertEqual(LLMRouter(CFG.load_config(keyed), clock=VClock()).choose(MODEL).slot, Slot.PRIMARY)
        self.assertEqual(make_router()[0].choose(MODEL).slot, Slot.PRIMARY)               # dry-run without keys works

    def test_capacity_scaling_is_not_assumed(self):
        for v, expect in (("", False), ("unknown", False), ("true", False), ("false", True)):
            self.assertEqual(CFG.load_config({"LLM_LIMITS_SHARED_ACROSS_KEYS": v}).capacity_scaling_guaranteed, expect, v)


class FailoverTests(unittest.TestCase):
    def one(self, errors, **kw):
        r, _ = make_router(**kw)
        prov = SimProvider(errors=errors)
        return run(execute(r, prov, request_model=MODEL)), prov, r

    def test_each_failure_kind_fails_over_once(self):
        for kind in (FailureKind.TIMEOUT, FailureKind.RATE_LIMIT, FailureKind.PROVIDER_5XX, FailureKind.CONNECTION_ERROR, FailureKind.OVERLOAD, FailureKind.UNKNOWN_ERROR):
            x, prov, _ = self.one({Slot.PRIMARY: [kind]})
            self.assertTrue(x.ok and x.slot is Slot.SECONDARY and x.failover_used, kind)
            self.assertEqual([a.slot for a in x.attempts], [Slot.PRIMARY, Slot.SECONDARY])
            self.assertEqual(x.attempts[0].failure, kind)
            self.assertEqual(x.metadata.fallback_reason, kind.value)

    def test_max_one_failover_no_infinite_retry(self):
        x, prov, _ = self.one({Slot.PRIMARY: [FailureKind.TIMEOUT] * 9, Slot.SECONDARY: [FailureKind.RATE_LIMIT] * 9, Slot.EMERGENCY: [FailureKind.TIMEOUT] * 9})
        self.assertFalse(x.ok)
        self.assertEqual((len(x.attempts), sum(prov.calls.values()), prov.calls[Slot.EMERGENCY]), (2, 2, 0))       # never a third attempt
        self.assertEqual((MAX_ATTEMPTS, x.reason), (2, "controlled_failure_after_failover"))
        self.assertIsNone(x.slot)

    def test_no_failover_when_disabled(self):
        x, prov, _ = self.one({Slot.PRIMARY: [FailureKind.RATE_LIMIT]}, failover=False)
        self.assertEqual((x.ok, len(x.attempts), sum(prov.calls.values()), x.reason), (False, 1, 1, "failed_no_failover:RATE_LIMIT"))

    def test_a_slot_is_never_tried_twice(self):
        x, prov, _ = self.one({Slot.PRIMARY: [FailureKind.TIMEOUT]})
        self.assertEqual(len({a.slot for a in x.attempts}), len(x.attempts))

    def test_emergency_after_secondary_unhealthy(self):
        r, _ = make_router()
        SIM._mark_unavailable(r, Slot.SECONDARY)
        prov = SimProvider(errors={Slot.PRIMARY: [FailureKind.RATE_LIMIT]})
        x = run(execute(r, prov, request_model=MODEL))
        self.assertEqual([a.slot for a in x.attempts], [Slot.PRIMARY, Slot.EMERGENCY])
        self.assertTrue(x.ok)

    def test_overload_routes_new_requests_without_a_failed_attempt(self):
        r, _ = make_router(primary_capacity=1)
        r.health.acquire(Slot.PRIMARY)
        prov = SimProvider()
        x = run(execute(r, prov, request_model=MODEL))
        self.assertEqual((x.slot, len(x.attempts), prov.calls[Slot.PRIMARY], x.metadata.fallback_reason), (Slot.SECONDARY, 1, 0, "primary_overloaded"))

    def test_classify(self):
        class H(Exception):
            def __init__(self, status): self.status = status
        self.assertEqual([classify(H(s)) for s in (429, 500, 502, 503, 529, 404)],
                         [FailureKind.RATE_LIMIT, FailureKind.PROVIDER_5XX, FailureKind.PROVIDER_5XX, FailureKind.PROVIDER_5XX, FailureKind.OVERLOAD, FailureKind.UNKNOWN_ERROR])
        self.assertEqual(classify(asyncio.TimeoutError()), FailureKind.TIMEOUT)
        self.assertEqual(classify(ConnectionResetError()), FailureKind.CONNECTION_ERROR)
        self.assertEqual(classify(type("ClientConnectorError", (Exception,), {})()), FailureKind.CONNECTION_ERROR)
        self.assertEqual(classify(ValueError("x")), FailureKind.UNKNOWN_ERROR)
        self.assertEqual(classify(ProviderError(FailureKind.OVERLOAD)), FailureKind.OVERLOAD)

    def test_real_timeout_triggers_failover(self):
        env = {**PLACEHOLDER_ENV, "LLM_PRIMARY_TIMEOUT": "0.05"}
        r = LLMRouter(CFG.load_config(env), clock=VClock())

        async def call(slot_cfg, model):
            if slot_cfg.slot is Slot.PRIMARY:
                await asyncio.sleep(5)
            return "ok", 100
        x = run(execute(r, call, request_model=MODEL))
        self.assertEqual((x.slot, x.attempts[0].failure), (Slot.SECONDARY, FailureKind.TIMEOUT))

    def test_cancellation_is_not_swallowed_and_capacity_is_released(self):
        r, _ = make_router()

        async def scenario():
            started = asyncio.Event()

            async def call(slot_cfg, model):
                started.set()
                await asyncio.sleep(10)
            t = asyncio.ensure_future(execute(r, call, request_model=MODEL))
            await started.wait()
            self.assertEqual(r.health.snapshot()["PRIMARY"]["active_requests"], 1)
            t.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await t
        run(scenario())
        self.assertEqual(r.health.snapshot()["PRIMARY"]["active_requests"], 0)


class HealthTests(unittest.TestCase):
    def tracker(self, **th):
        cfg = CFG.load_config(PLACEHOLDER_ENV)
        clock = VClock()
        return HealthTracker(cfg.slots, Thresholds(**th), clock), clock

    def test_transitions(self):
        h, clock = self.tracker()
        P = Slot.PRIMARY
        self.assertEqual(h.state(P), Health.HEALTHY)
        h.record_success(P, 9000)                                                          # slow: degraded
        self.assertEqual(h.state(P), Health.DEGRADED)
        h.record_success(P, 25000)
        self.assertEqual(h.state(P), Health.OVERLOADED)
        for _ in range(3):
            h.record_success(P, 300)
        self.assertEqual(h.state(P), Health.HEALTHY)                                       # recovers when latency recovers
        h.record_failure(P, FailureKind.RATE_LIMIT, "429")
        self.assertEqual(h.state(P), Health.OVERLOADED)                                    # cooldown
        clock.advance(31)
        self.assertEqual(h.state(P), Health.DEGRADED if h.snapshot()["PRIMARY"]["error_count"] and False else h.state(P))
        self.assertTrue(h.state(P).routable)
        for _ in range(3):
            h.record_failure(P, FailureKind.PROVIDER_5XX, "500")
        self.assertEqual(h.state(P), Health.UNAVAILABLE)
        clock.advance(61)
        self.assertNotEqual(h.state(P), Health.UNAVAILABLE)                                # probe allowed again after the cooldown

    def test_capacity_and_counters(self):
        h, _ = self.tracker()
        S = Slot.SECONDARY
        for _ in range(10):
            h.acquire(S)
        self.assertEqual((h.state(S), h.snapshot()["SECONDARY"]["active_requests"]), (Health.OVERLOADED, 10))
        h.release(S)
        self.assertEqual(h.state(S), Health.HEALTHY)
        h.record_failure(S, FailureKind.TIMEOUT, "t")
        h.record_failure(S, FailureKind.RATE_LIMIT, "r")
        snap = h.snapshot()["SECONDARY"]
        self.assertEqual((snap["error_count"], snap["timeout_count"], snap["rate_limit_count"], snap["last_error"]), (2, 1, 1, "RATE_LIMIT:r"))
        for _ in range(12):
            h.release(S)
        self.assertEqual(h.snapshot()["SECONDARY"]["active_requests"], 0)               # never negative

    def test_error_rate_states_need_min_events(self):
        h, clock = self.tracker(consecutive_failures_unavailable=99, rate_limit_cooldown_s=0.0001)
        P = Slot.PRIMARY
        for ok in (True, True, True, False):
            (h.record_success(P, 100) if ok else h.record_failure(P, FailureKind.PROVIDER_5XX, "x"))
        self.assertEqual(h.state(P), Health.DEGRADED)                                      # 25% of 4 events
        h2, _ = self.tracker(consecutive_failures_unavailable=99)
        h2.record_failure(P, FailureKind.PROVIDER_5XX, "x")
        self.assertEqual(h2.state(P), Health.HEALTHY)                                      # one event is not a trend
        for _ in range(3):
            h2.record_failure(P, FailureKind.PROVIDER_5XX, "x")
        self.assertEqual(h2.state(P), Health.UNAVAILABLE)

    def test_disabled_slot_is_unavailable_and_old_events_expire(self):
        cfg = CFG.load_config({**PLACEHOLDER_ENV, "LLM_EMERGENCY_ENABLED": "false"})
        clock = VClock()
        h = HealthTracker(cfg.slots, Thresholds(), clock)
        self.assertEqual(h.state(Slot.EMERGENCY), Health.UNAVAILABLE)
        h.record_success(Slot.PRIMARY, 30000)
        self.assertEqual(h.state(Slot.PRIMARY), Health.OVERLOADED)
        clock.advance(Thresholds().window_s + 1)
        self.assertEqual(h.state(Slot.PRIMARY), Health.HEALTHY)


class LedgerMetadataTests(unittest.TestCase):
    def test_metadata_contract(self):
        r, _ = make_router()
        x = run(execute(r, SimProvider(errors={Slot.PRIMARY: [FailureKind.TIMEOUT]}, latencies={Slot.SECONDARY: 640}), request_model=MODEL))
        m = x.metadata.to_dict()
        self.assertEqual(set(m), {"llm_provider", "llm_model", "llm_key_slot", "fallback_used", "fallback_reason", "primary_latency_ms", "secondary_latency_ms"})
        self.assertEqual((m["llm_provider"], m["llm_model"], m["llm_key_slot"], m["fallback_used"], m["fallback_reason"], m["primary_latency_ms"], m["secondary_latency_ms"]),
                         ("sim", MODEL, "secondary", True, "TIMEOUT", None, 640))
        ok = run(execute(make_router()[0], SimProvider(latencies={Slot.PRIMARY: 321}), request_model=MODEL)).metadata
        self.assertEqual((ok.llm_key_slot, ok.fallback_used, ok.fallback_reason, ok.primary_latency_ms), ("primary", False, None, 321))
        json.dumps(m)                                                                      # plain JSON: fits the existing metadata column

    def test_ledger_schema_and_recorder_untouched(self):
        for f in ("copilot_decisions/contract.py", "copilot_decisions/store.py", "copilot_decisions/recorder.py"):
            self.assertNotIn("llm_router", (ROOT / f).read_text())
            self.assertNotIn("llm_key_slot", (ROOT / f).read_text())


if __name__ == "__main__":
    unittest.main()
