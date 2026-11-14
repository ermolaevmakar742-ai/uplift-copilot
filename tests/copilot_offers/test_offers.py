import ast
import asyncio
import dataclasses
import json
import os
import pathlib
import socket
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest import mock

import httpx
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from copilot_offers import events, hook, migrate, store
from copilot_offers.accounts import account_ref_for
from copilot_offers.api import build_router
from copilot_offers.context import build_offer_context
from copilot_offers.decision import decide
from copilot_offers.models import OfferContext, OfferInput, OfferReason as R, OfferResolution, OfferStatus as S
from copilot_offers.resolver import resolve_assigned_offer, resolve_from_data
from copilot_offers.service import AccountRefInvalid, OfferService, ProgramNotFound
from copilot_offers.validate import ContentValidationError, valid_url
from copilot_programs import migrate as pmigrate
from copilot_programs.contract import build_program
from copilot_programs.service import ProgramService

ACCT_ID = 777000111
ACCT = account_ref_for(ACCT_ID)
URL = "https://partner.example/reg?ref=abc"
H = {"X-Admin-Token": "good"}
ROOT = pathlib.Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


def program(pid="p_mx", active=True, **over):
    data = {"name": "BetPartner MX", "brand": "BetPartner", "registration_url": URL, "promo_code": "PROMO1",
            "terms": "TERMS_MARK", "notes": "NOTES_MARK", "support_contact": "SUPPORT_MARK"}
    p = build_program(pid, data, active=active, updated_by="t", created_at=NOW, updated_at=NOW)
    return dataclasses.replace(p, **over) if over else p


def content(desc="hello", images=()):
    return {"description": desc, "images": [{"image_id": f"img_{n}", "url": u, "sort_order": n, "active": True}
                                             for n, u in enumerate(images)]}


def resolve(prog=None, cont=None, pid="p_mx", acct=ACCT):
    return resolve_from_data(acct, pid, prog, cont if cont is not None else content())


def inp(acct=ACCT, stage="visited", corr="pd_abc"):
    return OfferInput(correlation_id=corr, client_ref="cli_1234", account_ref=acct, stage_raw=stage)


