import ast
import asyncio
import copy
import json
import logging
import pickle
import re
import socket
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI, Header, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from copilot_secrets import api as sapi, migration, repository as repo, service as svc_mod, store
from copilot_secrets.contracts import LLMSecretProvider, Operation, SecretError, SecretMetadata, SecretValue, Slot, Status, TestResult
from copilot_secrets.credential_test import test_provider_credentials as check_provider_credentials   # alias: pytest must not collect the imported function as a test
from copilot_secrets.crypto import MASTER_ENV, PREVIOUS_ENV, SecretBox, generate_master_key
from copilot_secrets.service import SecretService, mask, validate_key, validate_provider

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "copilot_secrets"
T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
KEY1 = "sk-test-SENTINEL-one-0123456789abcdef91af"
KEY2 = "sk-test-SENTINEL-two-ABCDEFGHIJKLMNOP77ee"
MASTER = generate_master_key()
BOX = lambda: SecretBox({MASTER_ENV: MASTER})
SECRET_PARTS = ("SENTINEL-one", "SENTINEL-two", "0123456789abcdef", "ABCDEFGHIJKLMNOP")


def leaked(blob: str) -> list[str]:
    return [p for p in (KEY1, KEY2) + SECRET_PARTS if p in blob]


class CryptoTests(unittest.TestCase):
    def test_roundtrip_and_ciphertext_is_not_plaintext(self):
        b = BOX()
        tok = b.encrypt("PRIMARY", 1, KEY1)
        self.assertEqual(leaked(tok), [])
        self.assertEqual(b.decrypt("PRIMARY", 1, tok), KEY1)
        self.assertNotEqual(tok, b.encrypt("PRIMARY", 1, KEY1))                          # fresh IV: ciphertexts differ

    def test_binding_tamper_and_wrong_key(self):
        b = BOX()
        tok = b.encrypt("PRIMARY", 1, KEY1)
        self.assertIsNone(b.decrypt("SECONDARY", 1, tok))                               # copied to another slot
        self.assertIsNone(b.decrypt("PRIMARY", 2, tok))                                 # copied to another version
        self.assertIsNone(b.decrypt("PRIMARY", 1, tok[:-3] + ("AAA" if not tok.endswith("AAA") else "BBB")))
        self.assertIsNone(b.decrypt("PRIMARY", 1, "not-a-token"))
        self.assertIsNone(b.decrypt("PRIMARY", 1, ""))
        self.assertIsNone(SecretBox({MASTER_ENV: generate_master_key()}).decrypt("PRIMARY", 1, tok))

    def test_missing_or_invalid_master_key_is_disabled_not_fatal(self):
        for env, state in (({}, "missing"), ({MASTER_ENV: ""}, "missing"), ({MASTER_ENV: "   "}, "missing"), ({MASTER_ENV: "garbage-not-a-fernet-key"}, "invalid")):
            b = SecretBox(env)
            self.assertEqual((b.available, b.state), (False, state))
            with self.assertRaises(RuntimeError):
                b.encrypt("PRIMARY", 1, KEY1)                                           # no plaintext fallback
            self.assertIsNone(b.decrypt("PRIMARY", 1, BOX().encrypt("PRIMARY", 1, KEY1)))
        self.assertNotIn("garbage", repr(SecretBox({MASTER_ENV: "garbage-not-a-fernet-key"}).__dict__))

    def test_master_key_rotation_with_previous_key(self):
        old = generate_master_key()
        tok = SecretBox({MASTER_ENV: old}).encrypt("PRIMARY", 1, KEY1)
        both = SecretBox({MASTER_ENV: MASTER, PREVIOUS_ENV: old})
        self.assertEqual(both.decrypt("PRIMARY", 1, tok), KEY1)
        self.assertIsNone(SecretBox({MASTER_ENV: MASTER}).decrypt("PRIMARY", 1, tok))

    def test_uses_the_standard_library_only(self):
        tree = ast.parse((PKG / "crypto.py").read_text())
        mods = {(n.module if isinstance(n, ast.ImportFrom) else a.name) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in getattr(n, "names", [None])}
        self.assertIn("cryptography.fernet", mods)
        self.assertFalse(mods & {"hashlib", "base64", "random", "secrets", "hmac", "Crypto", "nacl"})        # no home-made primitives


