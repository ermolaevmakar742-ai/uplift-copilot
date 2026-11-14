import ast
import asyncio
import json
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from copilot_acquisition import contract as C
from copilot_acquisition import intake, research, store
from copilot_acquisition.router import build_router
from copilot_shadow import store as shadow_store

ROOT = Path(__file__).resolve().parents[2]
ENV = {C.FLAG: "true", "COPILOT_ACQ_ALLOWED_DESTINATIONS": "valeria_test"}
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        store._ready.clear(); shadow_store._ready.clear()
        self.env = mock.patch.dict(os.environ, ENV); self.env.start()
        async with self.engine.begin() as c:        # stand-ins for main.py's existing tables (raw SQL only touches these columns)
            await c.execute(text("CREATE TABLE attribution_clicks (id INTEGER PRIMARY KEY AUTOINCREMENT, click_id VARCHAR(128) NOT NULL UNIQUE,"
                                 " source VARCHAR(128), campaign VARCHAR(128), placement VARCHAR(128), creative VARCHAR(128), user_id BIGINT,"
                                 " clicked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL, registered_at TIMESTAMP, converted_at TIMESTAMP,"
                                 " revenue FLOAT NOT NULL)"))
            await c.execute(text("CREATE TABLE users (id BIGINT PRIMARY KEY, client_id VARCHAR(64), attribution_click_id VARCHAR(128),"
                                 " attribution_source VARCHAR(128), attribution_campaign VARCHAR(128), attribution_placement VARCHAR(128),"
                                 " attribution_creative VARCHAR(128))"))
        await store.ensure_tables(self.sf)
        async with self.sf() as s:
            await s.execute(text("INSERT INTO users (id, client_id) VALUES (1001, 'cli_aaaa'), (1002, 'cli_bbbb'), (1003, NULL)")); await s.commit()
        self.app = FastAPI(); self.app.include_router(build_router(self.sf))

    async def asyncTearDown(self):
        self.env.stop(); await self.engine.dispose()

    async def campaign(self, **k):
        d = dict(source="tiktok", medium="ubt", campaign_name="Spring 1", creative_id="cr_01", destination="valeria_test")
        d.update(k)
        return await store.create_campaign(self.sf, **d)

    async def get(self, path):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://t", follow_redirects=False) as c:
            return await c.get(path)

    async def click(self, camp=None):
        camp = camp or await self.campaign()
        r = await self.get("/go/" + camp["token"])
        q = parse_qs(urlparse(r.headers["location"]).query)
        token = unquote(q["text"][0]).split("COPILOTREF:")[1]
        return camp, token

    async def q(self, sql, **p):
        async with self.sf() as s:
            return (await s.execute(text(sql), p)).mappings().all()


