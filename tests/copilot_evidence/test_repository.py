import ast
import asyncio
import dataclasses
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sqlalchemy import inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from copilot_contracts.stages import Stage
from copilot_evidence import migrate, store
from copilot_evidence.contracts import Evidence, EvidenceSource as S, EvidenceType as T, Provenance
from copilot_evidence.decision import DecisionEvidence, build_decision_evidence
from copilot_evidence.fusion import fuse
from copilot_evidence.repository import (DecisionEvidenceConflict, DecisionEvidenceCorrupt,
                                      DecisionEvidenceRepository, DecisionEvidenceStoreError)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def ev(eid, src, typ, value, stage=Stage.UNKNOWN, did="pd_one", sid="same"):
    return Evidence(evidence_id=eid, chat_id=5, message_id=9, created_at=NOW, source=src, evidence_type=typ,
                    value=value, confidence=0.5, reliability=None, observed_at=NOW, stage=stage,
                    provenance=Provenance(src, did if sid == "same" else sid), metadata={"m": {"n": [1]}})


def snap(did="pd_one", message_id=9, created_at=NOW, extra=False):
    items = [ev("cs", S.CLIENT_STATE, T.STAGE, {"production_stage": "deposit_attempt"}, Stage.PAYMENT, sid=None),
             ev("sh", S.SHADOW, T.STAGE, {"n": 1}, Stage.TRUST, did),
             ev("act", S.EXTERNAL, T.ACTION, {"regime": "sales"}, did=did)]
    if extra:
        items.append(ev("extra", S.SHADOW, T.EXTERNAL_SIGNAL, {"c": 1}, sid="partner_feed"))
    return build_decision_evidence(did, fuse(items), chat_id=5, message_id=message_id, created_at=created_at)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        await migrate.ensure_schema(self.engine)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        self.repo = DecisionEvidenceRepository(self.sf)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def raw(self, sql, **p):
        async with self.sf() as s:
            await s.execute(text(sql), p)
            await s.commit()

    async def count(self):
        async with self.sf() as s:
            return (await s.execute(text("SELECT count(*) FROM decision_evidence"))).scalar()


class MigrationTests(Base):
    async def test_schema(self):
        async with self.engine.connect() as c:
            def look(sc):
                i = inspect(sc)
                cols = {x["name"]: x for x in i.get_columns("decision_evidence")}
                return cols, i.get_pk_constraint("decision_evidence"), i.get_unique_constraints("decision_evidence")
            cols, pk, uq = await c.run_sync(look)
        self.assertEqual(set(cols), {"decision_evidence_id", "decision_id", "chat_id", "message_id",
                                     "created_at", "contract_version", "snapshot"})
        self.assertEqual(pk["constrained_columns"], ["decision_evidence_id"])
        self.assertEqual([u["column_names"] for u in uq], [["decision_id"]])
        self.assertTrue(cols["message_id"]["nullable"])
        for n in ("decision_id", "chat_id", "created_at", "contract_version", "snapshot"):
            self.assertFalse(cols[n]["nullable"], n)

    async def test_idempotent_and_rollback(self):
        await migrate.ensure_schema(self.engine)
        await migrate.ensure_schema(self.engine)
        await self.repo.save(snap())
        await migrate.rollback_schema(self.engine)
        async with self.sf() as s:
            n = (await s.execute(text("SELECT count(*) FROM sqlite_master WHERE name='decision_evidence'"))).scalar()
        self.assertEqual(n, 0)

    async def test_only_own_table_and_pg_plan(self):
        self.assertEqual(set(store.metadata.tables), {"decision_evidence"})
        plan = " ".join(migrate.plan(postgresql.dialect()))
        self.assertIn("JSONB", plan)
        self.assertIn("uq_de_decision_id", plan)
        self.assertIn("BEFORE UPDATE", plan)
        self.assertNotIn("production_decisions", plan)
        for stmt in migrate.plan(postgresql.dialect()) + migrate.rollback_plan(postgresql.dialect()):
            self.assertNotRegex(stmt.upper(), r"ALTER TABLE|DROP TABLE (?!IF EXISTS DECISION_EVIDENCE)")

    async def test_no_table_without_migration(self):
        eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        repo = DecisionEvidenceRepository(async_sessionmaker(eng, expire_on_commit=False))
        with self.assertRaises(Exception):
            await repo.exists("pd_one")                       # repository never creates the table
        await eng.dispose()


