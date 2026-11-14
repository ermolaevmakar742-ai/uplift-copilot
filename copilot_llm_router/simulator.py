"""Dry-run simulator. NO real API calls, NO keys, NO Telegram, NO DB: a fake provider replaces the network, the configuration is built in code with
placeholder (absent) keys, the clock is virtual and sockets are blocked while it runs (any attempt is counted and reported).

    python3 -m copilot_llm_router.simulator [--json]          (or ./scripts/test_llm_failover.sh)
Exit code 0 only when every case matches its expectation and zero real calls were attempted."""
import asyncio
import contextlib
import json
import socket
import sys
from collections import Counter
from dataclasses import dataclass, replace

from copilot_llm_router.config import load_config
from copilot_llm_router.contracts import FailureKind, ProviderError, RouterConfig, Slot, Thresholds
from copilot_llm_router.failover import execute
from copilot_llm_router.router import LLMRouter

MODEL = "sim-provider/sim-model-1"          # one model for every slot: SAME_MODEL_FAILOVER
PLACEHOLDER_ENV = {"LLM_ROUTER_ENABLED": "true", "LLM_FAILOVER_ENABLED": "true", "LLM_DRY_RUN": "true",
                   "LLM_PRIMARY_PROVIDER": "sim", "LLM_SECONDARY_PROVIDER": "sim", "LLM_EMERGENCY_PROVIDER": "sim",
                   "LLM_PRIMARY_MODEL": MODEL, "LLM_SECONDARY_MODEL": MODEL, "LLM_EMERGENCY_MODEL": MODEL}      # no *_KEY variables at all


class VClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s: float):
        self.t += s


class SimProvider:
    """Deterministic fake provider: per-slot latency, per-slot scripted errors, a call counter (these are SIMULATED calls, never network)."""
    def __init__(self, latencies=None, errors=None, error_rate=None, gate: asyncio.Event | None = None):
        self.latencies = {Slot.PRIMARY: 500, Slot.SECONDARY: 700, Slot.EMERGENCY: 1500, **(latencies or {})}
        self.errors = {s: list(v) for s, v in (errors or {}).items()}       # slot -> list of FailureKind|None consumed in order
        self.error_rate = error_rate or {}                                 # slot -> rate in [0,1], deterministic (Bresenham)
        self.calls = Counter()
        self.gate = gate
        self.seq = Counter()

    async def __call__(self, slot_cfg, model):
        s = slot_cfg.slot
        self.calls[s] += 1
        i = self.seq[s]
        self.seq[s] += 1
        if self.gate is not None:
            await self.gate.wait()
        scripted = self.errors.get(s)
        kind = scripted.pop(0) if scripted else None
        rate = self.error_rate.get(s, 0)
        if kind is None and rate and int((i + 1) * rate) > int(i * rate):
            kind = FailureKind.RATE_LIMIT
        if kind is not None:
            raise ProviderError(kind, "simulated")
        return f"ok:{s.value}:{model}", self.latencies[s]


def make_router(*, primary_capacity=10, th: Thresholds | None = None, emergency=True, env_extra=None, failover=True) -> tuple[LLMRouter, VClock]:
    env = dict(PLACEHOLDER_ENV, LLM_PRIMARY_MAX_CONCURRENCY=str(primary_capacity), **(env_extra or {}))
    if not emergency:
        env["LLM_EMERGENCY_ENABLED"] = "false"
    if not failover:
        env["LLM_FAILOVER_ENABLED"] = "false"
    clock = VClock()
    return LLMRouter(load_config(env, thresholds=th), clock=clock), clock


def _mark_unavailable(router: LLMRouter, slot: Slot):
    for _ in range(router.config.thresholds.consecutive_failures_unavailable):
        router.health.record_failure(slot, FailureKind.PROVIDER_5XX, "simulated")


@contextlib.contextmanager
def no_network():
    """Blocks socket creation for the duration of a run; yields a counter of attempts (must stay 0)."""
    attempts = Counter()
    real = socket.socket

    class Blocked(real):
        def __init__(self, *a, **k):
            attempts["socket"] += 1
            raise RuntimeError("network is blocked in the dry-run simulator")
    socket.socket = Blocked
    try:
        yield attempts
    finally:
        socket.socket = real