class TestCampaign(Base):
    async def test_create_lookup_and_registry_fields(self):
        c = await self.campaign()
        self.assertRegex(c["campaign_id"], r"^cmp_[0-9a-f]{8}$"); self.assertEqual(len(c["token"]), C.TOKEN_LEN)
        self.assertEqual((c["status"], c["registry_version"]), ("active", "acquisition-1"))
        got = await store.get_campaign_by_token(self.sf, c["token"])
        self.assertEqual((got["source"], got["medium"], got["creative_id"], got["destination"]), ("tiktok", "ubt", "cr_01", "valeria_test"))
        self.assertNotIn(c["campaign_id"], c["token"]); self.assertNotIn("tiktok", c["token"].lower())
        self.assertIsNone(await store.get_campaign_by_token(self.sf, "x" * 12)); self.assertIsNone(await store.get_campaign_by_token(self.sf, "bad"))

    async def test_invalid_campaigns_rejected(self):
        for bad in ({"source": "a@b.co"}, {"source": "https://x.y"}, {"medium": ""}, {"campaign_name": "x" * 80}, {"creative_id": None},
                    {"destination": "not_allowlisted"}, {"destination": "http://evil.com"}, {"destination": "a b"}, {"status": "weird"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                await self.campaign(**bad)

    async def test_source_is_a_free_string_not_an_enum(self):
        for src in ("tiktok", "partner_x", "organic", "Telegram"):
            self.assertEqual((await self.campaign(source=src))["source"], src.lower())

    async def test_disabled_campaign_cannot_issue(self):
        c = await self.campaign()
        await store.set_campaign_status(self.sf, c["campaign_id"], "paused")
        with self.assertRaises(ValueError):
            await store.issue_click(self.sf, await store.get_campaign_by_token(self.sf, c["token"]))


class TestRedirect(Base):
    async def test_valid_token_creates_click_and_redirects_to_telegram(self):
        c = await self.campaign()
        r = await self.get("/go/" + c["token"])
        self.assertEqual(r.status_code, 302)
        u = urlparse(r.headers["location"])
        self.assertEqual((u.scheme, u.netloc, u.path), ("https", "t.me", "/valeria_test"))
        self.assertEqual(r.headers["cache-control"], "no-store")
        legacy = await self.q("SELECT click_id, source, campaign, creative, user_id, revenue FROM attribution_clicks")
        side = await self.q("SELECT click_id, campaign_id, status, token_hash, expires_at FROM acquisition_clicks")
        self.assertEqual(len(legacy), 1); self.assertEqual(len(side), 1)
        self.assertEqual(legacy[0]["click_id"], side[0]["click_id"])
        self.assertEqual((legacy[0]["source"], legacy[0]["campaign"], legacy[0]["creative"], legacy[0]["user_id"], legacy[0]["revenue"]),
                         ("tiktok", c["campaign_id"], "cr_01", None, 0))
        self.assertEqual(side[0]["status"], "HANDOFF_GENERATED")

    async def test_every_redirect_gets_a_unique_click_and_token(self):
        c = await self.campaign()
        seen = set()
        for _ in range(5):
            r = await self.get("/go/" + c["token"]); seen.add(r.headers["location"])
        self.assertEqual(len(seen), 5)
        self.assertEqual(len({r["click_id"] for r in await self.q("SELECT click_id FROM acquisition_clicks")}), 5)

    async def test_invalid_unknown_paused_and_disabled_flag_make_no_click(self):
        c = await self.campaign()
        for path in ("/go/bad", "/go/" + "A" * 12, "/go/" + "A" * 40, "/go/%2e%2e"):
            self.assertEqual((await self.get(path)).status_code, 404)
        await store.set_campaign_status(self.sf, c["campaign_id"], "paused")
        self.assertEqual((await self.get("/go/" + c["token"])).status_code, 404)
        await store.set_campaign_status(self.sf, c["campaign_id"], "active")
        with mock.patch.dict(os.environ, {C.FLAG: "false"}):
            self.assertEqual((await self.get("/go/" + c["token"])).status_code, 404)
        self.assertEqual(await self.q("SELECT click_id FROM acquisition_clicks"), [])
        self.assertEqual(await self.q("SELECT click_id FROM attribution_clicks"), [])

    async def test_no_open_redirect_destination_only_from_allowlisted_registry(self):
        c = await self.campaign()
        async with self.sf() as s:                                   # even a tampered registry row cannot redirect elsewhere
            await s.execute(text("UPDATE acquisition_campaigns SET destination = 'evil_site' WHERE campaign_id = :c"), {"c": c["campaign_id"]}); await s.commit()
        self.assertEqual((await self.get("/go/" + c["token"])).status_code, 404)
        with mock.patch.dict(os.environ, {"COPILOT_ACQ_ALLOWED_DESTINATIONS": ""}):
            self.assertFalse(C.valid_destination("valeria_test"))
        r = await self.get("/go/" + (await self.campaign())["token"] + "?to=http://evil.com&next=//evil.com")
        self.assertEqual(urlparse(r.headers["location"]).netloc, "t.me")

    async def test_rate_limit_is_fail_closed(self):
        from copilot_acquisition import router as R
        c = await self.campaign()
        R._hits.clear()
        with mock.patch.object(R, "MAX_PER_MINUTE", 2):
            codes = [(await self.get("/go/" + c["token"])).status_code for _ in range(4)]
        R._hits.clear()
        self.assertEqual(codes, [302, 302, 404, 404])


class TestHandoffAndParser(Base):
    async def test_handoff_url_and_token_have_no_pii(self):
        c = await self.campaign(campaign_name="Spring Secret", source="tiktok")
        r = await self.get("/go/" + c["token"])
        loc = r.headers["location"]
        draft = unquote(parse_qs(urlparse(loc).query)["text"][0])
        self.assertRegex(draft, r"^Hola 👋 COPILOTREF:[A-Za-z0-9_-]{12}$")
        for needle in ("Secret", "tiktok", c["campaign_id"], c["token"], "cr_01", "1001"):
            self.assertNotIn(needle, loc + draft)
        side = (await self.q("SELECT * FROM acquisition_clicks"))[0]
        self.assertNotIn(draft.split(":")[1], json.dumps(dict(side), default=str))     # only the hash is stored, never the token
        self.assertNotIn("handoff", " ".join(side.keys()).replace("handoff_at", ""))   # no stored URL

class TestBinding(Base):
    async def bind(self, token, chat, **k):
        return await store.bind_token(self.sf, token, chat, wait_s=0.01, retries=2, **k)

    async def test_bind_links_click_to_the_existing_lead_identity_without_telegram_id_in_new_tables(self):
        camp, token = await self.click()
        self.assertEqual(await self.bind(token, 1001), "bound")
        click = (await self.q("SELECT * FROM acquisition_clicks"))[0]
        self.assertEqual((click["status"], click["lead_ref"]), ("BOUND", "cli_aaaa"))
        self.assertEqual((await self.q("SELECT user_id, registered_at FROM attribution_clicks"))[0]["user_id"], 1001)    # legacy table, as before
        u = (await self.q("SELECT * FROM users WHERE id = 1001"))[0]
        self.assertEqual((u["attribution_click_id"], u["attribution_source"], u["attribution_campaign"], u["attribution_creative"]),
                         (click["click_id"], "tiktok", camp["campaign_id"], "cr_01"))
        for t in ("acquisition_campaigns", "acquisition_clicks", "acquisition_leads"):
            rows = [dict(r) for r in await self.q(f"SELECT * FROM {t}")]
            for row in rows:
                self.assertFalse({"user_id", "telegram_id", "telegram_user_id"} & set(row))
                self.assertNotIn(1001, row.values())
                # Random opaque identifiers may coincidentally contain the decimal substring "1001".
                self.assertNotIn("1001", [str(v) for k, v in row.items() if k not in {"click_id", "token_hash"}])

    async def test_repeated_bind_is_idempotent_and_token_binds_one_lead_only(self):
        _, token = await self.click()
        self.assertEqual(await self.bind(token, 1001), "bound")
        self.assertEqual(await self.bind(token, 1001), "already_bound")
        self.assertEqual(await self.bind(token, 1002), "token_reused")
        (lead,) = await self.q("SELECT * FROM acquisition_leads")
        self.assertEqual(lead["touches"], 1)
        self.assertEqual((await self.q("SELECT user_id FROM attribution_clicks"))[0]["user_id"], 1001)

    async def test_first_touch_preserved_last_touch_updated(self):
        c1, t1 = await self.click(); c2, t2 = await self.click(await self.campaign(source="instagram", creative_id="cr_02"))
        self.assertEqual(await self.bind(t1, 1001, now=T0 + timedelta(hours=1)) if False else await self.bind(t1, 1001), "bound")
        self.assertEqual(await self.bind(t2, 1001), "bound")
        (lead,) = await self.q("SELECT * FROM acquisition_leads")
        self.assertEqual((lead["first_campaign_id"], lead["last_campaign_id"], lead["touches"]), (c1["campaign_id"], c2["campaign_id"], 2))
        self.assertNotEqual(lead["first_click_id"], lead["last_click_id"])
        u = (await self.q("SELECT attribution_campaign FROM users WHERE id = 1001"))[0]
        self.assertEqual(u["attribution_campaign"], c1["campaign_id"])                    # legacy field also keeps the first touch
        self.assertEqual(len(await self.q("SELECT * FROM acquisition_leads")), 1)

    async def test_invalid_expired_unknown_and_not_ready(self):
        _, token = await self.click()
        self.assertEqual(await self.bind("short", 1001), "invalid_token")
        self.assertEqual(await self.bind("A" * 12, 1001), "invalid_token")
        self.assertEqual(await self.bind(token, 1003), "lead_not_ready")                   # users row without client_id yet
        self.assertEqual(await self.bind(token, 9999), "lead_not_ready")
        self.assertEqual((await self.q("SELECT status FROM acquisition_clicks"))[0]["status"], "HANDOFF_GENERATED")
        late = datetime.now(timezone.utc) + timedelta(seconds=C.token_ttl_seconds() + 5)
        self.assertEqual(await self.bind(token, 1001, now=late), "expired")
        self.assertEqual((await self.q("SELECT status FROM acquisition_clicks"))[0]["status"], "EXPIRED")
        self.assertEqual(await self.bind(token, 1001), "not_bindable")
        self.assertEqual(await self.q("SELECT * FROM acquisition_leads"), [])

    async def test_state_machine_unbound_and_converted(self):
        _, t1 = await self.click(); _, t2 = await self.click()
        await self.bind(t1, 1001)
        click_id = (await self.q("SELECT click_id FROM acquisition_clicks WHERE status = 'BOUND'"))[0]["click_id"]
        self.assertTrue(await store.mark_converted(self.sf, click_id)); self.assertFalse(await store.mark_converted(self.sf, click_id))
        self.assertEqual(await store.expire_unbound(self.sf, datetime.now(timezone.utc) + timedelta(days=30)), 1)
        self.assertEqual(sorted(r["status"] for r in await self.q("SELECT status FROM acquisition_clicks")), ["CONVERTED", "UNBOUND"])

    async def test_token_entropy_and_format(self):
        toks = {C.new_token() for _ in range(2000)}
        self.assertEqual(len(toks), 2000)
        self.assertTrue(all(C.TOKEN_RE.match(t) for t in toks))
        self.assertEqual(len({C.new_click_id() for _ in range(500)}), 500)


class TestResearchChain(Base):
    async def test_click_to_lead_to_decision_to_outcome_to_reward(self):
        camp, token = await self.click()
        await self.bind(token, 1001) if False else await store.bind_token(self.sf, token, 1001, wait_s=0.01, retries=2)
        await shadow_store.ensure_table(self.sf)
        row = lambda did, msg, **k: {"decision_id": did, "chat_id": 1001, "message_id": msg, "state_snapshot": {}, "candidate_actions": [],
                                     "chosen_action": "WAIT", "wait_baseline": {}, "decision_status": "x", "shadow_mode": True,
                                     "model_name": "m", "model_version": "1", "analyzer_version": "a", "code_version": "c", "dedup_key": "dk" + did, **k}
        await shadow_store.save_decision(self.sf, row("pd_a", 1, outcome="DEPOSIT", outcome_status="observed", outcome_ref="postback:1001:deposit_success:9",
                                                      reward=1.0, reward_version="reward.v1"))
        await shadow_store.save_decision(self.sf, row("pd_b", 2))
        await shadow_store.save_decision(self.sf, {**row("pd_other", 3), "chat_id": 1002})
        click_id = (await self.q("SELECT click_id FROM acquisition_clicks"))[0]["click_id"]
        chain = await research.lead_chain(self.sf, click_id)
        self.assertEqual((chain["campaign_id"], chain["source"], chain["status"], chain["lead_ref"]), (camp["campaign_id"], "tiktok", "BOUND", "cli_aaaa"))
        self.assertEqual([d["decision_id"] for d in chain["decisions"]], ["pd_a", "pd_b"])      # only this lead's decisions
        a = chain["decisions"][0]
        self.assertEqual((a["outcome"], a["reward"], a["reward_basis"], a["reward_version"]), ("DEPOSIT", 1.0, "original", "reward.v1"))
        self.assertIsNone(chain["decisions"][1]["reward"]); self.assertIsNone(chain["decisions"][1]["outcome"])      # pending: no fake reward
        dump = json.dumps(chain)
        self.assertNotIn("1001", dump); self.assertNotIn("postback:", dump)                       # no Telegram id, no raw outcome id

    async def test_delayed_correction_flows_through_and_revenue_is_not_a_reward_source(self):
        camp, token = await self.click()
        await store.bind_token(self.sf, token, 1001, wait_s=0.01, retries=2)
        await shadow_store.ensure_table(self.sf)
        await shadow_store.save_decision(self.sf, {"decision_id": "pd_c", "chat_id": 1001, "message_id": 1, "state_snapshot": {}, "candidate_actions": [],
            "chosen_action": "WAIT", "wait_baseline": {}, "decision_status": "x", "shadow_mode": True, "model_name": "m", "model_version": "1",
            "analyzer_version": "a", "code_version": "c", "dedup_key": "dkc", "reward": 1.0, "reward_version": "reward.v1",
            "reward_corrections": [{"corrected_reward": 0.25, "reward_version": "reward.v1", "event_ids": ["cb"]}]})
        async with self.sf() as s:
            await s.execute(text("UPDATE attribution_clicks SET revenue = 999")); await s.commit()
        click_id = (await self.q("SELECT click_id FROM acquisition_clicks"))[0]["click_id"]
        d = (await research.lead_chain(self.sf, click_id))["decisions"][0]
        self.assertEqual((d["reward"], d["reward_basis"]), (0.25, "corrected"))
        (summ,) = await research.campaign_summary(self.sf)
        self.assertEqual((summ["clicks"], summ["bound_clicks"], summ["first_touch_leads"], summ["decisions"], summ["reward_sum"]), (1, 1, 1, 1, 0.25))

    async def test_unbound_click_has_no_lead_and_unknown_click_is_none(self):
        camp, _ = await self.click()
        click_id = (await self.q("SELECT click_id FROM acquisition_clicks"))[0]["click_id"]
        chain = await research.lead_chain(self.sf, click_id)
        self.assertEqual((chain["lead_ref"], chain["decisions"], chain["status"]), (None, [], "HANDOFF_GENERATED"))
        self.assertIsNone(await research.lead_chain(self.sf, "clk_missing"))


