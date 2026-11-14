import ast
import json
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from copilot_acquisition import api as A
from copilot_acquisition import queries as Q
from copilot_acquisition import store
from copilot_outcomes.store import metadata as outcomes_md
from copilot_shadow import store as shadow_store

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.now(timezone.utc)
D = lambda days=0, hours=0: (NOW - timedelta(days=days, hours=hours)).astimezone(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


async def _admin(x_admin_token: str = Header(default="")):
    if x_admin_token != "good":
        raise HTTPException(status_code=401, detail="Missing token")
    return x_admin_token


class DB(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        store._ready.clear(); shadow_store._ready.clear(); A._cache.clear()
        self.patch = mock.patch.object(A, "CACHE_TTL", 0.0); self.patch.start()
        async with self.engine.begin() as c:
            await c.execute(text("CREATE TABLE attribution_clicks (id INTEGER PRIMARY KEY AUTOINCREMENT, click_id VARCHAR(128) NOT NULL UNIQUE,"
                                 " source VARCHAR(128), campaign VARCHAR(128), placement VARCHAR(128), creative VARCHAR(128), user_id BIGINT,"
                                 " clicked_at TIMESTAMP NOT NULL, registered_at TIMESTAMP, converted_at TIMESTAMP, revenue FLOAT NOT NULL)"))
            await c.execute(text("CREATE TABLE users (id BIGINT PRIMARY KEY, client_id VARCHAR(64), attribution_click_id VARCHAR(128))"))
            await c.execute(text("CREATE TABLE funnel_events (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id BIGINT, old_stage VARCHAR(32), new_stage VARCHAR(32), created_at TIMESTAMP)"))
            await c.run_sync(outcomes_md.create_all)
        await store.ensure_tables(self.sf); await shadow_store.ensure_table(self.sf)
        self.app = FastAPI(); self.app.include_router(A.build_router(_admin, self.sf))

    async def asyncTearDown(self):
        self.patch.stop(); await self.engine.dispose()

    async def x(self, sql, **p):
        async with self.sf() as s:
            await s.execute(text(sql), p); await s.commit()

    async def q(self, sql, **p):
        async with self.sf() as s:
            return (await s.execute(text(sql), p)).mappings().all()

    async def campaign(self, cid, source, medium, name, creative):
        await self.x("INSERT INTO acquisition_campaigns (campaign_id, registry_version, source, medium, campaign_name, creative_id, destination, token, status)"
                     " VALUES (:i,'acquisition-1',:s,:m,:n,:c,'valeria_test',:t,'active')", i=cid, s=source, m=medium, n=name, c=creative, t=cid[-8:] + "tok1234")

    async def click(self, cid, campaign=None, source=None, creative=None, at=None, registered=None):
        await self.x("INSERT INTO attribution_clicks (click_id, source, campaign, creative, clicked_at, registered_at, revenue) VALUES (:i,:s,:c,:cr,:a,:r,777)",
                     i=cid, s=source, c=campaign, cr=creative, a=at or D(1), r=registered)

    async def lead(self, chat, ref, click_id, at=None):
        await self.x("INSERT INTO users (id, client_id) VALUES (:i,:r)", i=chat, r=ref)
        await self.x("INSERT INTO acquisition_leads (lead_ref, first_click_id, first_campaign_id, first_touch_at, last_click_id, last_campaign_id, last_touch_at, touches)"
                     " VALUES (:r,:c,'x',:a,:c,'x',:a,1)", r=ref, c=click_id, a=at or D(1))

    async def stage(self, chat, new, at=None):
        await self.x("INSERT INTO funnel_events (user_id, new_stage, created_at) VALUES (:u,:s,:a)", u=chat, s=new, a=at or D(0, 5))

    async def outcome(self, chat, typ, oid, at=None):
        await self.x("INSERT INTO outcomes (outcome_id, chat_id, observed_at, outcome_type, value, source, contract_version, metadata)"
                     " VALUES (:o,:c,:a,:t,'null','POSTBACK','outcome.v1','{}')", o=oid, c=chat, a=at or D(0, 4), t=typ)

    async def decision(self, did, chat, reward=None, rv="reward.v1", status="final", corr=None, otime=None):
        det = json.dumps({"status": status}) if reward is not None else None
        await shadow_store.save_decision(self.sf, {
            "decision_id": did, "chat_id": chat, "message_id": hash(did) % 10**6, "state_snapshot": {}, "candidate_actions": [], "chosen_action": "WAIT",
            "wait_baseline": {}, "decision_status": "x", "shadow_mode": True, "model_name": "m", "model_version": "1", "analyzer_version": "a",
            "code_version": "c", "dedup_key": "dk" + did, "reward": reward, "reward_version": rv if reward is not None else None,
            "reward_detail": json.loads(det) if det else None, "reward_corrections": corr,
            "outcome_timestamp": datetime.strptime(otime, "%Y-%m-%d %H:%M:%S.%f") if otime else None})

    async def get(self, path, token="good"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://t") as c:
            return await c.get(path, headers={"X-Admin-Token": token} if token else {})

    async def summary(self, qs=""):
        r = await self.get("/api/acquisition/summary?period=all" + qs)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    async def seed_basic(self):
        await self.campaign("cmp_aaaa1111", "tiktok", "ubt", "Spring", "cr_01"); await self.campaign("cmp_bbbb2222", "instagram", "paid", "Reels", "cr_02")
        await self.click("c1", "cmp_aaaa1111", "tiktok", "cr_01"); await self.click("c2", "cmp_aaaa1111", "tiktok", "cr_01")
        await self.click("c3", "cmp_bbbb2222", "instagram", "cr_02"); await self.click("c4", "cmp_bbbb2222", "instagram", "cr_02")
        await self.click("c5", None, "partner_x", None); await self.click("c6")                                  # legacy + Unknown
        await self.lead(1001, "cli_a", "c1"); await self.lead(1002, "cli_b", "c3")


class TestEmptyAndBasics(DB):
    async def test_empty_dataset_and_zero_clicks(self):
        s = await self.summary()
        self.assertTrue(s["empty"]); self.assertEqual(s["kpi"]["clicks"], 0)
        self.assertIsNone(s["kpi"]["reward"]); self.assertIsNone(s["kpi"]["reward_per_lead"]); self.assertIsNone(s["kpi"]["deposit_rate"])
        self.assertEqual([f["count"] for f in s["funnel"]], [0, 0, 0, 0, 0])
        self.assertEqual((await self.get("/api/acquisition/sources?period=all")).json()["rows"], [])
        self.assertFalse((await self.get("/api/acquisition/timeseries?metric=clicks&period=all")).json()["enough"])

    async def test_clicks_without_leads(self):
        await self.click("c1", None, "tiktok"); await self.click("c2", None, "tiktok")
        s = await self.summary()
        self.assertFalse(s["empty"])
        k = s["kpi"]
        self.assertEqual((k["clicks"], k["leads"], k["qualified"], k["registrations"], k["deposits"]), (2, 0, 0, 0, 0))
        self.assertIsNone(k["deposit_rate"]); self.assertIsNone(k["reward"])                  # unknown, not 0
        self.assertEqual(s["funnel"][1]["conversion_from_previous"], 0.0)

    async def test_click_lead_qualified_registration_deposit_funnel(self):
        await self.seed_basic()
        await self.stage(1001, "conversation"); await self.stage(1001, "offer"); await self.stage(1002, "conversation")
        await self.outcome(1001, "REGISTRATION", "pb:1:reg"); await self.outcome(1001, "DEPOSIT", "pb:1:dep1")
        k = (await self.summary())["kpi"]
        self.assertEqual((k["clicks"], k["leads"], k["qualified"], k["registrations"], k["deposits"]), (6, 2, 1, 1, 1))
        f = (await self.summary())["funnel"]
        self.assertEqual([x["stage"] for x in f], ["clicks", "leads", "qualified", "registrations", "deposits"])
        self.assertEqual([x["conversion_from_previous"] for x in f], [None, round(2 / 6, 4), 0.5, 1.0, 1.0])
        self.assertEqual(k["deposit_rate"], 0.5)

    async def test_qualified_is_offer_or_later_only_and_once_per_lead(self):
        await self.seed_basic()
        for st in ("new", "conversation", "qualified", "objection", "lost"):
            await self.stage(1001, st)
        self.assertEqual((await self.summary())["kpi"]["qualified"], 0)
        await self.stage(1001, "offer"); await self.stage(1001, "deposited"); await self.stage(1001, "offer")
        self.assertEqual((await self.summary())["kpi"]["qualified"], 1)

    async def test_duplicate_deposit_not_double_counted(self):
        await self.seed_basic()
        for i in range(3):
            await self.outcome(1001, "DEPOSIT", f"pb:dep{i}"); await self.outcome(1001, "REGISTRATION", f"pb:reg{i}")
        k = (await self.summary())["kpi"]
        self.assertEqual((k["deposits"], k["registrations"], k["leads"]), (1, 1, 2))

    async def test_lead_counted_once_under_its_first_touch_click(self):
        await self.seed_basic()
        await self.x("UPDATE acquisition_leads SET last_click_id = 'c5' WHERE lead_ref = 'cli_a'")      # later touch elsewhere
        rows = {r["key"]: r for r in (await self.get("/api/acquisition/sources?period=all&group_by=source")).json()["rows"]}
        self.assertEqual((rows["tiktok"]["leads"], rows["instagram"]["leads"], rows["partner_x"]["leads"]), (1, 1, 0))
        self.assertEqual((await self.summary())["kpi"]["leads"], 2)

    async def test_legacy_bound_lead_via_users_attribution_click_id(self):
        await self.click("c1", None, "tiktok")
        await self.x("INSERT INTO users (id, client_id, attribution_click_id) VALUES (5, 'cli_x', 'c1')")
        self.assertEqual((await self.summary())["kpi"]["leads"], 1)


class TestBreakdowns(DB):
    async def test_source_campaign_creative_medium_grouping_and_unknown(self):
        await self.seed_basic()
        src = (await self.get("/api/acquisition/sources?period=all&group_by=source")).json()["rows"]
        self.assertEqual({r["key"]: r["clicks"] for r in src}, {"tiktok": 2, "instagram": 2, "partner_x": 1, "Unknown": 1})
        self.assertEqual(src[-1]["key"], "Unknown")                                              # Unknown last at equal count... and by clicks
        camp = (await self.get("/api/acquisition/sources?period=all&group_by=campaign")).json()["rows"]
        self.assertEqual({r["key"] for r in camp}, {"Spring", "Reels", "Unknown"})              # registry name; legacy free text has no campaign -> Unknown
        cre = (await self.get("/api/acquisition/sources?period=all&group_by=creative")).json()["rows"]
        self.assertEqual({r["key"]: r["clicks"] for r in cre}, {"cr_01": 2, "cr_02": 2, "Unknown": 2})
        med = (await self.get("/api/acquisition/sources?period=all&group_by=medium")).json()["rows"]
        self.assertEqual({r["key"]: r["clicks"] for r in med}, {"ubt": 2, "paid": 2, "Unknown": 2})

    async def test_sorted_by_clicks_desc_unknown_not_a_winner_label(self):
        await self.click("a1", None, "alpha"); await self.click("a2", None, "alpha"); await self.click("u1"); await self.click("b1", None, "beta"); await self.click("b2", None, "beta"); await self.click("b3", None, "beta")
        rows = (await self.get("/api/acquisition/sources?period=all&group_by=source")).json()["rows"]
        self.assertEqual([r["key"] for r in rows], ["beta", "alpha", "Unknown"])
        self.assertTrue(rows[0]["small_sample"])
        for r in rows:
            for bad in ("best", "worst", "winner", "loser", "rank"):
                self.assertNotIn(bad, json.dumps(r).lower())

    async def test_equal_clicks_unknown_sorts_after_named(self):
        await self.click("a1", None, "zeta"); await self.click("u1")
        self.assertEqual([r["key"] for r in (await self.get("/api/acquisition/sources?period=all&group_by=source")).json()["rows"]], ["zeta", "Unknown"])

    async def test_filters_apply_consistently_and_drill_down(self):
        await self.seed_basic(); await self.stage(1001, "offer"); await self.outcome(1001, "DEPOSIT", "pb:d")
        s = await self.summary("&source=tiktok")
        self.assertEqual((s["kpi"]["clicks"], s["kpi"]["leads"], s["kpi"]["qualified"], s["kpi"]["deposits"]), (2, 1, 1, 1))
        self.assertEqual((await self.summary("&source=instagram"))["kpi"]["deposits"], 0)
        d = (await self.get("/api/acquisition/sources?period=all&group_by=campaign&source=tiktok")).json()
        self.assertEqual([(r["key"], r["clicks"], r["leads"]) for r in d["rows"]], [("Spring", 2, 1)]); self.assertEqual(d["parent"], {"source": "tiktok"})
        d2 = (await self.get("/api/acquisition/sources?period=all&group_by=creative&source=tiktok&campaign=Spring")).json()
        self.assertEqual([(r["key"], r["clicks"]) for r in d2["rows"]], [("cr_01", 2)])
        self.assertEqual((await self.summary("&source=Unknown"))["kpi"]["clicks"], 1)
        self.assertEqual((await self.summary("&source=tiktok&creative=cr_02"))["kpi"]["clicks"], 0)
        opts = (await self.get("/api/acquisition/filters")).json()
        self.assertEqual({o["value"] for o in opts["source"]}, {"tiktok", "instagram", "partner_x", "Unknown"})


class TestReward(DB):
    async def test_zero_vs_unknown_reward_and_aggregation(self):
        await self.seed_basic()
        k = (await self.summary())["kpi"]
        self.assertIsNone(k["reward"])                                                            # no decisions at all: unknown
        await self.decision("d_pending", 1001)                                                    # pending: no reward
        self.assertIsNone((await self.summary())["kpi"]["reward"])
        await self.decision("d_zero", 1002, reward=0.0)                                           # a REAL zero
        k = (await self.summary())["kpi"]
        self.assertEqual((k["reward"], k["reward_leads"], k["reward_per_lead"]), (0.0, 1, 0.0))
        await self.decision("d_one", 1001, reward=1.0); await self.decision("d_half", 1001, reward=0.5)
        k = (await self.summary())["kpi"]
        self.assertEqual((k["reward"], k["reward_leads"]), (1.5, 2))
        self.assertEqual(k["reward_per_lead"], 0.75); self.assertEqual(k["reward_per_click"], round(1.5 / 6, 6))
        self.assertEqual(k["reward_coverage"], 1.0)
        self.assertNotIn(777, [v for v in k.values()])                                            # click revenue never used

    async def test_delayed_correction_is_applied_not_the_original(self):
        await self.seed_basic()
        await self.decision("d1", 1001, reward=1.0, corr=[{"corrected_reward": 0.4, "reward_version": "reward.v1", "event_ids": ["cb"]}])
        self.assertEqual((await self.summary())["kpi"]["reward"], 0.4)
        self.assertEqual((await self.q("SELECT reward FROM shadow_decisions WHERE decision_id='d1'"))[0]["reward"], 1.0)   # original untouched

    async def test_final_30d_vs_provisional_maturity(self):
        await self.seed_basic()
        await self.decision("fin", 1001, reward=1.0, status="final")
        await self.decision("prov_young", 1002, reward=1.0, status="provisional", otime=D(2))
        k = (await self.summary())["kpi"]
        self.assertEqual((k["final_30d"]["value"], k["final_30d"]["leads"], k["provisional"]["value"], k["provisional"]["leads"]), (1.0, 1, 1.0, 1))
        await self.x("UPDATE shadow_decisions SET outcome_timestamp = :t WHERE decision_id = 'prov_young'", t=D(40))     # window now closed
        k = (await self.summary())["kpi"]
        self.assertEqual((k["final_30d"]["leads"], k["provisional"]["leads"]), (2, 0))
        await self.decision("prov_again", 1002, reward=0.5, status="provisional", otime=D(1))                          # lead mixes: not all final
        k = (await self.summary())["kpi"]
        self.assertEqual((k["final_30d"]["leads"], k["provisional"]["leads"]), (1, 1))

    async def test_cohort_maturity_flag(self):
        await self.click("old", None, "s", at=D(60)); await self.click("new", None, "s", at=D(1))
        r = (await self.get("/api/acquisition/sources?period=all&group_by=source")).json()["rows"][0]
        self.assertEqual((r["immature_clicks"], r["mature"]), (1, False))
        only_old = (await self.summary("&source=s"))
        await self.x("UPDATE attribution_clicks SET clicked_at = :t WHERE click_id = 'new'", t=D(50))
        r = (await self.get("/api/acquisition/sources?period=all&group_by=source")).json()["rows"][0]
        self.assertEqual((r["immature_clicks"], r["mature"]), (0, True))
        self.assertEqual(only_old["maturity"]["window_days"], 30)


class TestCohortAndTime(DB):
    async def test_date_range_is_the_click_cohort(self):
        await self.click("old", None, "s", at=D(20)); await self.click("fresh", None, "s", at=D(1))
        await self.lead(1, "cli_old", "old", at=D(19)); await self.lead(2, "cli_fresh", "fresh")
        await self.outcome(1, "DEPOSIT", "pb:old", at=D(0, 1)); await self.outcome(2, "DEPOSIT", "pb:fresh", at=D(0, 1))
        k7 = (await self.get("/api/acquisition/summary?period=7d")).json()["kpi"]
        self.assertEqual((k7["clicks"], k7["leads"], k7["deposits"]), (1, 1, 1))                 # old click's lead AND deposit excluded together
        k30 = (await self.get("/api/acquisition/summary?period=30d")).json()["kpi"]
        self.assertEqual((k30["clicks"], k30["leads"], k30["deposits"]), (2, 2, 2))
        self.assertEqual((await self.get("/api/acquisition/summary?period=today")).json()["kpi"]["clicks"], 0)

    async def test_timeseries_uses_each_metrics_own_event_timestamp(self):
        await self.click("c1", None, "s", at=D(10), registered=D(9)); await self.lead(1, "cli_1", "c1", at=D(9))
        await self.click("c2", None, "s", at=D(8), registered=D(7)); await self.lead(2, "cli_2", "c2", at=D(7))
        await self.outcome(1, "DEPOSIT", "pb:1", at=D(2)); await self.outcome(2, "DEPOSIT", "pb:2", at=D(1))
        await self.decision("d1", 1, reward=1.0, otime=D(2)); await self.decision("d2", 2, reward=0.5, otime=D(1))
        day = lambda n: (NOW - timedelta(days=n)).strftime("%Y-%m-%d")
        g = lambda m: self.get(f"/api/acquisition/timeseries?metric={m}&period=30d")
        c = (await g("clicks")).json(); self.assertEqual([p["day"] for p in c["points"]], [day(10), day(8)]); self.assertTrue(c["enough"])
        l = (await g("leads")).json(); self.assertEqual([p["day"] for p in l["points"]], [day(9), day(7)])
        d = (await g("deposits")).json(); self.assertEqual([(p["day"], p["value"]) for p in d["points"]], [(day(2), 1.0), (day(1), 1.0)])
        r = (await g("reward")).json(); self.assertEqual([(p["day"], p["value"]) for p in r["points"]], [(day(2), 1.0), (day(1), 0.5)])
        w = (await self.get("/api/acquisition/timeseries?metric=deposits&period=7d")).json()
        self.assertEqual(len(w["points"]), 2)
        n = (await self.get("/api/acquisition/timeseries?metric=clicks&period=7d")).json()
        self.assertEqual((len(n["points"]), n["enough"]), (0, False))                              # clicks fall outside 7d: no chart


class TestSecurityPrivacyReadOnly(DB):
    async def test_admin_auth_required_on_every_endpoint(self):
        for path in ("summary", "sources", "timeseries", "filters"):
            self.assertEqual((await self.get("/api/acquisition/" + path, token="")).status_code, 401)
            self.assertEqual((await self.get("/api/acquisition/" + path, token="bad")).status_code, 401)
            self.assertEqual((await self.get("/api/acquisition/" + path)).status_code, 200)

    async def test_input_validation_and_no_sql_injection(self):
        await self.seed_basic()
        for qs in ("group_by=password", "group_by=src;DROP TABLE users", "period=forever", "metric=revenue"):
            path = "timeseries" if qs.startswith("metric") else "sources"
            self.assertEqual((await self.get(f"/api/acquisition/{path}?{qs}")).status_code, 400, qs)
        for bad in ("tiktok' OR '1'='1", "a;b", "x" * 80, "a@b.co", "%27%20OR%201%3D1"):
            self.assertEqual((await self.get(f"/api/acquisition/summary?source={bad}")).status_code, 400, bad)
        r = await self.get("/api/acquisition/summary?period=all&source=nothing_here")
        self.assertEqual(r.json()["kpi"]["clicks"], 0)
        self.assertEqual(len(await self.q("SELECT * FROM users")), 2)

    async def test_no_pii_or_identifiers_in_any_response(self):
        await self.seed_basic(); await self.stage(1001, "offer"); await self.outcome(1001, "DEPOSIT", "postback:1001:deposit_success:9")
        await self.decision("pd_secret1", 1001, reward=1.0)
        blob = ""
        for p in ("summary?period=all", "sources?period=all&group_by=source", "sources?period=all&group_by=campaign", "timeseries?metric=reward&period=all",
                  "filters"):
            blob += (await self.get("/api/acquisition/" + p)).text
        for needle in ("1001", "1002", "cli_a", "cli_b", "postback:", "pd_secret1", "valeria_test", "token", "client_id", "chat_id"):
            self.assertNotIn(needle, blob, needle)

    async def test_dashboard_never_mutates_data_or_creates_tables(self):
        await self.seed_basic(); await self.decision("d1", 1001, reward=1.0)
        snap = lambda: self.q("SELECT name FROM sqlite_master ORDER BY name")
        tables = await snap()
        counts = {t: len(await self.q(f"SELECT * FROM {t}")) for t in ("attribution_clicks", "users", "shadow_decisions", "acquisition_clicks", "acquisition_leads")}
        content = await self.q("SELECT * FROM shadow_decisions")
        for p in ("summary?period=all", "sources?period=all&group_by=source", "timeseries?metric=clicks&period=all", "filters"):
            self.assertEqual((await self.get("/api/acquisition/" + p)).status_code, 200)
        self.assertEqual(await snap(), tables)
        self.assertEqual({t: len(await self.q(f"SELECT * FROM {t}")) for t in counts}, counts)
        self.assertEqual([dict(r) for r in await self.q("SELECT * FROM shadow_decisions")], [dict(r) for r in content])

    async def test_missing_acquisition_tables_degrade_to_unknown_without_creating_them(self):
        await self.x("DROP TABLE acquisition_campaigns"); await self.x("DROP TABLE acquisition_leads"); await self.x("DROP TABLE acquisition_clicks")
        await self.click("c1", "free text", "tiktok", "cr")
        s = await self.summary()
        self.assertEqual(s["kpi"]["clicks"], 1)
        self.assertEqual({r["name"] for r in await self.q("SELECT name FROM sqlite_master WHERE name LIKE 'acquisition_%'")}, set())

    def test_group_by_and_filter_columns_come_only_from_an_allowlist(self):
        self.assertEqual(set(Q.DIMENSIONS), {"source", "campaign", "creative", "medium"})
        src = (ROOT / "copilot_acquisition" / "queries.py").read_text()
        self.assertNotIn("INSERT", src.upper().replace("INSERTION", "")); self.assertNotIn("UPDATE ", src.upper()); self.assertNotIn("DELETE ", src.upper())
        self.assertNotIn("commit(", src)
        tree = ast.parse((ROOT / "copilot_acquisition" / "api.py").read_text())
        self.assertEqual([n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in ("commit", "execute")], [])


class TestMissingTablePlaceholders(unittest.IsolatedAsyncioTestCase):
    """LIVE regression: with acquisition_leads absent, an untyped NULL placeholder was `text` on PostgreSQL and
    COALESCE(text, timestamptz) made every /api/acquisition/* endpoint return 503."""

    def test_time_and_number_columns_in_placeholders_are_typed(self):
        for name, sql in Q._EMPTY.items():
            for col in ("first_touch_at", "created_at", "observed_at", "outcome_timestamp", "outcome_closed_at"):
                self.assertNotIn(f"NULL AS {col}", sql, f"{name}.{col} must be CAST to a timestamp")
        self.assertIn("CAST(NULL AS DOUBLE PRECISION) AS reward", Q._EMPTY["shadow_decisions"])

    async def test_endpoints_work_when_optional_tables_are_missing(self):
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        sf = async_sessionmaker(engine, expire_on_commit=False)
        A._cache.clear()
        async with engine.begin() as c:   # only the two base tables: every optional source goes through _EMPTY
            await c.execute(text("CREATE TABLE attribution_clicks (id INTEGER PRIMARY KEY AUTOINCREMENT, click_id VARCHAR(128) NOT NULL UNIQUE,"
                                 " source VARCHAR(128), campaign VARCHAR(128), creative VARCHAR(128), clicked_at TIMESTAMP NOT NULL,"
                                 " registered_at TIMESTAMP, revenue FLOAT NOT NULL)"))
            await c.execute(text("CREATE TABLE users (id BIGINT PRIMARY KEY, client_id VARCHAR(64), attribution_click_id VARCHAR(128))"))
            await c.execute(text("INSERT INTO attribution_clicks (click_id, source, clicked_at, revenue) VALUES ('c1','tiktok',:a,0)"), {"a": D(1)})
            await c.execute(text("INSERT INTO users (id, client_id, attribution_click_id) VALUES (1,'cli_1','c1')"))
        app = FastAPI(); app.include_router(A.build_router(_admin, sf))
        with mock.patch.object(A, "CACHE_TTL", 0.0):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
                for path in ("/api/acquisition/summary?period=all", "/api/acquisition/sources?period=all&group_by=source",
                             "/api/acquisition/timeseries?period=30d&metric=leads", "/api/acquisition/filters"):
                    r = await cl.get(path, headers={"X-Admin-Token": "good"})
                    self.assertEqual(r.status_code, 200, path + " " + r.text)
                r = await cl.get("/api/acquisition/summary?period=all", headers={"X-Admin-Token": "good"})
                self.assertEqual((r.json()["kpi"]["clicks"], r.json()["kpi"]["leads"]), (1, 1))
        await engine.dispose()
