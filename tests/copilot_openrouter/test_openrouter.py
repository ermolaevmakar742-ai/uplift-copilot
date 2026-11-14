import ast
import asyncio
import json
import logging
import re
import socket
import unittest
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from copilot_openrouter import api as oapi, client as oclient
from copilot_openrouter.contracts import FetchError, KeyState, OpenRouterKeyStatus
from copilot_openrouter.service import OpenRouterBalanceService
from copilot_secrets.contracts import SecretValue, Slot

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "copilot_openrouter"
KEY = "sk-or-test-SENTINEL-0123456789abcdef-91af"
T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
CREDITS = {"data": {"total_credits": 20, "total_usage": 19.309022217}}
GOOD = {"data": {"label": "sk-or-v1-abc...xyz", "limit": 50, "limit_remaining": 37.5, "usage": 12.5, "usage_daily": 1.25, "usage_weekly": 4, "usage_monthly": 12.5,
                 "is_free_tier": False}}


def run(c):
    return asyncio.run(c)


class FakeSecrets:
    def __init__(self, slots=(Slot.PRIMARY,)):
        self.slots, self.calls = set(slots), []

    async def get_secret_for_runtime(self, slot):
        self.calls.append(slot)
        return SecretValue(KEY) if slot in self.slots else None


def transport(status=200, body=GOOD, exc=None, seen=None, raw=None):
    def h(req: httpx.Request):
        if seen is not None:
            seen.append(req)
        if exc:
            raise exc
        return httpx.Response(status, content=raw) if raw is not None else httpx.Response(status, json=body)
    return httpx.MockTransport(h)


def routed(key=None, credits=None):
    """key / credits: (status, body) or an Exception. Routes by URL path; unknown path -> 404."""
    def h(req: httpx.Request):
        r = {"/api/v1/key": key, "/api/v1/credits": credits}.get(req.url.path)
        if isinstance(r, Exception):
            raise r
        if r is None:
            return httpx.Response(404, json={})
        return httpx.Response(r[0], json=r[1]) if not isinstance(r[1], bytes) else httpx.Response(r[0], content=r[1])
    return httpx.MockTransport(h)


def mkfetch(**kw):
    """key-only fetch (the /key endpoint), as before."""
    async def f(secret):
        return await oclient.fetch_key_info(secret, transport=transport(**kw))
    return f


def mkall(key=(200, GOOD), credits=(200, CREDITS)):
    async def f(secret):
        return await oclient.fetch_all(secret, transport=routed(key, credits))
    return f


def svc(fetch=None, slots=(Slot.PRIMARY,), ttl=45.0, mono=None):
    return OpenRouterBalanceService(FakeSecrets(slots), fetch=fetch or mkfetch(), clock=lambda: T0, ttl=ttl, **({"monotonic": mono} if mono else {}))