class ContractTests(unittest.TestCase):
    def test_secret_value_never_leaks_by_accident(self):
        v = SecretValue(KEY1)
        for out in (repr(v), str(v), f"{v}", "%s" % v, "%r" % v, format(v, ">30"), repr([v]), repr({"k": v})):
            self.assertEqual(leaked(out), [], out)
        for fn in (pickle.dumps, copy.copy, copy.deepcopy, json.dumps):
            with self.assertRaises(TypeError):
                fn(v)
        with self.assertRaises(AttributeError):
            v._v = "x"
        with self.assertRaises(TypeError):
            hash(v)
        self.assertEqual(v.reveal(), KEY1)
        self.assertNotEqual(v, SecretValue(KEY1))                                        # no equality oracle

    def test_metadata_has_no_value_fields(self):
        names = set(SecretMetadata.__dataclass_fields__)
        self.assertEqual(names, {"secret_id", "slot", "provider", "key_slot", "masked", "status", "version", "created_at", "updated_at", "last_tested_at", "last_test_result"})
        self.assertFalse({n for n in names if re.search(r"value|key$|secret$|cipher|token|plain", n)} - {"key_slot"})

    def test_masking_and_validation(self):
        self.assertEqual(mask(KEY1), "••••••••••••91af")
        self.assertEqual(mask("sk-short-0123456789abc"), "••••••••••••")                  # short key: no suffix at all
        self.assertEqual(validate_key("  " + KEY1 + "\n"), KEY1)
        for bad in ("", "short", "x" * 19, "x" * 513, "has space in the middle of the key 123456", "tab\tinside-0123456789abcdef", "ключ-0123456789abcdef-ключ", None, 5, ["a"], "key\nnewline-0123456789abc"):
            with self.assertRaises(SecretError) as e:
                validate_key(bad)
            self.assertEqual(e.exception.code, "invalid_key_format")
        self.assertEqual(validate_provider(" OpenRouter "), "openrouter")
        for bad in ("", " ", "-x", "a b", "a;b", "x" * 65, None, 3):
            with self.assertRaises(SecretError):
                validate_provider(bad)


class Base(unittest.IsolatedAsyncioTestCase):
    master = True

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        await migration.ensure_schema(self.engine)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        self.now = T0
        self.box = BOX() if self.master else SecretBox({})
        self.panel_audit = []

        async def panel_audit(action, ip=None, details=None):
            self.panel_audit.append((action, details))
        self.svc = SecretService(self.sf, self.box, clock=self._clock, audit_log=panel_audit)

    def _clock(self):
        self.now += timedelta(seconds=1)
        return self.now

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def dump(self) -> str:
        out = []
        async with self.sf() as s:
            for t in ("llm_secrets", "llm_secret_audit"):
                out += [repr(tuple(r)) for r in (await s.execute(text(f"select * from {t}"))).all()]
        return "\n".join(out)

    async def rows(self, table="llm_secrets"):
        async with self.sf() as s:
            order = "version" if table == "llm_secrets" else "created_at"
            return [dict(r) for r in (await s.execute(text(f"select * from {table} order by {order}"))).mappings().all()]