class TestResolver(unittest.TestCase):
    def test_01_valid_assigned_offer(self):
        r = resolve(program(), content("d", ["https://i.example/1.jpg"]))
        self.assertEqual((r.status, r.reason, r.program_id), (S.OFFER_ASSIGNED, R.OFFER_ASSIGNED, "p_mx"))
        c = r.context
        self.assertEqual((c.offer_id, c.account_ref, c.name, c.brand, c.description, c.registration_url, c.promo_code, c.active),
                         ("p_mx", ACCT, "BetPartner MX", "BetPartner", "d", URL, "PROMO1", True))
        self.assertEqual([i.url for i in c.images], ["https://i.example/1.jpg"])

    def test_02_no_assignment(self):
        r = resolve_from_data(ACCT, None, None, None)
        self.assertEqual((r.status, r.reason, r.program_id, r.context), (S.NO_OFFER_ASSIGNED, R.NO_ASSIGNMENT, None, None))

    def test_03_unknown_account(self):
        for bad in (None, "", "acct_zz", "777000111", 5):
            r = resolve_from_data(bad, "p_mx", program(), content())
            self.assertEqual((r.status, r.reason, r.context), (S.ACCOUNT_UNKNOWN, R.ACCOUNT_UNKNOWN, None), bad)

    def test_04_unknown_program(self):
        r = resolve_from_data(ACCT, "gone", None, None)
        self.assertEqual((r.status, r.reason, r.program_id), (S.OFFER_UNAVAILABLE, R.PROGRAM_NOT_FOUND, "gone"))

    def test_05_inactive_offer(self):
        r = resolve(program(active=False))
        self.assertEqual((r.status, r.reason, r.context), (S.OFFER_UNAVAILABLE, R.INACTIVE, None))

    def test_06_invalid_url(self):
        for u in (None, "", "ftp://x.com/a", "https://", "https://a b.com", "not a url", "https://user:pw@x.com/", "x" * 3000, "//x.com"):
            r = resolve(program(registration_url=u))
            self.assertEqual((r.status, r.reason, r.context), (S.OFFER_UNAVAILABLE, R.INVALID, None), u)

    def test_07_08_javascript_data_file_rejected(self):
        for u in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>1</script>", "file:///etc/passwd", "vbscript:x"):
            self.assertFalse(valid_url(u), u)
            self.assertEqual(resolve(program(registration_url=u)).reason, R.INVALID, u)

    def test_09_localhost_rejected(self):
        for u in ("http://localhost/", "http://localhost:8080/x", "https://LOCALHOST./", "http://foo.localhost/", "http://intranet/",
                  "http://printer.local/", "http://db.internal/", "http://[::1]/"):
            self.assertEqual(resolve(program(registration_url=u)).reason, R.INVALID, u)

    def test_10_private_ip_rejected(self):
        for h in ("127.0.0.1", "127.1.2.3", "10.0.0.5", "172.16.0.1", "172.31.255.255", "192.168.1.1", "169.254.169.254", "0.0.0.0",
                  "100.64.0.1", "[::1]", "[fd00::1]", "[fe80::1]", "224.0.0.1", "2130706433", "0x7f.0.0.1", "017700000001", "0177.0.0.1"):
            self.assertEqual(resolve(program(registration_url=f"http://{h}/")).reason, R.INVALID, h)
        self.assertEqual(resolve(program(registration_url="https://8.8.8.8/x")).status, S.OFFER_ASSIGNED)      # public IP is fine
        self.assertEqual(resolve(program(registration_url="https://partner.example:8443/x")).status, S.OFFER_ASSIGNED)

    def test_11_invalid_image_url(self):
        for bad in ("javascript:x", "data:image/png;base64,AAAA", "http://127.0.0.1/a.png", "https://", "http://10.0.0.1/a.jpg"):
            self.assertEqual(resolve(program(), content("d", ["https://i.example/ok.jpg", bad])).reason, R.INVALID, bad)

    def test_12_duplicate_images_deduplicated_in_order(self):
        urls = ["https://i.example/3.jpg", "https://i.example/1.jpg", "https://i.example/3.jpg", "https://i.example/2.jpg"]
        c = resolve(program(), content("d", urls)).context
        self.assertEqual([i.url for i in c.images], ["https://i.example/3.jpg", "https://i.example/1.jpg", "https://i.example/2.jpg"])

    def test_13_max_images(self):
        ten = [f"https://i.example/{n}.jpg" for n in range(10)]
        self.assertEqual(len(resolve(program(), content("d", ten)).context.images), 10)
        self.assertEqual(resolve(program(), content("d", ten + ["https://i.example/x.jpg"])).reason, R.INVALID)
        inactive = content("d", ten); inactive["images"].append({"image_id": "z", "url": "https://i.example/z.jpg", "sort_order": 99, "active": False})
        self.assertEqual(len(resolve(program(), inactive).context.images), 10)          # inactive images are not counted or exposed

    def test_14_empty_description(self):
        for d in (None, "", "   "):
            r = resolve(program(), {"description": d if d is None else d.strip() or None, "images": []})
            self.assertEqual((r.status, r.context.description), (S.OFFER_ASSIGNED, None), repr(d))

    def test_15_long_description(self):
        self.assertEqual(resolve(program(), content("x" * 1000)).status, S.OFFER_ASSIGNED)
        self.assertEqual(resolve(program(), content("x" * 1001)).reason, R.INVALID)
        self.assertEqual(resolve(program(), content("bad\x00char")).reason, R.INVALID)

    def test_16_promo_code(self):
        self.assertIsNone(resolve(program(promo_code=None)).context.promo_code)
        self.assertEqual(resolve(program(promo_code="P" * 64)).status, S.OFFER_ASSIGNED)
        for bad in ("P" * 65, "a\x00b", "a\nb"):
            self.assertEqual(resolve(program(promo_code=bad)).reason, R.INVALID, repr(bad))

    def test_name_brand_validation(self):
        for over in ({"name": ""}, {"name": "  "}, {"name": "n" * 121}, {"brand": ""}):
            self.assertEqual(resolve(program(**over)).reason, R.INVALID, over)

    def test_17_context_only_allowed_fields(self):
        self.assertEqual({f.name for f in dataclasses.fields(OfferContext)},
                         {"offer_id", "account_ref", "name", "brand", "description", "registration_url", "promo_code", "images", "active"})
        blob = repr(resolve(program(), content("d", ["https://i.example/1.jpg"])).context)
        for needle in ("TERMS_MARK", "NOTES_MARK", "SUPPORT_MARK", str(ACCT_ID)):
            self.assertNotIn(needle, blob)
        self.assertIsNone(build_offer_context(ACCT, program(active=False), content()))
        self.assertIsNone(build_offer_context(ACCT, program(registration_url="http://localhost/"), content()))

    def test_resolution_repr_hides_context(self):
        r = resolve(program(), content("SECRETISH-DESC"))
        self.assertNotIn("SECRETISH", repr(r))
        self.assertNotIn(URL, repr(r))

    def test_typed_result(self):
        self.assertEqual({s.value for s in S}, {"OFFER_ASSIGNED", "OFFER_UNAVAILABLE", "NO_OFFER_ASSIGNED", "ACCOUNT_UNKNOWN"})
        self.assertIsInstance(resolve(program()), OfferResolution)

    def test_one_resolver_no_duplicated_rules(self):
        src = {p.name: p.read_text() for p in (ROOT / "copilot_offers").glob("*.py")}
        self.assertFalse((ROOT / "copilot_offers" / "provider.py").exists())
        self.assertEqual([n for n, t in src.items() if "def resolve_from_data" in t], ["resolver.py"])
        self.assertEqual([n for n, t in src.items() if "def validate_offer(" in t], ["validate.py"])


