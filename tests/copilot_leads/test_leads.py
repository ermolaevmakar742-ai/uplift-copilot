import ast
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
from sqlalchemy import insert, text, update, delete, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from copilot_contracts.stages import Stage
from copilot_decisions import migrate as dec_migrate
from copilot_decisions.store import production_decisions
from copilot_leads import api as leads_api, migrate, repository as R, store
from copilot_leads.contract import AXIS, TARGETS, LeadError, mask_chat_id, parse_chat_id, parse_stage
from copilot_leads.service import LeadService
from copilot_outcomes import migrate as out_migrate
from copilot_response_boundary import migrate as rb_migrate

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        for m in (dec_migrate, out_migrate, rb_migrate):
            await m.ensure_schema(self.engine)
        async with self.engine.begin() as c:
            await c.run_sync(R._ro.create_all)
        await migrate.ensure_schema(self.engine)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        self.now = T0 + timedelta(hours=5)
        self.svc = LeadService(self.sf, clock=lambda: self.now)
        self.queries = []
        event.listen(self.engine.sync_engine, "before_cursor_execute", lambda *a: self.queries.append(a[2]))

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def add_user(self, cid, stage="new", minutes=0):
        async with self.sf() as s:
            await s.execute(insert(R.users_t).values(id=cid, stage=stage, blocked=False, manual_mode=False, last_message_at=T0 + timedelta(minutes=minutes)))
            await s.commit()

    async def add_decision(self, cid, i, snap):
        async with self.sf() as s:
            await s.execute(insert(production_decisions).values(
                decision_id=f"pd_{cid}_{i}", chat_id=cid, message_id=i, created_at=T0 + timedelta(minutes=i), decision_path="llm_reply",
                client_state_snapshot=snap, **{c.name: None for c in production_decisions.columns if c.nullable and c.name not in
                                               ("decision_id", "chat_id", "message_id", "created_at", "decision_path", "client_state_snapshot", "id")}
                | {c.name: ("x" if str(c.type).startswith("VARCHAR") else {} if "JSON" in str(c.type) else 0) for c in production_decisions.columns
                   if not c.nullable and not c.primary_key and c.name not in ("decision_id", "chat_id", "message_id", "created_at", "decision_path")}))
            await s.commit()

    async def count(self, table="lead_stage_overrides"):
        async with self.sf() as s:
            return (await s.execute(text(f"select count(*) from {table}"))).scalar()


class ContractTests(unittest.TestCase):
    def test_axis_is_the_canonical_stage_enum(self):
        self.assertEqual([s.value for s in AXIS], ["START", "INTEREST", "NEED", "TRUST", "OBJECTION", "PAYMENT", "DEPOSIT", "SUCCESS"])
        self.assertTrue(all(isinstance(s, Stage) for s in AXIS + TARGETS))
        self.assertEqual({s.value for s in TARGETS} | {"UNKNOWN"}, {s.value for s in Stage})        # exactly the canonical members, no second dictionary
        src = "".join((ROOT / "copilot_leads" / f).read_text() for f in ("contract.py", "service.py", "stage_override.py", "api.py", "repository.py"))
        self.assertNotIn('"INTEREST"', src)

    def test_parsing(self):
        self.assertEqual(parse_stage("TRUST", allow_unknown=False), Stage.TRUST)
        for bad in ("trust", " TRUST", "FOO", "", None, 5, "UNKNOWN", ["TRUST"]):
            with self.assertRaises(LeadError, msg=repr(bad)):
                parse_stage(bad, allow_unknown=False)
        self.assertEqual(parse_stage("UNKNOWN", allow_unknown=True), Stage.UNKNOWN)
        self.assertEqual(parse_chat_id("777"), 777)
        for bad in ("abc", "0", 0, True, None, 2 ** 70, "1; drop table users", 1.5, ""):
            with self.assertRaises(LeadError, msg=repr(bad)):
                parse_chat_id(bad)
        self.assertEqual(mask_chat_id(7771030657), "…0657")