class RepositoryTests(Base):
    async def test_save_get_exists(self):
        s = snap()
        self.assertFalse(await self.repo.exists("pd_one"))
        self.assertIsNone(await self.repo.get_by_decision_id("pd_one"))
        self.assertTrue(await self.repo.save(s))
        self.assertTrue(await self.repo.exists("pd_one"))
        got = await self.repo.get_by_decision_id("pd_one")
        self.assertEqual(got, s)
        self.assertIsInstance(got, DecisionEvidence)
        self.assertEqual(got.to_dict(), s.to_dict())
        self.assertEqual(len(got.conflicts), 1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            got.decision_id = "pd_x"
        self.assertFalse(await self.repo.exists("pd_other"))

    async def test_round_trip_through_db_column_is_contract_dict(self):
        s = snap()
        await self.repo.save(s)
        async with self.sf() as sess:
            stored = (await sess.execute(select(store.decision_evidence.c.snapshot))).scalar()
        self.assertEqual(stored, json.loads(json.dumps(s.to_dict())))

    async def test_nullable_message_id(self):
        s = snap(message_id=None)
        await self.repo.save(s)
        got = await self.repo.get_by_decision_id("pd_one")
        self.assertIsNone(got.message_id)
        self.assertEqual(got, s)

    async def test_idempotent_identical_save(self):
        s = snap()
        self.assertTrue(await self.repo.save(s))
        self.assertFalse(await self.repo.save(snap()))       # equal, freshly built
        self.assertEqual(await self.count(), 1)

    async def test_conflicting_duplicate_rejected(self):
        await self.repo.save(snap())
        for other in (snap(extra=True), snap(created_at=NOW + timedelta(hours=1))):
            with self.assertRaises(DecisionEvidenceConflict):
                await self.repo.save(other)
        self.assertEqual(await self.count(), 1)
        self.assertEqual(await self.repo.get_by_decision_id("pd_one"), snap())   # nothing overwritten

    async def test_unique_decision_id_enforced_by_db(self):
        await self.repo.save(snap())
        with self.assertRaises(Exception):
            await self.raw("INSERT INTO decision_evidence VALUES ('de_other','pd_one',5,9,:t,'decision_evidence.v1','{}')",
                           t=NOW)
        await self.repo.save(snap(did="pd_two"))
        self.assertEqual(await self.count(), 2)

    async def test_concurrent_duplicates(self):
        res = await asyncio.gather(*[self.repo.save(snap()) for _ in range(8)])
        self.assertEqual(sorted(res), [False] * 7 + [True])
        self.assertEqual(await self.count(), 1)
        res = await asyncio.gather(self.repo.save(snap(did="pd_c")), self.repo.save(snap(did="pd_c", extra=True)),
                                   return_exceptions=True)
        self.assertEqual(sum(isinstance(r, DecisionEvidenceConflict) for r in res), 1)
        self.assertEqual(sum(r is True for r in res), 1)
        self.assertEqual(await self.count(), 2)

    async def test_transaction_rollback(self):
        with mock.patch.object(AsyncSession, "commit", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                await self.repo.save(snap())
        self.assertFalse(await self.repo.exists("pd_one"))
        self.assertEqual(await self.count(), 0)
        self.assertTrue(await self.repo.save(snap()))        # clean retry works

    async def test_invalid_save_input(self):
        for bad in (None, {"decision_id": "pd_one"}, snap().to_dict()):
            with self.assertRaises(TypeError):
                await self.repo.save(bad)
        self.assertEqual(await self.count(), 0)

    async def test_corrupted_snapshot_rejected(self):
        await self.repo.save(snap())
        for bad in ('{"nope": 1}', '"just a string"', 'null', '[]', '{"contract_version":"decision_evidence.v1"}'):
            await self.raw("UPDATE decision_evidence SET snapshot=:s", s=bad)
            with self.assertRaises(DecisionEvidenceCorrupt, msg=bad):
                await self.repo.get_by_decision_id("pd_one")
        self.assertTrue(await self.repo.exists("pd_one"))    # exists() does not parse

    async def test_tampered_content_and_incompatible_version_rejected(self):
        s = snap()
        await self.repo.save(s)
        d = s.to_dict()
        d["evidence"][0]["value"] = {"tampered": True}
        await self.raw("UPDATE decision_evidence SET snapshot=:s", s=json.dumps(d))
        with self.assertRaises(DecisionEvidenceCorrupt):
            await self.repo.get_by_decision_id("pd_one")
        d = s.to_dict()
        d["contract_version"] = "decision_evidence.v9"
        await self.raw("UPDATE decision_evidence SET snapshot=:s, contract_version='decision_evidence.v9'",
                       s=json.dumps(d))
        with self.assertRaises(DecisionEvidenceCorrupt):
            await self.repo.get_by_decision_id("pd_one")

    async def test_row_snapshot_integrity_mismatch_rejected(self):
        await self.repo.save(snap())
        good = snap()
        cases = [("decision_id='pd_zzz'", "pd_zzz"), ("chat_id=6", "pd_one"),
                 ("message_id=10", "pd_one"), ("message_id=NULL", "pd_one"),
                 ("created_at='2026-10-03 12:00:00'", "pd_one"),
                 ("contract_version='decision_evidence.v2'", "pd_one"),
                 ("decision_evidence_id='de_other'", "pd_one")]
        for setter, lookup in cases:
            await self.raw("DELETE FROM decision_evidence")
            await self.repo.save(good)
            await self.raw(f"UPDATE decision_evidence SET {setter}")
            with self.assertRaises(DecisionEvidenceCorrupt, msg=setter):
                await self.repo.get_by_decision_id(lookup)
        # a corrupt stored row is never mistaken for an identical snapshot
        await self.raw("DELETE FROM decision_evidence")
        await self.repo.save(good)
        await self.raw("UPDATE decision_evidence SET chat_id=6")
        with self.assertRaises(DecisionEvidenceCorrupt):
            await self.repo.save(good)

    async def test_no_mutating_api(self):
        public = {n for n in dir(DecisionEvidenceRepository) if not n.startswith("_")}
        self.assertEqual(public, {"save", "get_by_decision_id", "exists"})
        self.assertTrue(issubclass(DecisionEvidenceConflict, DecisionEvidenceStoreError))


class SafetyTests(unittest.TestCase):
    FILES = [ROOT / "copilot_evidence" / n for n in ("store.py", "repository.py", "migrate.py")]
    BANNED = ("main", "telethon", "fastapi", "starlette", "aiohttp", "httpx", "requests", "socket", "urllib",
              "pickle", "subprocess", "openai", "anthropic", "copilot_decisions", "copilot_shadow",
              "client_state", "language_layer")

    def test_imports(self):
        for f in self.FILES:
            for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                mods = ([a.name for a in n.names] if isinstance(n, ast.Import)
                        else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
                for m in mods:
                    self.assertNotIn(m.split(".")[0], self.BANNED, f"{f.name}: {m}")

    def test_repository_is_insert_only_and_ddl_free(self):
        for name in ("repository.py", "store.py"):
            tree = ast.parse((ROOT / "copilot_evidence" / name).read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    mods = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""]
                    for m in mods:
                        self.assertNotIn("migrate", m, name)
                    if isinstance(n, ast.ImportFrom):
                        self.assertNotIn("migrate", [a.name for a in n.names], name)
                if isinstance(n, ast.Call):
                    f = n.func
                    called = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
                    self.assertNotIn(called, {"update", "delete", "create_all", "drop_all", "text", "merge",
                                              "on_conflict_do_update", "eval", "exec", "open"}, f"{name}:{called}")

    def test_nothing_runs_migration_automatically(self):
        prod = [ROOT / n for n in ("client_state.py", "classifier.py", "embeddings.py")]
        for d in ("copilot_decisions", "copilot_shadow"):
            prod += [p for p in (ROOT / d).glob("*.py") if p.name != "recorder.py"]   # only copilot_decisions/recorder.py may hook capture
        prod += [ROOT / "copilot_evidence" / n for n in ("store.py", "repository.py", "decision.py", "fusion.py")]
        for f in prod:
            if f.exists():
                self.assertNotIn("copilot_evidence.migrate", f.read_text(encoding="utf-8"), f.name)

    def test_production_does_not_reference_persistence(self):
        prod = [ROOT / n for n in ("client_state.py", "classifier.py", "embeddings.py")]
        for d in ("copilot_decisions", "copilot_shadow"):
            prod += [p for p in (ROOT / d).glob("*.py") if p.name != "recorder.py"]   # recorder hooks capture only (test_capture)
        for f in prod:
            if f.exists():
                self.assertNotIn("copilot_evidence", f.read_text(encoding="utf-8"), f.name)

    def test_contract_modules_stay_db_free(self):
        import subprocess
        import sys
        code = ("import sys, copilot_evidence.decision, copilot_evidence.fusion;"
                "sys.exit(1 if 'sqlalchemy' in sys.modules else 0)")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