class TestDecision(unittest.TestCase):
    def test_decision_maps_resolution_without_context_data(self):
        res = resolve(program(), content("d", ["https://i.example/1.jpg"]))
        d = decide(inp(), res, offer_decision_id="od_x")
        self.assertEqual((d.status, d.reason, d.program_id, d.mode, d.policy_version, d.correlation_id, d.client_ref, d.account_ref),
                         (S.OFFER_ASSIGNED, R.OFFER_ASSIGNED, "p_mx", "shadow", "account_offer_v1", "pd_abc", "cli_1234", ACCT))
        blob = json.dumps(d.to_record(), default=str)
        for needle in (URL, "PROMO1", "BetPartner", "hello", "i.example"):
            self.assertNotIn(needle, blob)
        self.assertEqual(d, decide(inp(), res, offer_decision_id="od_x"))                     # deterministic

    def test_statuses(self):
        self.assertEqual(decide(inp(), resolve_from_data(ACCT, None, None, None)).status, S.NO_OFFER_ASSIGNED)
        self.assertEqual(decide(inp(acct=None), resolve_from_data(None, None, None, None)).status, S.ACCOUNT_UNKNOWN)
        self.assertEqual(decide(inp(), resolve(program(active=False))).reason, R.INACTIVE)

    def test_stage_is_observability_only(self):
        self.assertEqual(decide(inp(stage="deposited"), resolve(program())).status, S.OFFER_ASSIGNED)


class DbBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        await migrate.ensure_schema(self.engine)
        await pmigrate.ensure_schema(self.engine)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        self.svc = OfferService(self.sf)
        self._env = dict(os.environ)
        for k in ("COPILOT_OFFER_ENGINE_ENABLED", "COPILOT_OFFER_SHADOW_ENABLED", "COPILOT_OFFER_REAL_ENABLED", "COPILOT_OFFER_CONTEXT_ENABLED"):
            os.environ.pop(k, None)

    async def asyncTearDown(self):
        os.environ.clear(); os.environ.update(self._env)
        await self.engine.dispose()

    async def add_program(self, pid="p_mx", active=True, **over):
        data = {"name": "BetPartner MX", "brand": "BetPartner", "registration_url": URL, **over}
        await ProgramService(self.sf).create_program(pid, data, updated_by="t", active=active)

    async def rows(self, table="offer_decisions", cols="*"):
        async with self.sf() as s:
            return (await s.execute(text(f"select {cols} from {table}"))).all()


class TestAssignment(DbBase):
    async def test_04_assign(self):
        await self.add_program()
        self.assertIsNone(await self.svc.get_assignment(ACCT))
        await self.svc.assign(ACCT, "p_mx")
        self.assertEqual(await self.svc.get_assignment(ACCT), "p_mx")

    async def test_05_replace(self):
        await self.add_program("p_mx"); await self.add_program("p_br", name="BetPartner BR")
        await self.svc.assign(ACCT, "p_mx"); await self.svc.assign(ACCT, "p_br")
        self.assertEqual(await self.svc.get_assignment(ACCT), "p_br")
        self.assertEqual(len(await self.rows("offer_account_assignments")), 1)

    async def test_06_remove(self):
        await self.add_program(); await self.svc.assign(ACCT, "p_mx")
        self.assertTrue(await self.svc.unassign(ACCT))
        self.assertIsNone(await self.svc.get_assignment(ACCT))
        self.assertFalse(await self.svc.unassign(ACCT))

    async def test_07_max_one_offer_per_account(self):
        await self.add_program("pa"); await self.add_program("pb"); await self.add_program("pc")
        for pid in ("pa", "pb", "pc", "pa"):
            await self.svc.assign(ACCT, pid)
        self.assertEqual(len(await self.rows("offer_account_assignments")), 1)
        async with self.sf() as s:                       # the DB itself refuses a second row for the same account
            from sqlalchemy.exc import IntegrityError
            with self.assertRaises(IntegrityError):
                await s.execute(store.offer_account_assignments.insert().values(account_ref=ACCT, program_id="pb", assigned_by="x"))
        other = account_ref_for(5)
        await self.svc.assign(other, "pa")                # a different account may use the same offer
        self.assertEqual(await self.svc.list_assignments(), {ACCT: "pa", other: "pa"})

    async def test_08_invalid_offer_and_account(self):
        with self.assertRaises(ProgramNotFound):
            await self.svc.assign(ACCT, "nope")
        await self.add_program()
        for bad in ("", "acct_zz", "bot_123456789012", "777000111", None):
            with self.assertRaises(AccountRefInvalid):
                await self.svc.assign(bad, "p_mx")

    async def test_21_deterministic_assignment(self):
        await self.add_program(); await self.svc.assign(ACCT, "p_mx")
        a = await resolve_assigned_offer(self.sf, ACCT); b = await resolve_assigned_offer(self.sf, ACCT)
        self.assertEqual(a, b)
        self.assertEqual((a.status, a.program_id, a.context.registration_url), (S.OFFER_ASSIGNED, "p_mx", URL))


