import ast
import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from copilot_outcomes import migrate, store
from copilot_outcomes.contracts import Outcome
from copilot_outcomes.repository import OutcomeConflict, OutcomeCorrupt, OutcomeRepository, OutcomeStoreError

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def out(oid="postback:1:deposit_success:e1", did=None, chat=5, at=T0, typ="DEPOSIT", value=None, conf=None,
        src="POSTBACK", meta=None):
    return Outcome(outcome_id=oid, decision_id=did, chat_id=chat, observed_at=at, outcome_type=typ, value=value,
                   confidence=conf, source=src, metadata={"event": "x", "n": [1, {"k": 2}]} if meta is None else meta)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        await migrate.ensure_schema(self.engine)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        self.repo = OutcomeRepository(self.sf)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def raw(self, sql, **p):
        async with self.sf() as s:
            await s.execute(text(sql), p)
            await s.commit()

    async def scalar(self, sql):
        async with self.sf() as s:
            return (await s.execute(text(sql))).scalar()


class MigrationTests(Base):
    async def test_schema(self):
        async with self.engine.connect() as c:
            def look(sc):
                i = inspect(sc)
                return ({x["name"]: x for x in i.get_columns("outcomes")}, i.get_pk_constraint("outcomes"),
                        {x["name"]: x for x in i.get_indexes("outcomes")}, i.get_foreign_keys("outcomes"))
            cols, pk, idx, fks = await c.run_sync(look)
        self.assertEqual(set(cols), {"outcome_id", "decision_id", "chat_id", "observed_at", "outcome_type", "value",
                                     "confidence", "source", "contract_version", "metadata"})
        self.assertEqual(pk["constrained_columns"], ["outcome_id"])
        self.assertTrue(cols["decision_id"]["nullable"])
        self.assertTrue(cols["confidence"]["nullable"])
        for n in ("chat_id", "observed_at", "outcome_type", "value", "source", "contract_version", "metadata"):
            self.assertFalse(cols[n]["nullable"], n)
        self.assertEqual(idx["ix_outcomes_decision_id"]["column_names"], ["decision_id"])
        self.assertEqual(idx["ix_outcomes_chat_observed"]["column_names"], ["chat_id", "observed_at"])
        self.assertFalse(idx["ix_outcomes_decision_id"]["unique"])
        self.assertEqual(fks, [])                                      # no FK to production_decisions
        self.assertIsNone(store.outcomes.c.observed_at.server_default)  # observed_at is never INSERT time

    async def test_dry_run_and_apply_sql(self):
        pg = postgresql.dialect()
        plan = migrate.plan(pg)
        sql = " ".join(plan)
        self.assertIn("CREATE TABLE IF NOT EXISTS outcomes", sql)
        self.assertIn("JSONB", sql)
        self.assertIn("CREATE INDEX IF NOT EXISTS ix_outcomes_decision_id ON outcomes (decision_id)", sql)
        self.assertIn("CREATE INDEX IF NOT EXISTS ix_outcomes_chat_observed ON outcomes (chat_id, observed_at)", sql)
        self.assertNotIn("UNIQUE", sql)
        self.assertNotIn("FOREIGN KEY", sql.upper())
        self.assertIn("BEFORE UPDATE ON outcomes", sql)                # immutability trigger
        self.assertIn("RAISE EXCEPTION 'outcomes is immutable'", sql)
        for stmt in plan + migrate.rollback_plan(pg):
            self.assertNotRegex(stmt.upper(), r"ALTER TABLE|PRODUCTION_DECISIONS|DECISION_EVIDENCE")
        self.assertEqual(plan, migrate.plan(pg))                       # deterministic

    async def test_idempotent_apply_and_rollback(self):
        await migrate.ensure_schema(self.engine)
        await migrate.ensure_schema(self.engine)
        await self.repo.save(out())
        self.assertEqual(migrate.rollback_plan(postgresql.dialect())[-1], "DROP TABLE IF EXISTS outcomes")
        self.assertTrue(all("decision_evidence" not in s and "production_decisions" not in s
                            for s in migrate.rollback_plan(postgresql.dialect())))
        await migrate.rollback_schema(self.engine)
        self.assertEqual(await self.scalar("SELECT count(*) FROM sqlite_master WHERE name='outcomes'"), 0)

    async def test_only_own_table_and_no_table_without_migration(self):
        self.assertEqual(set(store.metadata.tables), {"outcomes"})
        eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        repo = OutcomeRepository(async_sessionmaker(eng, expire_on_commit=False))
        with self.assertRaises(Exception):
            await repo.exists("x")                                     # repository never creates the table
        await eng.dispose()