class RetrievalTests(Base):
    async def test_cards_use_only_real_data(self):
        await self.add_user(101, "new", 3)
        await self.add_user(102, "visited", 2)                    # ambiguous production stage -> UNKNOWN, never guessed
        await self.add_user(103, "deposited", 1)
        await self.add_decision(101, 1, {"stage": "new", "intent": "question", "sentiment": 0.7, "messages_count": 4})
        d = await self.svc.journey()
        by = {c["chat_id"]: c for c in d["leads"]}
        self.assertEqual((by["101"]["stage"], by["102"]["stage"], by["103"]["stage"]), ("START", "UNKNOWN", "DEPOSIT"))
        self.assertEqual((by["101"]["intent"], by["101"]["sentiment"], by["101"]["trust"], by["101"]["blocker"]), ("question", 0.7, None, None))
        self.assertEqual((by["102"]["intent"], by["102"]["sentiment"]), (None, None))
        self.assertEqual(by["101"]["label"], "…101")
        self.assertEqual(d["axis"], [s.value for s in AXIS])
        self.assertEqual(d["separate"], ["DROPPED", "UNKNOWN"])
        for c in d["leads"]:
            self.assertFalse({"probability", "conversion", "uplift", "score"} & set(c))
        self.assertEqual([c["chat_id"] for c in d["leads"]], ["101", "102", "103"])      # newest activity first

    async def test_pagination_is_bounded_and_query_count_is_constant(self):
        for i in range(1, 31):
            await self.add_user(1000 + i, "new", i)
            await self.add_decision(1000 + i, 1, {"intent": "x", "sentiment": 0.5})
        self.queries.clear()
        p1 = await self.svc.journey(limit=10, offset=0)
        n10 = len(self.queries)
        self.queries.clear()
        p3 = await self.svc.journey(limit=30, offset=0)
        self.assertEqual(len(self.queries), n10)                                          # no N+1
        self.assertEqual((p1["total"], len(p1["leads"]), len(p3["leads"])), (30, 10, 30))
        p2 = await self.svc.journey(limit=10, offset=10)
        self.assertTrue(set(c["chat_id"] for c in p1["leads"]).isdisjoint(c["chat_id"] for c in p2["leads"]))
        self.assertEqual(len((await self.svc.journey(limit=10_000))["leads"]), 30)
        self.assertEqual(R.MAX_PAGE, 200)
        self.assertEqual((await self.svc.journey(limit=0, offset=-5))["limit"], 1)
        self.assertEqual((await self.svc.journey(offset=500))["leads"], [])

    async def test_detail_series_honest_about_missing_history(self):
        await self.add_user(201)
        await self.add_decision(201, 1, {"sentiment": 0.4, "messages_count": 1})
        self.assertEqual((await self.svc.detail(201))["series"]["status"], "INSUFFICIENT_DATA")      # one point is not a trend
        self.assertEqual((await self.svc.detail(201))["series"]["points"], [])
        await self.add_decision(201, 2, {"sentiment": 0.6, "messages_count": 3})
        s = (await self.svc.detail(201))["series"]
        self.assertEqual((s["status"], [p["sentiment"] for p in s["points"]]), ("OK", [0.4, 0.6]))       # oldest -> newest, real values
        await self.add_user(202)
        self.assertEqual((await self.svc.detail(202))["series"]["status"], "INSUFFICIENT_DATA")
        with self.assertRaises(LeadError) as e:
            await self.svc.detail(99999)
        self.assertEqual(e.exception.code, "lead_not_found")

    async def test_detail_is_bounded(self):
        await self.add_user(301)
        for i in range(1, 60):
            await self.add_decision(301, i, {"sentiment": 0.5})
        self.assertLessEqual(len((await self.svc.detail(301))["series"]["points"]), R.MAX_SERIES)