class ClientAndService(unittest.TestCase):
    def test_healthy_and_parsing(self):
        r = run(svc().status(Slot.PRIMARY))
        self.assertEqual((r.status, r.configured, r.limit, r.limit_remaining, r.balance), ("HEALTHY", True, 50.0, 37.5, None))      # key-only fetch: balance is NOT limit_remaining
        self.assertEqual((r.usage_daily, r.usage_weekly, r.usage_monthly), (1.25, 4.0, 12.5))
        self.assertEqual((r.provider, r.contract_version, r.retrieved_at), ("openrouter", 2, T0))
        with self.assertRaises(Exception):
            r.balance = 1                                                                # immutable

    def test_missing_fields_are_none_not_zero(self):
        r = run(svc(mkfetch(body={"data": {"limit": None, "usage": 3}})).status(Slot.PRIMARY))
        self.assertEqual(r.status, "HEALTHY")
        for f in ("balance", "limit", "limit_remaining", "usage_daily", "usage_weekly", "usage_monthly"):
            self.assertIsNone(getattr(r, f), f)
        r = run(svc(mkfetch(body={"data": {"limit_remaining": "12", "usage_daily": True, "usage_weekly": float("nan")}})).status(Slot.PRIMARY))
        self.assertIsNone(r.balance); self.assertIsNone(r.usage_daily)

    def test_limited_when_exhausted(self):
        r = run(svc(mkfetch(body={"data": {"limit": 10, "limit_remaining": 0}})).status(Slot.PRIMARY))
        self.assertEqual((r.status, r.limit_remaining, r.balance), ("LIMITED", 0.0, None))

    def test_not_configured_makes_no_request(self):
        seen = []
        s = svc(mkfetch(seen=seen), slots=())
        r = run(s.status(Slot.SECONDARY))
        self.assertEqual((r.configured, r.status), (False, "NOT_CONFIGURED")); self.assertEqual(seen, [])

    def test_error_mapping(self):
        cases = [(dict(status=401), "UNAUTHORIZED"), (dict(status=403), "UNAUTHORIZED"), (dict(status=429), "RATE_LIMITED"),
                 (dict(exc=httpx.ReadTimeout("x")), "UNAVAILABLE"), (dict(exc=httpx.ConnectError("x")), "UNAVAILABLE"),
                 (dict(status=500), "ERROR"), (dict(status=503), "ERROR"), (dict(status=400), "ERROR"),
                 (dict(raw=b"<html>nope"), "ERROR"), (dict(body={"data": []}), "ERROR"), (dict(body=[1]), "ERROR"), (dict(body={}), "ERROR")]
        for kw, want in cases:
            r = run(svc(mkfetch(**kw)).status(Slot.PRIMARY))
            self.assertEqual(r.status, want, kw)
            self.assertIsNone(r.balance); self.assertTrue(r.configured)
            self.assertNotIn("html", json.dumps(r.to_dict()))
            self.assertTrue(r.error_message_safe)

    def test_unexpected_exception_is_contained(self):
        async def boom(secret):
            raise RuntimeError("boom " + secret)
        with self.assertLogs("copilot_openrouter", "WARNING") as lg:
            r = run(svc(boom).status(Slot.PRIMARY))
        self.assertEqual(r.status, "UNAVAILABLE")
        self.assertNotIn("SENTINEL", "\n".join(lg.output) + json.dumps(r.to_dict()))

    def test_request_shape_and_timeout_and_no_arbitrary_url(self):
        seen = []
        run(oclient.fetch_key_info(KEY, transport=transport(seen=seen)))
        self.assertEqual(str(seen[0].url), "https://openrouter.ai/api/v1/key")
        self.assertEqual(seen[0].method, "GET"); self.assertEqual(seen[0].headers["authorization"], "Bearer " + KEY)
        self.assertTrue(oclient.KEY_URL.startswith("https://")); self.assertTrue(0 < oclient.TIMEOUT_SECONDS <= 15)
        import inspect
        self.assertEqual(list(inspect.signature(oclient.fetch_key_info).parameters), ["secret_value", "transport", "timeout"])   # no url / proxy parameter
        captured = {}
        orig = httpx.AsyncClient.__init__
        def spy(self_, *a, **kw):
            captured.update(kw); orig(self_, *a, **kw)
        httpx.AsyncClient.__init__ = spy
        try:
            run(oclient.fetch_key_info(KEY, transport=transport()))
        finally:
            httpx.AsyncClient.__init__ = orig
        self.assertIsNotNone(captured["timeout"].connect); self.assertIs(captured["follow_redirects"], False); self.assertIs(captured["trust_env"], False)
        self.assertNotIn("proxy", captured); self.assertNotIn("proxies", captured)

    def test_cache(self):
        clock = [0.0]; calls = []
        async def f(secret):
            calls.append(1); return {"limit_remaining": 5.0}
        s = svc(f, mono=lambda: clock[0])
        a = run(s.status(Slot.PRIMARY)); b = run(s.status(Slot.PRIMARY))
        self.assertIs(a, b); self.assertEqual(len(calls), 1)
        run(s.status(Slot.PRIMARY, force=True)); self.assertEqual(len(calls), 2)       # refresh bypasses
        clock[0] = 46; run(s.status(Slot.PRIMARY)); self.assertEqual(len(calls), 3)    # expired
        for cached in s._cache.values():
            self.assertNotIn("SENTINEL", repr(cached))
        # failures are never cached
        s2 = svc(mkfetch(status=500), mono=lambda: clock[0]); run(s2.status(Slot.PRIMARY)); self.assertEqual(s2._cache, {})

    def test_no_secret_in_logs(self):
        logging.getLogger("copilot_openrouter").setLevel(logging.DEBUG)
        with self.assertLogs("copilot_openrouter", "DEBUG") as lg:
            logging.getLogger("copilot_openrouter").debug("start")
            for kw in (dict(status=401), dict(exc=httpx.ConnectError(KEY)), dict(status=500), dict()):
                run(svc(mkfetch(**kw)).status(Slot.PRIMARY))
        self.assertNotIn("SENTINEL", "\n".join(lg.output))