class RepositoryTests(Base):
    async def test_save_get_exists(self):
        o = out(did="pd_1", value={"amount": 25.5}, conf=0.3)
        self.assertFalse(await self.repo.exists(o.outcome_id))
        self.assertIsNone(await self.repo.get(o.outcome_id))
        self.assertTrue(await self.repo.save(o))
        self.assertTrue(await self.repo.exists(o.outcome_id))
        got = await self.repo.get(o.outcome_id)
        self.assertEqual(got, o)
        self.assertIsInstance(got, Outcome)
        self.assertEqual(got.to_dict(), o.to_dict())
        self.assertEqual((got.observed_at, got.confidence, got.value["amount"]), (T0, 0.3, 25.5))
        self.assertFalse(await self.repo.exists("other"))

    async def test_round_trip_variants(self):
        cases = [out("a"), out("b", value=None, conf=None), out("c", value=[1, "x", {"y": None}], meta={}),
                 out("d", typ="CONTINUED", src="TELEGRAM_EVENT"), out("e", typ="DROPPED", src="PRODUCTION", conf=0),
                 out("f", at=datetime(2026, 10, 2, 14, 0, tzinfo=timezone(timedelta(hours=2)))),
                 out("g", at=T0 + timedelta(microseconds=123456))]
        for o in cases:
            await self.repo.save(o)
        for o in cases:
            got = await self.repo.get(o.outcome_id)
            self.assertEqual(got, o, o.outcome_id)
            self.assertEqual(got.observed_at, o.observed_at)           # same instant, never INSERT time
        self.assertIsNone((await self.repo.get("b")).value)            # JSON null survives NOT NULL column

    async def test_observed_at_is_event_time_not_insert_time(self):
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        await self.repo.save(out(at=past))
        self.assertEqual((await self.repo.get("postback:1:deposit_success:e1")).observed_at, past)

    async def test_idempotent_duplicate(self):
        self.assertTrue(await self.repo.save(out()))
        self.assertFalse(await self.repo.save(out()))
        self.assertFalse(await self.repo.save(out()))
        self.assertEqual(await self.scalar("SELECT count(*) FROM outcomes"), 1)

    async def test_conflicting_duplicate(self):
        await self.repo.save(out(conf=0.5))
        for other in (out(conf=0.6), out(typ="REGISTRATION"), out(did="pd_1"), out(chat=6),
                      out(at=T0 + timedelta(seconds=1)), out(value={"a": 1}), out(meta={"z": 1}), out(src="SYSTEM")):
            with self.assertRaises(OutcomeConflict):
                await self.repo.save(other)
        self.assertEqual(await self.scalar("SELECT count(*) FROM outcomes"), 1)
        self.assertEqual(await self.repo.get("postback:1:deposit_success:e1"), out(conf=0.5))

    async def test_nullable_decision_id_no_decision_lookup(self):
        await self.repo.save(out("n1", did=None))
        self.assertIsNone((await self.repo.get("n1")).decision_id)
        await self.repo.save(out("n2", did="pd_never_recorded"))       # no production_decisions table even exists here
        self.assertEqual((await self.repo.get("n2")).decision_id, "pd_never_recorded")

    async def test_multiple_outcomes_same_decision(self):
        for i, t in enumerate(("PAYMENT_ATTEMPT", "DEPOSIT", "CONTINUED")):
            await self.repo.save(out(f"o{i}", did="pd_1", typ=t, at=T0 + timedelta(minutes=i + 1)))
        self.assertEqual(await self.scalar("SELECT count(*) FROM outcomes WHERE decision_id='pd_1'"), 3)

    async def test_same_chat_different_timestamps(self):
        for i in range(3):
            await self.repo.save(out(f"t{i}", chat=9, at=T0 + timedelta(hours=i)))
        self.assertEqual(await self.scalar("SELECT count(DISTINCT observed_at) FROM outcomes WHERE chat_id=9"), 3)
        got = [await self.repo.get(f"t{i}") for i in range(3)]
        self.assertEqual([g.observed_at for g in got], [T0 + timedelta(hours=i) for i in range(3)])

    async def test_concurrent_duplicates(self):
        res = await asyncio.gather(*[self.repo.save(out()) for _ in range(8)])
        self.assertEqual(sorted(res), [False] * 7 + [True])
        res = await asyncio.gather(self.repo.save(out("c1")), self.repo.save(out("c1", conf=0.9)),
                                   return_exceptions=True)
        self.assertEqual(sum(isinstance(r, OutcomeConflict) for r in res), 1)
        self.assertEqual(await self.scalar("SELECT count(*) FROM outcomes"), 2)

    async def test_transaction_rollback(self):
        with mock.patch.object(AsyncSession, "commit", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                await self.repo.save(out())
        self.assertFalse(await self.repo.exists("postback:1:deposit_success:e1"))
        self.assertTrue(await self.repo.save(out()))

    async def test_invalid_save_input(self):
        for bad in (None, out().to_dict(), "x"):
            with self.assertRaises(TypeError):
                await self.repo.save(bad)
        self.assertEqual(await self.scalar("SELECT count(*) FROM outcomes"), 0)

    async def test_corrupted_rows_rejected(self):
        await self.repo.save(out(did="pd_1", conf=0.5))
        oid = "postback:1:deposit_success:e1"
        for setter in ("contract_version='outcome.v9'", "outcome_type='LLM_GUESS'", "source='LLM'",
                       "decision_id='bad'", "confidence=5.0", "confidence=-1", "metadata='\"str\"'",
                       "metadata='[1]'", "metadata='not json'", "value='{broken'", "chat_id=0, outcome_id='ZZ'"):
            await self.raw("DELETE FROM outcomes")
            await self.repo.save(out(did="pd_1", conf=0.5))
            try:
                await self.raw(f"UPDATE outcomes SET {setter}")
            except Exception:
                continue                                               # DB itself refused to store garbage: fine
            target = "ZZ" if "outcome_id='ZZ'" in setter else oid
            if setter.startswith("chat_id=0"):
                self.assertEqual((await self.repo.get(target)).chat_id, 0)   # schema-valid edit: undetectable, see docs
                continue
            with self.assertRaises(OutcomeCorrupt, msg=setter):
                await self.repo.get(target)

    async def test_corrupt_row_never_mistaken_for_identical(self):
        await self.repo.save(out())
        await self.raw("UPDATE outcomes SET contract_version='outcome.v9'")
        with self.assertRaises(OutcomeCorrupt):
            await self.repo.save(out())

    async def test_no_mutating_api_and_error_hierarchy(self):
        public = {n for n in dir(OutcomeRepository) if not n.startswith("_")}
        self.assertEqual(public, {"save", "get", "exists"})
        self.assertTrue(issubclass(OutcomeConflict, OutcomeStoreError) and issubclass(OutcomeCorrupt, OutcomeStoreError))

    async def test_deterministic(self):
        outs = [out(f"d{i}", chat=i % 2, at=T0 + timedelta(minutes=i)) for i in range(6)]
        for o in reversed(outs):
            await self.repo.save(o)
        a = [(await self.repo.get(o.outcome_id)).to_dict() for o in outs]
        eng2 = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        await migrate.ensure_schema(eng2)
        repo2 = OutcomeRepository(async_sessionmaker(eng2, expire_on_commit=False))
        for o in outs:
            await repo2.save(o)
        b = [(await repo2.get(o.outcome_id)).to_dict() for o in outs]
        await eng2.dispose()
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))