class OverrideTests(Base):
    async def move(self, cid, a, b):
        self.now += timedelta(seconds=1)                                                  # real clocks never repeat a microsecond between two admin clicks
        return await self.svc.move(cid, a, b, "admin")

    async def test_valid_move_is_appended_not_applied_to_client_state(self):
        await self.add_user(401, "new")
        o = await self.move(401, "START", "TRUST")
        self.assertEqual((o["previous_stage"], o["new_stage"], o["actor"], o["source"], o["previous_override_id"]), ("START", "TRUST", "admin", "human", None))
        async with self.sf() as s:
            self.assertEqual((await s.execute(text("select stage from users where id=401"))).scalar(), "new")   # Client State untouched
        card = next(c for c in (await self.svc.journey())["leads"] if c["chat_id"] == "401")
        self.assertEqual((card["stage"], card["stage_origin"], card["production_stage_raw"]), ("TRUST", "human", "new"))

    async def test_repeated_moves_create_new_events_and_form_a_chain(self):
        await self.add_user(402, "new")
        a = await self.move(402, "START", "TRUST")
        b = await self.move(402, "TRUST", "START")
        c = await self.move(402, "START", "TRUST")                                        # identical content to `a`, still a new event
        self.assertEqual(len({a["override_id"], b["override_id"], c["override_id"]}), 3)
        self.assertEqual((b["previous_override_id"], c["previous_override_id"]), (a["override_id"], b["override_id"]))
        self.assertEqual(await self.count(), 3)
        d = await self.svc.detail(402)
        self.assertEqual([o["new_stage"] for o in d["overrides"]], ["TRUST", "START", "TRUST"])      # newest first, full history kept

    async def test_validation(self):
        await self.add_user(403, "new")
        cases = [("START", "NOPE", "invalid_stage"), ("START", "UNKNOWN", "invalid_stage"), ("START", "trust", "invalid_stage"), ("START", None, "invalid_stage"),
                 ("BOGUS", "TRUST", "invalid_stage"), ("START", "START", "same_stage"), ("TRUST", "NEED", "stale_stage")]
        for a, b, code in cases:
            with self.assertRaises(LeadError, msg=(a, b)) as e:
                await self.move(403, a, b)
            self.assertEqual(e.exception.code, code, (a, b))
        with self.assertRaises(LeadError) as e:
            await self.move(999, "START", "TRUST")
        self.assertEqual(e.exception.code, "lead_not_found")
        with self.assertRaises(LeadError) as e:
            await self.svc.move("1 or 1=1", "START", "TRUST", "admin")
        self.assertEqual(e.exception.code, "invalid_chat_id")
        with self.assertRaises(LeadError):
            await self.svc.move(403, "START", "TRUST", "")
        self.assertEqual(await self.count(), 0)                                           # nothing persisted by any rejected move

    async def test_unknown_lead_can_be_placed_and_same_stage_uses_effective_stage(self):
        await self.add_user(404, "visited")                                               # production UNKNOWN
        await self.move(404, "UNKNOWN", "INTEREST")
        with self.assertRaises(LeadError) as e:
            await self.move(404, "INTEREST", "INTEREST")
        self.assertEqual(e.exception.code, "same_stage")
        await self.move(404, "INTEREST", "DROPPED")                                       # DROPPED is a valid human observation

    async def test_storage_not_migrated(self):
        await self.add_user(405)
        async with self.engine.begin() as c:
            await c.execute(text("drop table lead_stage_overrides"))
        with self.assertRaises(LeadError) as e:
            await self.move(405, "START", "TRUST")
        self.assertEqual(e.exception.code, "override_storage_unavailable")
        d = await self.svc.journey()                                                       # the board still reads
        self.assertEqual((d["override_storage"], len(d["leads"])), ("not_migrated", 1))

    async def test_override_does_not_touch_any_other_table(self):
        await self.add_user(406, "new")
        await self.add_decision(406, 1, {"sentiment": 0.5})
        tables = ("production_decisions", "outcomes", "response_boundaries", "users")
        before = {t: await self.count(t) for t in tables}
        async with self.sf() as s:
            snap_before = (await s.execute(text("select stage, last_message_at from users where id=406"))).all()
        await self.move(406, "START", "PAYMENT")
        self.assertEqual({t: await self.count(t) for t in tables}, before)
        async with self.sf() as s:
            self.assertEqual((await s.execute(text("select stage, last_message_at from users where id=406"))).all(), snap_before)


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_apply_rollback(self):
        eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        stmts = migrate.plan(eng.dialect)
        self.assertTrue(stmts[0].startswith("CREATE TABLE IF NOT EXISTS lead_stage_overrides"))
        self.assertTrue(any("ix_lead_override_chat_time" in s for s in stmts))
        from sqlalchemy.dialects import postgresql
        pg = migrate.plan(postgresql.dialect())
        self.assertTrue(any("BEFORE UPDATE OR DELETE" in s for s in pg))
        self.assertEqual(migrate.rollback_plan(postgresql.dialect())[-1], "DROP TABLE IF EXISTS lead_stage_overrides")
        async with eng.connect() as c:
            self.assertEqual((await c.execute(text("select count(*) from sqlite_master where name='lead_stage_overrides'"))).scalar(), 0)   # plan() wrote nothing
        await migrate.ensure_schema(eng)
        await migrate.ensure_schema(eng)                                                  # idempotent
        async with eng.begin() as c:
            self.assertEqual((await c.execute(text("select count(*) from sqlite_master where name='lead_stage_overrides'"))).scalar(), 1)
            await c.execute(text("insert into lead_stage_overrides values ('so_1',1,'START','TRUST','admin','human','2026-01-01 00:00:00',null,'lead-journey.v1')"))
        for bad in ("insert into lead_stage_overrides values ('so_2',1,'START','START','admin','human','2026-01-01 00:00:00',null,'v')",
                    "insert into lead_stage_overrides values ('so_3',1,'START','TRUST','admin','bot','2026-01-01 00:00:00',null,'v')"):
            with self.assertRaises(Exception):
                async with eng.begin() as c:
                    await c.execute(text(bad))
        await migrate.rollback_schema(eng)
        async with eng.connect() as c:
            self.assertEqual((await c.execute(text("select count(*) from sqlite_master where name='lead_stage_overrides'"))).scalar(), 0)
        await eng.dispose()

    def test_postgres_trigger_blocks_update_and_delete_by_design(self):
        ddl = " ".join(store.APPEND_ONLY_DDL)
        self.assertIn("BEFORE UPDATE OR DELETE", ddl)
        self.assertIn("RAISE EXCEPTION", ddl)

    def test_no_repository_code_updates_or_deletes_overrides(self):
        src = (ROOT / "copilot_leads" / "repository.py").read_text()
        for w in ("update(", "delete(", "UPDATE ", "DELETE "):
            self.assertNotIn(w, src)

    def test_migration_never_runs_implicitly(self):
        for f in ("repository.py", "service.py", "api.py", "stage_override.py", "contract.py"):
            self.assertNotIn("ensure_schema", (ROOT / "copilot_leads" / f).read_text())