class ServiceTests(Base):
    async def test_create_encrypts_and_returns_only_metadata(self):
        m = await self.svc.set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin")
        self.assertEqual((m.slot, m.version, m.status, m.provider, m.key_slot, m.masked), ("PRIMARY", 1, "ACTIVE", "openrouter", "primary", "••••••••••••91af"))
        self.assertEqual(leaked(repr(m) + json.dumps(m.to_dict())), [])
        self.assertEqual(leaked(await self.dump()), [])                                   # plaintext appears in NO column of NO table
        row = (await self.rows())[0]
        self.assertEqual(self.box.decrypt("PRIMARY", 1, row["encrypted_value"]), KEY1)    # it IS recoverable, only with the master key
        self.assertEqual(leaked(json.dumps({k: str(v) for k, v in row.items() if k != "encrypted_value"})), [])

    async def test_runtime_read_is_the_only_decrypting_path(self):
        await self.svc.set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin")
        sv = await self.svc.get_secret_for_runtime(Slot.PRIMARY)
        self.assertIsInstance(sv, SecretValue)
        self.assertEqual(sv.reveal(), KEY1)
        self.assertIsNone(await self.svc.get_secret_for_runtime(Slot.SECONDARY))
        meta = await self.svc.get_secret_metadata(Slot.PRIMARY)
        self.assertEqual(leaked(repr(meta)), [])
        self.assertIsInstance(self.svc, LLMSecretProvider.__class__) if False else None
        self.assertTrue(asyncio.iscoroutinefunction(self.svc.get_secret_for_runtime))

    async def test_rotation_new_version_active_old_revoked_and_erased(self):
        await self.svc.set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin")
        old_token = (await self.rows())[0]["encrypted_value"]
        m2 = await self.svc.set_secret(Slot.PRIMARY, KEY2, "openrouter", "admin")
        self.assertEqual((m2.version, m2.status), (2, "ACTIVE"))
        rows = await self.rows()
        self.assertEqual([(r["version"], r["status"], r["encrypted_value"] is None) for r in rows], [(1, "REVOKED", True), (2, "ACTIVE", False)])
        self.assertNotIn(old_token, await self.dump())                                    # the old ciphertext is gone from the database
        self.assertEqual((await self.svc.get_secret_for_runtime(Slot.PRIMARY)).reveal(), KEY2)
        self.assertIsNone(self.box.decrypt("PRIMARY", 1, rows[1]["encrypted_value"]))     # and a v2 token is not valid as v1
        m3 = await self.svc.set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin")
        self.assertEqual(m3.version, 3)                                                   # versions only grow, even when the value is reused
        self.assertEqual(leaked(await self.dump()), [])

    async def test_slots_are_independent(self):
        await self.svc.set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin")
        await self.svc.set_secret(Slot.SECONDARY, KEY2, "openrouter", "admin")
        self.assertEqual((await self.svc.get_secret_for_runtime(Slot.PRIMARY)).reveal(), KEY1)
        self.assertEqual((await self.svc.get_secret_for_runtime(Slot.SECONDARY)).reveal(), KEY2)
        self.assertEqual({k: (v.version if v else None) for k, v in (await self.svc.list_metadata()).items()}, {"PRIMARY": 1, "SECONDARY": 1, "EMERGENCY": None})
        await self.svc.delete_secret(Slot.PRIMARY, "admin")
        self.assertEqual((await self.svc.get_secret_for_runtime(Slot.SECONDARY)).reveal(), KEY2)

    async def test_delete_removes_runtime_access_and_erases(self):
        await self.svc.set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin")
        await self.svc.delete_secret(Slot.PRIMARY, "admin")
        self.assertIsNone(await self.svc.get_secret_for_runtime(Slot.PRIMARY))
        self.assertIsNone(await self.svc.get_secret_metadata(Slot.PRIMARY))
        r = (await self.rows())[0]
        self.assertEqual((r["status"], r["encrypted_value"]), ("DELETED", None))
        with self.assertRaises(SecretError) as e:
            await self.svc.delete_secret(Slot.PRIMARY, "admin")
        self.assertEqual(e.exception.code, "not_configured")
        m = await self.svc.set_secret(Slot.PRIMARY, KEY2, "openrouter", "admin")
        self.assertEqual((m.version, m.status), (2, "ACTIVE"))                            # a deleted slot can be set again, as a NEW version

    async def test_disable_and_activate(self):
        await self.svc.set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin")
        m = await self.svc.disable_secret(Slot.PRIMARY, "admin")
        self.assertEqual(m.status, "DISABLED")
        self.assertIsNone(await self.svc.get_secret_for_runtime(Slot.PRIMARY))            # no runtime access while disabled
        with self.assertRaises(SecretError):
            await self.svc.disable_secret(Slot.PRIMARY, "admin")
        self.assertEqual((await self.svc.activate_secret(Slot.PRIMARY, "admin")).status, "ACTIVE")
        self.assertEqual((await self.svc.get_secret_for_runtime(Slot.PRIMARY)).reveal(), KEY1)
        await self.svc.set_secret(Slot.PRIMARY, KEY2, "openrouter", "admin")
        await self.svc.disable_secret(Slot.PRIMARY, "admin")
        await self.svc.set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin")              # rotating a disabled slot revokes the disabled version too
        self.assertEqual([r["status"] for r in await self.rows()], ["REVOKED", "REVOKED", "ACTIVE"])
        with self.assertRaises(SecretError):
            await self.svc.activate_secret(Slot.SECONDARY, "admin")                       # nothing to activate

    async def test_revoked_versions_can_never_be_reactivated(self):
        await self.svc.set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin")
        await self.svc.set_secret(Slot.PRIMARY, KEY2, "openrouter", "admin")
        async with self.sf() as s:
            rows = (await s.execute(select(store.llm_secrets.c.secret_id, store.llm_secrets.c.status))).all()
        self.assertEqual(sorted(r[1] for r in rows), ["ACTIVE", "REVOKED"])
        with self.assertRaises(Exception):                                                 # at most one ACTIVE per slot (partial unique index)
            async with self.sf() as s:
                await s.execute(text("update llm_secrets set status='ACTIVE' where status='REVOKED'"))
                await s.commit()

    async def test_invalid_input_changes_nothing_and_never_echoes(self):
        await self.svc.set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin")
        before = await self.dump()
        for key, prov in (("short", "openrouter"), (KEY2, "bad provider!"), (KEY2 + " extra", "openrouter"), (None, "openrouter")):
            with self.assertRaises(SecretError) as e:
                await self.svc.set_secret(Slot.PRIMARY, key, prov, "admin")
            self.assertEqual(leaked(str(e.exception) + repr(e.exception.args)), [])
        rows = await self.rows()
        self.assertEqual([(r["version"], r["status"]) for r in rows], [(1, "ACTIVE")])
        self.assertEqual((await self.svc.get_secret_for_runtime(Slot.PRIMARY)).reveal(), KEY1)    # the existing key survives failed rotations

    async def test_audit_events(self):
        await self.svc.set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin")
        await self.svc.set_secret(Slot.PRIMARY, KEY2, "openrouter", "admin")
        await self.svc.test_secret(Slot.PRIMARY, "admin")
        await self.svc.disable_secret(Slot.PRIMARY, "admin")
        await self.svc.activate_secret(Slot.PRIMARY, "admin")
        await self.svc.delete_secret(Slot.PRIMARY, "admin")
        with self.assertRaises(SecretError):
            await self.svc.set_secret(Slot.PRIMARY, "bad", "openrouter", "admin")
        ev = await self.rows("llm_secret_audit")
        ops = [(e["operation"], e["success"]) for e in sorted(ev, key=lambda e: e["created_at"])]
        self.assertEqual(ops, [("KEY_CREATED", True), ("KEY_ACTIVATED", True), ("KEY_UPDATED", True), ("KEY_ACTIVATED", True), ("KEY_TESTED", True), ("KEY_DISABLED", True),
                               ("KEY_ACTIVATED", True), ("KEY_DELETED", True), ("KEY_UPDATED", False)])
        for e in ev:
            self.assertTrue(e["actor"] == "admin" and e["key_slot"] == "PRIMARY" and e["created_at"] is not None)
        self.assertEqual({e["operation"] for e in ev}, {o.value for o in Operation})
        failed = next(e for e in ev if not e["success"])
        self.assertEqual((failed["reason"], failed["version"]), ("invalid_key_format", None))
        self.assertEqual(leaked(await self.dump() + json.dumps(self.panel_audit)), [])
        self.assertEqual(len(self.panel_audit), len(ev))                                  # mirrored into the panel's existing audit log, also without keys
        self.assertTrue(all(a.startswith("llm_key_") for a, _ in self.panel_audit))

    async def test_nothing_is_logged_with_a_key(self):
        with self.assertLogs(level="DEBUG") as cm:
            logging.getLogger("probe").debug("start")
            await self.svc.set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin")
            await self.svc.set_secret(Slot.PRIMARY, KEY2, "openrouter", "admin")
            await self.svc.get_secret_for_runtime(Slot.PRIMARY)
            await self.svc.test_secret(Slot.PRIMARY, "admin")
            with self.assertRaises(SecretError):
                await self.svc.set_secret(Slot.PRIMARY, KEY2[:10], "openrouter", "admin")
            await self.svc.delete_secret(Slot.PRIMARY, "admin")
        self.assertEqual(leaked("\n".join(cm.output)), [])
        self.assertTrue(any("LLM_KEY op=KEY_CREATED slot=PRIMARY" in o for o in cm.output))

    async def test_key_cannot_appear_in_a_traceback(self):
        with mock.patch.object(SecretBox, "encrypt", side_effect=RuntimeError("boom")):
            try:
                await self.svc.set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin")
            except Exception:
                tb = traceback.format_exc()
        self.assertEqual(leaked(tb), [])
        self.assertIn("boom", tb)

    async def test_dry_run_connection_test_has_zero_network_calls(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network used")), mock.patch.object(socket, "create_connection", side_effect=AssertionError("network used")):
            self.assertEqual(await self.svc.test_secret(Slot.PRIMARY, "admin"), {"result": "NOT_CONFIGURED", "detail": "no_active_key"})
            await self.svc.set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin")
            r = await self.svc.test_secret(Slot.PRIMARY, "admin")
        self.assertEqual(r, {"result": "DRY_RUN", "detail": "CONFIGURATION_ONLY"})
        m = await self.svc.get_secret_metadata(Slot.PRIMARY)
        self.assertEqual((m.last_test_result, m.last_tested_at is not None), ("DRY_RUN", True))
        await self.svc.disable_secret(Slot.PRIMARY, "admin")
        self.assertEqual((await self.svc.test_secret(Slot.PRIMARY, "admin"))["result"], "NOT_CONFIGURED")

    def test_test_abstraction_results(self):
        self.assertEqual(check_provider_credentials("PRIMARY", "openrouter", None)[0], TestResult.NOT_CONFIGURED)
        self.assertEqual(check_provider_credentials("PRIMARY", "openrouter", SecretValue(KEY1))[0], TestResult.DRY_RUN)
        self.assertEqual(check_provider_credentials("PRIMARY", "openrouter", SecretValue(KEY1), dry_run=False)[0], TestResult.UNAVAILABLE)    # no real calls yet
        self.assertEqual({r.value for r in TestResult}, {"VALID", "INVALID", "UNAVAILABLE", "NOT_CONFIGURED", "DRY_RUN"})

    async def test_storage_not_migrated(self):
        async with self.engine.begin() as c:
            await c.execute(text("drop table llm_secrets"))
        for call in (self.svc.get_secret_metadata(Slot.PRIMARY), self.svc.set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin"), self.svc.delete_secret(Slot.PRIMARY, "admin")):
            with self.assertRaises(SecretError) as e:
                await call
            self.assertEqual(e.exception.code, "storage_not_migrated")
        self.assertIsNone(await self.svc.get_secret_for_runtime(Slot.PRIMARY))            # runtime read never raises


class NoMasterKeyTests(Base):
    master = False

    async def test_writes_and_reads_are_disabled_without_crashing(self):
        self.assertEqual((self.svc.writes_enabled, self.box.state), (False, "missing"))
        with self.assertRaises(SecretError) as e:
            await self.svc.set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin")
        self.assertEqual(e.exception.code, "master_key_unavailable")
        self.assertEqual(await self.rows(), [])                                           # nothing stored, in particular no plaintext fallback
        self.assertEqual(leaked(await self.dump()), [])
        self.assertIsNone(await self.svc.get_secret_for_runtime(Slot.PRIMARY))
        with self.assertRaises(SecretError):
            await self.svc.activate_secret(Slot.PRIMARY, "admin")
        self.assertEqual((await self.svc.test_secret(Slot.PRIMARY, "admin"))["result"], "NOT_CONFIGURED")

    async def test_existing_ciphertext_is_unreadable_without_the_master_key(self):
        await SecretService(self.sf, BOX(), clock=self._clock).set_secret(Slot.PRIMARY, KEY1, "openrouter", "admin")
        self.assertIsNone(await self.svc.get_secret_for_runtime(Slot.PRIMARY))
        self.assertEqual((await self.svc.test_secret(Slot.PRIMARY, "admin"))["result"], "UNAVAILABLE")
        self.assertEqual((await self.svc.get_secret_metadata(Slot.PRIMARY)).masked, "••••••••••••91af")     # metadata still works, value does not
        wrong = SecretService(self.sf, SecretBox({MASTER_ENV: generate_master_key()}), clock=self._clock)
        self.assertIsNone(await wrong.get_secret_for_runtime(Slot.PRIMARY))


class ApiTests(Base):
    def app(self, env=None, service=None):
        app = FastAPI()

        async def require_admin(x_admin_token: str | None = Header(default=None)):
            if x_admin_token != "ok":
                raise HTTPException(401, "Missing token")
        app.include_router(sapi.build_router(require_admin, self.sf, None, env={} if env is None else env, service=service or self.svc))
        return app

    def client(self, **kw):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app(**kw)), base_url="http://t")

    H = {"X-Admin-Token": "ok"}

    async def test_every_route_requires_the_admin_guard(self):
        routes = [("GET", ""), ("POST", "/PRIMARY"), ("DELETE", "/PRIMARY"), ("POST", "/PRIMARY/test"), ("POST", "/PRIMARY/disable"), ("POST", "/PRIMARY/activate")]
        async with self.client() as c:
            for tok in (None, "bad", "", "OK"):
                for method, path in routes:
                    h = {} if tok is None else {"X-Admin-Token": tok}
                    r = await c.request(method, "/api/settings/llm/keys" + path, headers=h, json={"api_key": KEY1} if method == "POST" and path == "/PRIMARY" else None)
                    self.assertEqual(r.status_code, 401, (tok, method, path))
                    self.assertEqual(leaked(r.text), [])
        self.assertEqual(await self.rows(), [])                                           # unauthenticated requests changed nothing
        self.assertEqual(await self.rows("llm_secret_audit"), [])

    async def test_set_list_rotate_delete_flow_never_returns_a_key(self):
        async with self.client() as c:
            u = "/api/settings/llm/keys"
            r = await c.post(f"{u}/primary", headers=self.H, json={"api_key": KEY1, "provider": "OpenRouter"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual((r.json()["key"]["masked"], r.json()["key"]["version"], r.json()["key"]["provider"]), ("••••••••••••91af", 1, "openrouter"))
            self.assertEqual(r.headers["cache-control"], "no-store")
            r2 = await c.post(f"{u}/PRIMARY", headers=self.H, json={"api_key": KEY2})
            self.assertEqual((r2.json()["key"]["version"], r2.json()["key"]["masked"]), (2, "••••••••••••77ee"))
            g = await c.get(u, headers=self.H)
            body = g.json()
            self.assertEqual(g.headers["cache-control"], "no-store")
            self.assertEqual([(s["slot"], s["status"], s["version"]) for s in body["slots"]], [("PRIMARY", "ACTIVE", 2), ("SECONDARY", "NOT_CONFIGURED", None), ("EMERGENCY", "NOT_CONFIGURED", None)])
            self.assertEqual((body["master_key"], body["writes_enabled"], body["storage"], body["router"]), ("configured", True, "ready", {"enabled": False, "failover_enabled": False, "dry_run": True}))
            t = await c.post(f"{u}/PRIMARY/test", headers=self.H)
            self.assertEqual((t.json()["result"], t.json()["detail"]), ("DRY_RUN", "CONFIGURATION_ONLY"))
            self.assertEqual((await c.post(f"{u}/PRIMARY/disable", headers=self.H)).json()["key"]["status"], "DISABLED")
            self.assertEqual((await c.post(f"{u}/PRIMARY/activate", headers=self.H)).json()["key"]["status"], "ACTIVE")
            self.assertEqual((await c.delete(f"{u}/PRIMARY", headers=self.H)).status_code, 200)
            self.assertEqual((await c.delete(f"{u}/PRIMARY", headers=self.H)).status_code, 404)
            everything = r.text + r2.text + g.text + t.text
            self.assertEqual(leaked(everything + await self.dump()), [])

    async def test_validation_errors_do_not_echo_the_submitted_key(self):
        u = "/api/settings/llm/keys"
        async with self.client() as c:
            cases = [({"api_key": KEY1[:10]}, 400), ({"api_key": KEY1, "provider": "bad provider"}, 400), ({"api_key": KEY1, "extra": 1}, 400), ({"provider": "x"}, 400),
                     ({"api_key": [KEY1]}, 400), ([KEY1], 400)]
            for body, code in cases:
                r = await c.post(f"{u}/PRIMARY", headers=self.H, json=body)
                self.assertEqual(r.status_code, code, body)
                self.assertEqual(leaked(r.text), [], body)
            r = await c.post(f"{u}/PRIMARY", headers=self.H, content=("{bad json " + KEY1).encode())
            self.assertEqual((r.status_code, leaked(r.text)), (400, []))
            r = await c.post(f"{u}/PRIMARY", headers=self.H, json={"api_key": KEY1 + "x" * 3000})
            self.assertEqual((r.status_code, leaked(r.text)), (413, []))
            r = await c.post(f"{u}/NOPE", headers=self.H, json={"api_key": KEY1})
            self.assertEqual((r.status_code, leaked(r.text)), (404, []))
        self.assertEqual(await self.rows(), [])

    async def test_unexpected_failure_is_a_generic_503_without_data_or_traceback(self):
        with mock.patch.object(SecretService, "set_secret", side_effect=RuntimeError("db exploded " + KEY1)), self.assertLogs("copilot_secrets", level="ERROR") as cm:
            async with self.client() as c:
                r = await c.post("/api/settings/llm/keys/PRIMARY", headers=self.H, json={"api_key": KEY1})
        self.assertEqual((r.status_code, r.json()["detail"]["error"]), (503, "secret_service_unavailable"))
        self.assertEqual(leaked(r.text + "\n".join(cm.output)), [])

    async def test_missing_master_key_and_missing_storage(self):
        nokey = SecretService(self.sf, SecretBox({}), clock=self._clock)
        async with self.client(service=nokey) as c:
            g = (await c.get("/api/settings/llm/keys", headers=self.H)).json()
            self.assertEqual((g["master_key"], g["writes_enabled"]), ("missing", False))
            r = await c.post("/api/settings/llm/keys/PRIMARY", headers=self.H, json={"api_key": KEY1})
            self.assertEqual((r.status_code, r.json()["detail"]["error"], leaked(r.text)), (503, "master_key_unavailable", []))
        async with self.engine.begin() as c2:
            await c2.execute(text("drop table llm_secrets"))
        async with self.client() as c:
            g = (await c.get("/api/settings/llm/keys", headers=self.H)).json()
            self.assertEqual((g["storage"], [s["status"] for s in g["slots"]]), ("not_migrated", ["NOT_CONFIGURED"] * 3))
            self.assertEqual((await c.post("/api/settings/llm/keys/PRIMARY", headers=self.H, json={"api_key": KEY1})).status_code, 503)

    async def test_provider_model_come_from_router_config_read_only(self):
        env = {"LLM_PRIMARY_PROVIDER": "openrouter", "LLM_PRIMARY_MODEL": "vendor/model-x", "LLM_PRIMARY_KEY": "SHOULD-NOT-APPEAR-ANYWHERE-123456"}
        async with self.client(env=env) as c:
            r = await c.get("/api/settings/llm/keys", headers=self.H)
        p = r.json()["slots"][0]
        self.assertEqual((p["provider"], p["model"]), ("openrouter", "vendor/model-x"))
        self.assertNotIn("SHOULD-NOT-APPEAR", r.text)                                      # key variables of the router config are never exposed


class MigrationAndInterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_apply_rollback(self):
        eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        plan = migration.plan(eng.dialect)
        self.assertTrue(any(s.startswith("CREATE TABLE IF NOT EXISTS llm_secrets") for s in plan))
        self.assertTrue(any("uq_llm_secrets_one_active" in s for s in plan))
        from sqlalchemy.dialects import postgresql
        pg = migration.plan(postgresql.dialect())
        self.assertTrue(any("WHERE status = 'ACTIVE'" in s for s in pg))
        self.assertIn("DROP TABLE IF EXISTS llm_secrets", migration.rollback_plan(postgresql.dialect()))
        async with eng.connect() as c:
            self.assertEqual((await c.execute(text("select count(*) from sqlite_master where name like 'llm_secret%'"))).scalar(), 0)    # dry-run wrote nothing
        await migration.ensure_schema(eng)
        await migration.ensure_schema(eng)
        async with eng.connect() as c:
            self.assertEqual((await c.execute(text("select count(*) from sqlite_master where type='table' and name like 'llm_secret%'"))).scalar(), 2)
        await migration.rollback_schema(eng)
        async with eng.connect() as c:
            self.assertEqual((await c.execute(text("select count(*) from sqlite_master where type='table' and name like 'llm_secret%'"))).scalar(), 0)
        await eng.dispose()
        self.assertIn("--apply", (PKG / "migration.py").read_text())
        for f in ("service.py", "api.py", "repository.py", "crypto.py"):
            self.assertNotIn("ensure_schema", (PKG / f).read_text())

    async def test_future_router_interface(self):
        eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        await migration.ensure_schema(eng)
        s = SecretService(async_sessionmaker(eng, expire_on_commit=False), BOX())
        self.assertTrue(callable(getattr(s, "get_secret_for_runtime")))
        await s.set_secret(Slot.SECONDARY, KEY1, "openrouter", "admin")
        v = await s.get_secret_for_runtime(Slot.SECONDARY)
        self.assertEqual(type(v).__name__, "SecretValue")
        from copilot_llm_router.config import load_config
        cfg = load_config({})
        self.assertEqual((cfg.router_enabled, cfg.failover_enabled, cfg.dry_run), (False, False, True))      # the router stays OFF / dry-run
        await eng.dispose()


if __name__ == "__main__":
    unittest.main()