async def distribution(*, requests=20, primary_capacity=10, primary_latency_ms=500, secondary_latency_ms=700, primary_error_rate=0.0) -> dict:
    """N concurrent requests; PRIMARY holds `primary_capacity` at once, the rest are routed to SECONDARY (decided at admission, never migrated)."""
    router, _ = make_router(primary_capacity=primary_capacity)
    gate = asyncio.Event()
    prov = SimProvider({Slot.PRIMARY: primary_latency_ms, Slot.SECONDARY: secondary_latency_ms}, error_rate={Slot.PRIMARY: primary_error_rate}, gate=gate)
    tasks = [asyncio.ensure_future(execute(router, prov, request_model=MODEL)) for _ in range(requests)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gate.set()
    res = await asyncio.gather(*tasks)
    served = Counter(r.slot.value if r.slot else "FAILED" for r in res)
    return {"requests": requests, "served": {s.value: served.get(s.value, 0) for s in Slot} | ({"FAILED": served["FAILED"]} if served["FAILED"] else {}),
            "failovers": sum(1 for r in res if r.failover_used), "ok": sum(1 for r in res if r.ok), "sim_calls": {s.value: prov.calls[s] for s in Slot},
            "config": {"primary_capacity": primary_capacity, "primary_latency_ms": primary_latency_ms, "secondary_latency_ms": secondary_latency_ms,
                       "primary_error_rate": primary_error_rate}}


async def _one(router, prov):
    return await execute(router, prov, request_model=MODEL)


async def run_cases() -> list[dict]:
    cases = []

    def add(name, expect, observed, ok, **extra):
        cases.append({"case": name, "expect": expect, "observed": observed, "pass": bool(ok), **extra})

    r, _ = make_router()                                                                            # 1
    x = await _one(r, SimProvider())
    add("1 primary healthy", "PRIMARY", x.slot.value, x.slot is Slot.PRIMARY and not x.failover_used)

    r, _ = make_router(primary_capacity=2)                                                          # 2
    r.health.acquire(Slot.PRIMARY); r.health.acquire(Slot.PRIMARY)
    x = await _one(r, SimProvider())
    add("2 primary overloaded", "SECONDARY", x.slot.value, x.slot is Slot.SECONDARY and len(x.attempts) == 1, reason=x.reason)

    r, _ = make_router()                                                                            # 3
    x = await _one(r, SimProvider(errors={Slot.PRIMARY: [FailureKind.TIMEOUT]}))
    add("3 primary timeout", "PRIMARY fail -> SECONDARY", "->".join(a.slot.value for a in x.attempts), x.slot is Slot.SECONDARY and x.failover_used
        and x.metadata.fallback_reason == "TIMEOUT")

    r, _ = make_router()                                                                            # 4
    prov = SimProvider(errors={Slot.PRIMARY: [FailureKind.RATE_LIMIT]})
    x = await _one(r, prov)
    y = await _one(r, prov)
    add("4 primary 429", "SECONDARY now and for the next NEW request", f"{x.slot.value},{y.slot.value}", x.slot is Slot.SECONDARY and y.slot is Slot.SECONDARY
        and len(y.attempts) == 1 and prov.calls[Slot.PRIMARY] == 1)

    r, _ = make_router()                                                                            # 5a
    _mark_unavailable(r, Slot.PRIMARY); _mark_unavailable(r, Slot.SECONDARY)
    x = await _one(r, SimProvider())
    add("5a primary+secondary unavailable", "EMERGENCY", x.slot.value, x.slot is Slot.EMERGENCY)
    r, _ = make_router(emergency=False)                                                             # 5b
    _mark_unavailable(r, Slot.PRIMARY); _mark_unavailable(r, Slot.SECONDARY)
    prov = SimProvider()
    x = await _one(r, prov)
    add("5b all unavailable, no emergency", "controlled failure, 0 calls", f"{x.reason.split(':')[0]}, calls={sum(prov.calls.values())}",
        not x.ok and x.slot is None and sum(prov.calls.values()) == 0)

    r, _ = make_router()                                                                            # 5c: both attempts fail -> stop (max one failover)
    prov = SimProvider(errors={Slot.PRIMARY: [FailureKind.PROVIDER_5XX], Slot.SECONDARY: [FailureKind.PROVIDER_5XX]})
    x = await _one(r, prov)
    add("5c primary and secondary both fail", "controlled failure after exactly 2 attempts", f"attempts={len(x.attempts)}, calls={sum(prov.calls.values())}",
        not x.ok and len(x.attempts) == 2 and sum(prov.calls.values()) == 2 and prov.calls[Slot.EMERGENCY] == 0)

    d = await distribution()                                                                        # 6
    add("6 20 concurrent, primary capacity 10", "PRIMARY 10 / SECONDARY 10 / EMERGENCY 0", f"PRIMARY {d['served']['PRIMARY']} / SECONDARY {d['served']['SECONDARY']} / EMERGENCY {d['served']['EMERGENCY']}",
        d["served"] == {"PRIMARY": 10, "SECONDARY": 10, "EMERGENCY": 0} and d["ok"] == 20, detail=d)

    d = await distribution(requests=30, primary_capacity=10)                                        # 7: capacity exhausted on both -> emergency / failure is bounded
    add("7 30 concurrent (capacity 10 each slot)", "no more than capacity per slot; the rest spill to EMERGENCY", str(d["served"]),
        d["served"]["PRIMARY"] == 10 and d["served"]["SECONDARY"] == 10 and d["served"]["EMERGENCY"] == 10, detail=d)

    d = await distribution(requests=10, primary_capacity=10, primary_error_rate=0.5, primary_latency_ms=900, secondary_latency_ms=1200)   # 8
    add("8 primary error rate 0.5 (deterministic)", "failures fail over; none lost", f"served={d['served']}, failovers={d['failovers']}",
        d["ok"] == 10 and d["failovers"] == 5, detail=d)

    r, _ = make_router()                                                                            # 9: in-flight requests are never migrated
    gate = asyncio.Event()
    prov = SimProvider(gate=gate)
    first = [asyncio.ensure_future(_one(r, prov)) for _ in range(2)]
    await asyncio.sleep(0)
    r.health.record_failure(Slot.PRIMARY, FailureKind.RATE_LIMIT, "event")                         # PRIMARY becomes unhealthy while two are running
    later = [asyncio.ensure_future(_one(r, prov)) for _ in range(2)]
    await asyncio.sleep(0)
    gate.set()
    a, b = await asyncio.gather(asyncio.gather(*first), asyncio.gather(*later))
    add("9 event-based failover", "running requests stay on PRIMARY, NEW ones go SECONDARY", f"{[x.slot.value for x in a]} then {[x.slot.value for x in b]}",
        all(x.slot is Slot.PRIMARY for x in a) and all(x.slot is Slot.SECONDARY for x in b))

    cfg = make_router()[0].config                                                                   # 10
    add("10 shared limits unknown", "capacity scaling NOT guaranteed", f"limits_shared={cfg.limits_shared_across_keys.value}, scaling_guaranteed={cfg.capacity_scaling_guaranteed}",
        cfg.capacity_scaling_guaranteed is False)
    return cases


def main(argv) -> int:
    async def guarded():
        with no_network() as attempts:                      # installed inside the running loop (asyncio itself needs its own socketpair first)
            return await run_cases(), attempts
    cases, attempts = asyncio.run(guarded())
    report = {"cases": cases, "real_api_calls": 0, "socket_attempts": attempts["socket"], "real_keys": 0, "all_pass": all(c["pass"] for c in cases) and attempts["socket"] == 0}
    if "--json" in argv:
        print(json.dumps(report, indent=1, default=str))
    else:
        print("LLM FAILOVER DRY-RUN (simulated provider, placeholder keys, sockets blocked)")
        for c in cases:
            print(f"  [{'OK' if c['pass'] else 'FAIL'}] {c['case']}: {c['observed']}")
        print(f"real API calls: 0 | socket attempts: {attempts['socket']} | real keys: 0 | {'ALL PASS' if report['all_pass'] else 'FAILED'}")
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