class BalanceTests(unittest.TestCase):
    """Real response shapes: /key (limit may be null) + /credits (account balance)."""
    NOLIMIT = {"data": {"limit": None, "limit_remaining": None, "limit_reset": None, "usage": 19.29473417, "usage_daily": 0.000660331, "usage_weekly": 0.000660331,
                        "usage_monthly": 0.000660331, "is_free_tier": False, "is_management_key": True, "rate_limit": {}, "free_model_daily_requests": {}}}

    def st(self, **kw):
        return run(svc(mkall(**kw)).status(Slot.PRIMARY))

    def test_key_without_limit_but_with_credits_shows_balance(self):
        r = self.st(key=(200, self.NOLIMIT))
        self.assertEqual((r.status, r.limit, r.limit_remaining), ("HEALTHY", None, None))
        self.assertAlmostEqual(r.balance, 0.690978, places=6); self.assertEqual((r.total_credits, r.total_usage), (20.0, 19.309022217))
        self.assertEqual((r.balance_source, r.balance_error_code), ("credits", None)); self.assertAlmostEqual(r.usage_daily, 0.000660331)

    def test_key_with_limit_and_credits_keeps_both_separate(self):
        r = self.st()
        self.assertEqual((r.limit, r.limit_remaining), (50.0, 37.5)); self.assertAlmostEqual(r.balance, 0.690978, places=6)
        self.assertNotEqual(r.balance, r.limit_remaining)

    def test_no_limit_never_invents_balance(self):
        r = self.st(key=(200, self.NOLIMIT), credits=(403, {"error": {"message": "Only management keys can perform this operation"}}))
        self.assertEqual((r.status, r.balance, r.balance_source, r.balance_error_code), ("HEALTHY", None, None, "credits_forbidden"))
        self.assertNotIn("management", json.dumps(r.to_dict()))                                     # provider message is not passed through

    def test_credits_missing_or_malformed(self):
        for body in ({"data": {}}, {"data": {"total_credits": 20}}, {"data": {"total_credits": "20", "total_usage": 1}}, {"data": []}, [], b"<html>"):
            r = self.st(credits=(200, body)); self.assertEqual(r.status, "HEALTHY"); self.assertIsNone(r.balance, body); self.assertEqual(r.balance_error_code, "credits_malformed_response")

    def test_credits_failures_do_not_break_key_status(self):
        for cr, code in (((401, {}), "credits_unauthorized"), ((429, {}), "credits_rate_limited"), ((500, {}), "credits_http_500"), (httpx.ReadTimeout("t"), "credits_network_error")):
            r = self.st(credits=cr); self.assertEqual((r.status, r.balance, r.balance_error_code), ("HEALTHY", None, code))

    def test_key_endpoint_failures_still_decide_status(self):
        cases = [((401, {}), "UNAUTHORIZED"), ((429, {}), "RATE_LIMITED"), (httpx.ReadTimeout("t"), "UNAVAILABLE"), ((500, {}), "ERROR"), ((200, b"<html>"), "ERROR"), ((200, {"data": []}), "ERROR")]
        for k, want in cases:
            r = self.st(key=k); self.assertEqual(r.status, want); self.assertIsNone(r.balance)

    def test_exhausted_balance_is_limited(self):
        r = self.st(key=(200, self.NOLIMIT), credits=(200, {"data": {"total_credits": 5, "total_usage": 5.5}}))
        self.assertEqual((r.status, r.balance), ("LIMITED", -0.5))

    def test_both_calls_use_only_constant_urls_and_same_bearer(self):
        seen = []
        def h(req):
            seen.append(req); return httpx.Response(200, json=CREDITS if req.url.path.endswith("credits") else GOOD)
        run(oclient.fetch_all(KEY, transport=httpx.MockTransport(h)))
        self.assertEqual([str(r.url) for r in seen], ["https://openrouter.ai/api/v1/key", "https://openrouter.ai/api/v1/credits"])
        self.assertTrue(all(r.headers["authorization"] == "Bearer " + KEY for r in seen))

    def test_credits_leak_nothing(self):
        with self.assertLogs("copilot_openrouter", "WARNING") as lg:
            run(svc(mkall(credits=httpx.ConnectError(KEY))).status(Slot.PRIMARY))
            logging.getLogger("copilot_openrouter").warning("end")
        self.assertNotIn("SENTINEL", "\n".join(lg.output))