class TestContent(DbBase):
    async def setUp_offer(self):
        await self.add_program()

    async def test_10_zero_images(self):
        await self.setUp_offer()
        c = await self.svc.set_content("p_mx", "desc", [])
        self.assertEqual((c["description"], c["images"]), ("desc", []))
        self.assertEqual((await self.svc.get_content("p_mx"))["images"], [])
        self.assertEqual(await self.svc.get_content("never_set"), {"description": None, "images": []})

    async def test_11_one_image(self):
        await self.setUp_offer()
        c = await self.svc.set_content("p_mx", None, ["https://i.example/1.jpg"])
        self.assertEqual([(i["url"], i["sort_order"], i["active"]) for i in c["images"]], [("https://i.example/1.jpg", 0, True)])

    async def test_12_13_multiple_images_and_ordering(self):
        await self.setUp_offer()
        urls = [f"https://i.example/{n}.jpg" for n in (3, 1, 2)]
        c = await self.svc.set_content("p_mx", "d", urls + [urls[0]])           # duplicate dropped, order kept
        self.assertEqual([i["url"] for i in c["images"]], urls)
        self.assertEqual([i["sort_order"] for i in c["images"]], [0, 1, 2])
        c = await self.svc.set_content("p_mx", "d", list(reversed(urls)))          # re-ordering replaces the list
        self.assertEqual([i["url"] for i in c["images"]], list(reversed(urls)))
        self.assertEqual(len(await self.rows("offer_images")), 3)
        ids = [i["image_id"] for i in c["images"]]
        self.assertEqual(ids, [i["image_id"] for i in (await self.svc.get_content("p_mx"))["images"]])

    async def test_09_invalid_image_urls_and_limits(self):
        await self.setUp_offer()
        for bad in (["javascript:alert(1)"], ["https://"], ["not a url"], [123], "https://x.com/a.jpg", ["data:image/png;base64,AAAA"],
                    [f"https://i.example/{n}.jpg" for n in range(11)]):
            with self.assertRaises(ContentValidationError, msg=str(bad)[:40]):
                await self.svc.set_content("p_mx", "d", bad)
        for bad in ("x" * 1001, "bad\x00char", 5):
            with self.assertRaises(ContentValidationError):
                await self.svc.set_content("p_mx", bad, [])
        self.assertEqual((await self.svc.get_content("p_mx"))["images"], [])        # failed saves changed nothing
        with self.assertRaises(ProgramNotFound):
            await self.svc.set_content("nope", "d", [])

    async def test_no_remote_fetch(self):
        await self.setUp_offer()
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network used")):
            await self.svc.set_content("p_mx", "d", ["https://i.example/1.jpg"])

    async def test_programs_table_untouched(self):
        await self.setUp_offer()
        before = await self.rows("partner_programs")
        await self.svc.set_content("p_mx", "d", ["https://i.example/1.jpg"]); await self.svc.assign(ACCT, "p_mx")
        self.assertEqual(before, await self.rows("partner_programs"))