class SafetyTests(unittest.TestCase):
    FILES = [ROOT / "copilot_outcomes" / n for n in ("store.py", "repository.py", "migrate.py")]
    BANNED = ("main", "telethon", "fastapi", "starlette", "aiohttp", "httpx", "requests", "socket", "urllib", "pickle",
              "subprocess", "openai", "anthropic", "copilot_decisions", "copilot_shadow", "copilot_external", "scout",
              "client_state", "copilot_evidence.repository", "copilot_evidence.store")

    def test_imports_no_join_no_adapters_no_clock(self):
        for f in self.FILES:
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                mods = ([a.name for a in n.names] if isinstance(n, ast.Import)
                        else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
                for m in mods:
                    self.assertNotIn(m.split(".")[0], self.BANNED, f"{f.name}: {m}")
                    self.assertNotIn(m, {"copilot_outcomes.joiner", "copilot_outcomes.adapters"}, f"{f.name}: {m}")
                    if f.name != "migrate.py":
                        self.assertNotIn("migrate", m, f.name)
                if isinstance(n, ast.Call):
                    fn = n.func
                    name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                    self.assertNotIn(name, {"update", "delete", "merge", "on_conflict_do_update", "now", "utcnow",
                                            "open", "eval", "exec"}, f"{f.name}:{name}")
                    if f.name != "migrate.py":
                        self.assertNotIn(name, {"join", "join_all"}, f"{f.name}:{name}")
            src = f.read_text(encoding="utf-8").lower()
            self.assertNotIn("func.now", src)
            self.assertNotIn("server_default", src.replace("# observed_at", "").replace("no server default", ""))
            if f.name != "migrate.py":
                self.assertNotIn("create_all", src)

    def test_nothing_in_production_references_outcome_persistence(self):
        prod = [ROOT / n for n in ("client_state.py", "scout.py", "run.py")]   # main.py: exactly one capture hook, see test_capture
        for d in ("copilot_decisions", "copilot_shadow", "copilot_external", "copilot_evidence"):
            prod += list((ROOT / d).rglob("*.py"))
        for f in prod:
            if f.exists():
                self.assertNotIn("copilot_outcomes", f.read_text(encoding="utf-8"), f.name)
        for pkg in ("joiner.py", "contracts.py", "__init__.py"):
            t = (ROOT / "copilot_outcomes" / pkg).read_text(encoding="utf-8")
            self.assertNotRegex(t, r"copilot_outcomes\.(repository|store|migrate)|sqlalchemy")
        for f in (ROOT / "copilot_outcomes" / "adapters").glob("*.py"):
            self.assertNotRegex(f.read_text(encoding="utf-8"), r"copilot_outcomes\.(repository|store|migrate)|sqlalchemy")

    def test_contract_layer_stays_db_free(self):
        import subprocess
        import sys
        code = "import sys, copilot_outcomes, copilot_outcomes.adapters; sys.exit(1 if 'sqlalchemy' in sys.modules else 0)"
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