class ApiTests(unittest.TestCase):
    def build(self, service=None, audit=None):
        async def require_admin(authorization: str | None = Header(None)):
            if authorization != "Bearer admin":
                raise HTTPException(401, "unauthorized")
        app = FastAPI()
        self.audit = []
        async def alog(action, ip=None, details=None, user_id=None):
            self.audit.append((action, details))
        self.svc = service or svc(slots=(Slot.PRIMARY, Slot.SECONDARY))
        app.include_router(oapi.build_router(require_admin, None, audit if audit is not None else alog, service=self.svc))
        return TestClient(app)

    H = {"Authorization": "Bearer admin"}
    B = "/api/settings/llm/openrouter"

    def test_admin_guard_on_every_route(self):
        c = self.build()
        for m, p in (("get", "/status"), ("get", "/status/PRIMARY"), ("post", "/refresh")):
            self.assertEqual(getattr(c, m)(self.B + p).status_code, 401, p)
            self.assertEqual(getattr(c, m)(self.B + p, headers={"Authorization": "Bearer x"}).status_code, 401, p)
        self.assertEqual(self.svc._secrets.calls, [])

    def test_status_endpoints_and_no_secret(self):
        c = self.build()
        r = c.get(self.B + "/status", headers=self.H); self.assertEqual(r.status_code, 200)
        j = r.json(); self.assertEqual([s["slot"] for s in j["slots"]], ["PRIMARY", "SECONDARY", "EMERGENCY"])
        self.assertEqual(j["slots"][0]["status"], "HEALTHY"); self.assertEqual(j["slots"][2]["status"], "NOT_CONFIGURED")
        self.assertEqual(r.headers["cache-control"], "no-store")
        r1 = c.get(self.B + "/status/primary", headers=self.H); self.assertEqual(r1.json()["slot"]["limit_remaining"], 37.5)
        self.assertEqual(c.get(self.B + "/status/bogus", headers=self.H).status_code, 404)
        blob = r.text + r1.text
        for bad in ("SENTINEL", KEY, "sk-or", "label", "is_free_tier", "Bearer"):
            self.assertNotIn(bad, blob)

    def test_refresh_endpoint_forces_and_audits_safely(self):
        calls = []
        async def f(secret):
            calls.append(1); return {"limit_remaining": 9.0, "balance": 1.0}
        c = self.build(svc(f, slots=(Slot.PRIMARY,)))
        c.get(self.B + "/status/PRIMARY", headers=self.H); self.assertEqual(len(calls), 1)
        r = c.post(self.B + "/refresh", headers=self.H, json={"slot": "PRIMARY"})
        self.assertEqual(len(calls), 2); self.assertEqual(len(r.json()["slots"]), 1)
        self.assertEqual(self.audit[0][0], "openrouter_balance_refreshed")
        self.assertIn("OPENROUTER_BALANCE_REFRESHED", self.audit[0][1]); self.assertIn("slot=PRIMARY status=HEALTHY", self.audit[0][1])
        r = c.post(self.B + "/refresh", headers=self.H); self.assertEqual(len(r.json()["slots"]), 3)
        self.assertEqual(len(self.audit), 4)
        self.assertNotIn("SENTINEL", json.dumps(self.audit) + r.text)
        self.assertEqual(c.post(self.B + "/refresh", headers=self.H, json={"url": "http://evil"}).status_code, 400)   # arbitrary url impossible
        self.assertEqual(c.post(self.B + "/refresh", headers=self.H, json={"slot": "X"}).status_code, 404)
        self.assertEqual(c.post(self.B + "/refresh", headers=self.H, content=b"{bad").status_code, 400)

    def test_automatic_reads_not_audited(self):
        c = self.build(); c.get(self.B + "/status", headers=self.H); c.get(self.B + "/status/PRIMARY", headers=self.H)
        self.assertEqual(self.audit, [])

    def test_failures_do_not_leak_or_break(self):
        for kw in (dict(status=401), dict(status=429), dict(exc=httpx.ReadTimeout(KEY)), dict(status=502), dict(raw=b"secret body")):
            c = self.build(svc(mkfetch(**kw)))
            r = c.post(self.B + "/refresh", headers=self.H, json={"slot": "PRIMARY"})
            self.assertEqual(r.status_code, 200); self.assertNotIn("secret body", r.text); self.assertNotIn("SENTINEL", r.text)
            self.assertNotIn("Traceback", r.text)

    def test_audit_failure_does_not_break(self):
        async def bad(*a, **k):
            raise RuntimeError("x")
        c = self.build(audit=bad); self.assertEqual(c.post(self.B + "/refresh", headers=self.H).status_code, 200)

    def test_default_service_without_master_key_is_not_configured(self):
        async def ra(authorization: str | None = Header(None)):
            return None
        app = FastAPI(); app.include_router(oapi.build_router(ra, None, None, env={}))
        r = TestClient(app).get(self.B + "/status")
        self.assertEqual(r.status_code, 200); self.assertTrue(all(s["status"] == "NOT_CONFIGURED" for s in r.json()["slots"]))


if __name__ == "__main__":
    unittest.main()