class TestShadowPipeline(DbBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        hook._seen.clear()

    def on(self, shadow=True):
        os.environ["COPILOT_OFFER_ENGINE_ENABLED"] = "true"
        if shadow:
            os.environ["COPILOT_OFFER_SHADOW_ENABLED"] = "true"

    def mk(self, corr="pd_777", stage="visited", chat=555):
        return hook.build_input(chat_id=chat, user=NS(stage=stage), client_state=None, decision_id=corr, account_id=ACCT_ID)

    async def test_end_to_end_persisted_with_correlation(self):          # 21
        self.on()
        await self.add_program(); await self.svc.assign(ACCT, "p_mx")
        i = self.mk()
        res = await hook.run(self.sf, i)
        self.assertEqual((res.status, res.correlation_id), (S.OFFER_ASSIGNED, "pd_777"))
        rows = await self.rows(cols="correlation_id, client_ref, account_ref, program_id, status, reason, mode, policy_version")
        self.assertEqual(rows, [("pd_777", i.client_ref, ACCT, "p_mx", "OFFER_ASSIGNED", "OFFER_ASSIGNED", "shadow", "account_offer_v1")])
        self.assertIsNotNone((await self.rows(cols="created_at"))[0][0])

    async def test_each_runtime_state_recorded(self):
        self.on()
        self.assertEqual((await hook.run(self.sf, self.mk("pd_1"))).status, S.NO_OFFER_ASSIGNED)
        await self.add_program(active=False); await self.svc.assign(ACCT, "p_mx")
        self.assertEqual((await hook.run(self.sf, self.mk("pd_2"))).reason, R.INACTIVE)
        r = await hook.run(self.sf, hook.build_input(chat_id=1, user=NS(stage="new"), client_state=None, decision_id="pd_3", account_id=None))
        self.assertEqual((r.status, r.reason), (S.ACCOUNT_UNKNOWN, R.ACCOUNT_UNKNOWN))
        self.assertEqual([x[0] for x in await self.rows(cols="status")], ["NO_OFFER_ASSIGNED", "OFFER_UNAVAILABLE", "ACCOUNT_UNKNOWN"])

    async def test_invalid_offer_in_db_is_unavailable(self):
        self.on()
        await self.add_program(); await self.svc.assign(ACCT, "p_mx")
        async with self.sf() as s:                                         # a stored program that bypassed validation
            await s.execute(text("update partner_programs set registration_url='http://127.0.0.1/x' where program_id='p_mx'"))
            await s.commit()
        r = await hook.run(self.sf, self.mk())
        self.assertEqual((r.status, r.reason), (S.OFFER_UNAVAILABLE, R.INVALID))

    async def test_22_duplicate_hook_does_not_duplicate_decision(self):
        self.on()
        await self.add_program(); await self.svc.assign(ACCT, "p_mx")
        self.assertIsNotNone(await hook.run(self.sf, self.mk("pd_dup")))
        self.assertIsNone(await hook.run(self.sf, self.mk("pd_dup")))        # same process: in-memory claim
        hook._seen.clear()                                                    # "restart": the DB guard still holds
        self.assertIsNone(await hook.run(self.sf, self.mk("pd_dup")))
        self.assertEqual(len(await self.rows()), 1)
        self.assertIsNotNone(await hook.run(self.sf, self.mk("pd_other")))
        self.assertEqual(len(await self.rows()), 2)

    async def test_22b_concurrent_duplicate_hooks(self):
        self.on()
        await self.add_program(); await self.svc.assign(ACCT, "p_mx")
        out = await asyncio.gather(*[hook.run(self.sf, self.mk("pd_race")) for _ in range(5)])
        self.assertEqual(sum(o is not None for o in out), 1)
        self.assertEqual(len(await self.rows()), 1)

    async def test_23_failopen_resolver_and_persist(self):
        self.on()
        async def boom(*a, **k):
            raise RuntimeError("db down")
        with self.assertLogs("copilot_offers", "INFO") as cm:
            self.assertIsNone(await hook.run(self.sf, self.mk("pd_f1"), loader=boom))
        self.assertTrue(any("OFFER_DECISION_ERROR" in m for m in cm.output))
        self.assertNotIn("pd_f1", hook._seen)                                # a failed run may be retried
        async def loader(*a): return resolve(program(), content())
        with self.assertLogs("copilot_offers", "INFO") as cm:
            r = await hook.run(self.sf, self.mk("pd_f2"), loader=loader, persister=boom)
        self.assertEqual(r.status, S.OFFER_ASSIGNED)
        self.assertTrue(any("OFFER_DECISION_ERROR" in m for m in cm.output))

    async def test_23b_real_db_outage_failopen(self):
        self.on()
        def broken():
            raise ConnectionError("database unavailable")
        with self.assertLogs("copilot_offers", "INFO") as cm:
            r = await hook.run(broken, self.mk("pd_down"))
        self.assertIsNone(r)
        self.assertTrue(any("OFFER_DECISION_ERROR" in m for m in cm.output))
        self.assertFalse(any("password" in m.lower() or "postgres" in m.lower() for m in cm.output))

    async def test_23c_hook_never_raises(self):
        self.on()
        with mock.patch.object(hook, "build_input", side_effect=RuntimeError("x")):
            self.assertFalse(hook.schedule_offer_shadow(session_factory=self.sf, chat_id=1, user=NS(stage="new")))
        self.assertFalse(hook.schedule_offer_shadow(session_factory=None, chat_id="bad", user=None))

    async def test_engine_off_bypasses_everything(self):
        called = []
        with mock.patch.object(hook, "build_input", side_effect=lambda **k: called.append(1)):
            self.assertFalse(hook.schedule_offer_shadow(session_factory=self.sf, chat_id=1, user=NS(stage="new")))
        os.environ["COPILOT_OFFER_SHADOW_ENABLED"] = "true"                       # shadow alone does not enable the engine
        with mock.patch.object(hook, "build_input", side_effect=lambda **k: called.append(1)):
            self.assertFalse(hook.schedule_offer_shadow(session_factory=self.sf, chat_id=1, user=NS(stage="new")))
        self.assertEqual((called, await self.rows(), len(hook._tasks)), ([], [], 0))

    async def test_24_shadow_disabled_means_no_decision_row(self):
        self.on(shadow=False)
        await self.add_program(); await self.svc.assign(ACCT, "p_mx")
        with self.assertLogs("copilot_offers", "INFO") as cm:
            r = await hook.run(self.sf, self.mk())
        self.assertEqual(r.status, S.OFFER_ASSIGNED)
        self.assertEqual(await self.rows(), [])
        self.assertTrue(any(m.split(":", 2)[2].startswith("OFFER_DECISION ") for m in cm.output))

    async def test_25b_real_flag_changes_nothing(self):
        os.environ["COPILOT_OFFER_REAL_ENABLED"] = "true"
        self.assertTrue(events.real_enabled())
        self.assertEqual(decide(inp(), resolve(program())).mode, "shadow")
        self.assertFalse(hook.schedule_offer_shadow(session_factory=self.sf, chat_id=1, user=NS(stage="new")))      # engine still off
        os.environ["COPILOT_OFFER_REAL_ENABLED"] = "false"
        self.assertFalse(events.real_enabled())

    async def test_26_no_secrets_or_raw_identifiers_in_rows_and_logs(self):
        self.on()
        await self.add_program(); await self.svc.assign(ACCT, "p_mx")
        await self.svc.set_content("p_mx", "DESC_MARK", ["https://i.example/1.jpg"])
        with self.assertLogs("copilot_offers", "INFO") as cm:
            await hook.run(self.sf, self.mk("pd_priv", chat=424242424))
        dump = json.dumps([list(map(str, r)) for r in await self.rows()]) + "\n".join(cm.output)
        for needle in ("424242424", str(ACCT_ID), URL, "partner.example", "PROMO", "DESC_MARK", "i.example", "TERMS", "token", "session", "BetPartner"):
            self.assertNotIn(needle, dump, needle)

    async def test_scheduled_task_runs(self):
        self.on()
        await self.add_program(); await self.svc.assign(ACCT, "p_mx")
        self.assertTrue(hook.schedule_offer_shadow(session_factory=self.sf, chat_id=9, user=NS(stage="visited"),
                                                   decision_id="pd_9", account_id=ACCT_ID))
        await asyncio.gather(*list(hook._tasks))
        self.assertEqual(len(await self.rows()), 1)

    async def test_no_migration_needed_schema_stable(self):
        await migrate.ensure_schema(self.engine)
        from sqlalchemy.dialects import postgresql
        sql = " ".join(migrate.plan(postgresql.dialect()))
        for needle in ("offer_account_assignments", "offer_details", "offer_images", "offer_decisions", "ix_offer_decisions_correlation_id"):
            self.assertIn(needle, sql)
        self.assertNotIn("ALTER", sql.upper()); self.assertNotIn("partner_programs", sql)

    async def test_append_only_contract_in_plan(self):
        from sqlalchemy.dialects import postgresql
        sql = " ".join(migrate.plan(postgresql.dialect()))
        self.assertIn("BEFORE UPDATE OR DELETE ON offer_decisions", sql)
        src = "".join(p.read_text() for p in (ROOT / "copilot_offers").glob("*.py") if p.name != "store.py")
        self.assertNotIn("offer_decisions.update", src); self.assertNotIn("offer_decisions.delete", src)


class TestApi(DbBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.audits = []

        async def admin(request: Request):
            if request.headers.get("X-Admin-Token") != "good":
                raise HTTPException(401, "no")

        async def audit(action, ip=None, details=None, user_id=None):
            self.audits.append((action, details))
        app = FastAPI()
        app.include_router(build_router(admin, self.sf, audit, lambda: ACCT_ID))
        self.c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    async def asyncTearDown(self):
        await self.c.aclose()
        await super().asyncTearDown()

    async def test_auth_required(self):
        for m, u in (("GET", "/api/offers"), ("GET", "/api/offers/accounts"), ("PUT", f"/api/offers/accounts/{ACCT}/assignment"),
                     ("DELETE", f"/api/offers/accounts/{ACCT}/assignment"), ("PUT", "/api/offers/p/content")):
            self.assertEqual((await self.c.request(m, u, json={})).status_code, 401, u)

    async def test_full_flow(self):
        await self.add_program("p_mx"); await self.add_program("p_br", name="BetPartner BR")
        r = await self.c.get("/api/offers/accounts", headers=H)
        self.assertEqual(r.json()["accounts"], [{"account_ref": ACCT, "label": "COPILOT Telegram account (current)", "is_current": True, "program_id": None}])
        r = await self.c.put("/api/offers/p_mx/content", headers=H, json={"description": "Great", "images": ["https://i.example/1.jpg", {"url": "https://i.example/2.jpg", "active": False}]})
        self.assertEqual([i["sort_order"] for i in r.json()["content"]["images"]], [0, 1])
        r = await self.c.put(f"/api/offers/accounts/{ACCT}/assignment", headers=H, json={"program_id": "p_mx"})
        self.assertEqual(r.status_code, 200)
        r = await self.c.put(f"/api/offers/accounts/{ACCT}/assignment", headers=H, json={"program_id": "p_br"})        # replace
        offers = {o["program_id"]: o for o in (await self.c.get("/api/offers", headers=H)).json()["offers"]}
        self.assertEqual((offers["p_br"]["used_by"], offers["p_mx"]["used_by"]), ([ACCT], []))
        self.assertEqual(offers["p_mx"]["description"], "Great")
        for o in offers.values():
            self.assertNotIn("terms", o); self.assertNotIn("notes", o)
        self.assertEqual((await self.c.delete(f"/api/offers/accounts/{ACCT}/assignment", headers=H)).json()["removed"], True)
        self.assertEqual([a[0] for a in self.audits], ["offer_content_updated", "account_offer_assigned", "account_offer_assigned", "account_offer_removed"])

    async def test_errors(self):
        await self.add_program()
        r = await self.c.put(f"/api/offers/accounts/{ACCT}/assignment", headers=H, json={"program_id": "nope"})
        self.assertEqual((r.status_code, r.json()["detail"]["error"]), (404, "program_not_found"))
        r = await self.c.put(f"/api/offers/accounts/{account_ref_for(9)}/assignment", headers=H, json={"program_id": "p_mx"})
        self.assertEqual((r.status_code, r.json()["detail"]["error"]), (404, "account_unknown"))
        r = await self.c.put("/api/offers/accounts/bad/assignment", headers=H, json={"program_id": "p_mx"})
        self.assertEqual(r.status_code, 404)           # unknown account is refused before anything is written
        for bad in ("javascript:x", "http://127.0.0.1/a.png", "http://localhost/a.png", "http://10.0.0.1/a.png", "data:image/png;base64,AA"):
            r = await self.c.put("/api/offers/p_mx/content", headers=H, json={"images": [bad]})
            self.assertEqual(r.status_code, 400, bad)
        r = await self.c.put("/api/offers/p_mx/content", headers=H, json={"description": "d", "terms": "x"})
        self.assertEqual(r.status_code, 400)
        r = await self.c.put("/api/offers/nope/content", headers=H, json={"description": "d"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(await self.rows("offer_account_assignments"), [])


if __name__ == "__main__":
    unittest.main()