class ApiTests(Base):
    def app(self):
        app = FastAPI()
        async def require_admin(x_admin_token: str | None = Header(default=None)):
            if x_admin_token != "ok":
                raise HTTPException(401, "Missing token")
        self.audits = []
        async def audit_log(action, ip=None, details=None):
            self.audits.append((action, details))
        app.include_router(leads_api.build_router(require_admin, self.sf, audit_log, clock=lambda: self.now))
        return app

    async def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()), base_url="http://t")

    async def test_admin_guard_on_every_route(self):
        await self.add_user(501)
        async with await self.client() as c:
            for method, url, body in (("GET", "/api/leads/journey", None), ("GET", "/api/leads/501", None), ("POST", "/api/leads/501/stage", {"from_stage": "START", "to_stage": "TRUST"})):
                r = await c.request(method, url, json=body)
                self.assertEqual(r.status_code, 401, url)
                r = await c.request(method, url, json=body, headers={"X-Admin-Token": "bad"})
                self.assertEqual(r.status_code, 401, url)
        self.assertEqual(await self.count(), 0)

    async def test_end_to_end(self):
        await self.add_user(502, "new", 5)
        H = {"X-Admin-Token": "ok"}
        async with await self.client() as c:
            j = (await c.get("/api/leads/journey?limit=10", headers=H)).json()
            self.assertEqual((j["ok"], j["total"], j["leads"][0]["stage"]), (True, 1, "START"))
            r = await c.post("/api/leads/502/stage", headers=H, json={"from_stage": "START", "to_stage": "OBJECTION"})
            self.assertEqual((r.status_code, r.json()["override"]["new_stage"], r.json()["override"]["actor"]), (200, "OBJECTION", "admin"))
            self.assertEqual(self.audits[0][0], "lead_stage_override")
            self.assertEqual((await c.get("/api/leads/502", headers=H)).json()["lead"]["stage"], "OBJECTION")
            codes = {}
            for name, body in (("same", {"from_stage": "OBJECTION", "to_stage": "OBJECTION"}), ("stale", {"from_stage": "START", "to_stage": "NEED"}),
                               ("bad", {"from_stage": "OBJECTION", "to_stage": "XYZ"}), ("extra", {"from_stage": "OBJECTION", "to_stage": "NEED", "sql": "x"}),
                               ("missing", {"to_stage": "NEED"}), ("list", ["a"])):
                codes[name] = (await c.post("/api/leads/502/stage", headers=H, json=body)).status_code
            self.assertEqual(codes, {"same": 400, "stale": 409, "bad": 400, "extra": 400, "missing": 400, "list": 400})
            self.assertEqual((await c.post("/api/leads/9999/stage", headers=H, json={"from_stage": "START", "to_stage": "NEED"})).status_code, 404)
            self.assertEqual((await c.post("/api/leads/abc/stage", headers=H, json={"from_stage": "START", "to_stage": "NEED"})).status_code, 400)
            self.assertEqual((await c.post("/api/leads/502/stage", headers=H, content=b"{nope")).status_code, 400)
            self.assertEqual((await c.get("/api/leads/journey?limit=5000", headers=H)).status_code, 422)
            self.assertEqual((await c.get("/api/leads/9999", headers=H)).status_code, 404)
        self.assertEqual(await self.count(), 1)

    async def test_db_failure_is_a_503_for_the_panel_only(self):
        H = {"X-Admin-Token": "ok"}
        async with await self.client() as c:
            async with self.engine.begin() as conn:
                await conn.execute(text("drop table users"))
            r = await c.get("/api/leads/journey", headers=H)
            self.assertEqual((r.status_code, r.json()["detail"]["error"]), (503, "leads_unavailable"))

    async def test_audit_log_failure_does_not_fail_the_move(self):
        await self.add_user(503, "new")
        app = FastAPI()
        async def ok(): return None
        async def boom(*a, **k): raise RuntimeError("audit down")
        app.include_router(leads_api.build_router(ok, self.sf, boom, clock=lambda: self.now))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/leads/503/stage", json={"from_stage": "START", "to_stage": "NEED"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(await self.count(), 1)


if __name__ == "__main__":
    unittest.main()
