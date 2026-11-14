import ast
import asyncio
import copy
import dataclasses
import json
import os
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from copilot_decisions import UNKNOWN, ProductionDecision, record_production_decision
from copilot_decisions import contract, migrate, recorder, store
from copilot_decisions.recorder import build_decision
from copilot_shadow import hook as shadow_hook
from copilot_shadow import store as shadow_store

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "copilot_decisions"
NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


def split(t):               # stand-in for main._split_reply_into_parts
    return [t[:100], t[100:]] if len(t) > 150 else [t]


class User:
    external_signals = {"priority": "HIGH", "recommended_mode": "agent", "recommended_model": "A2",
                     "has_gambling_breach": True, "region": "MX", "is_gamer": True}
    stage = "conversation"


CS = {"stage": "conversation", "regime": "casual", "user_id": 1,
      "identity": {"username": "uname", "name": "Juan", "region": "MX"},
      "conversation": {"intent": "question", "sentiment": 0.5, "complexity": 0.1, "messages_count": 3,
                       "last_message": "SECRET TEXT"},
      "external": {"priority": "HIGH", "is_gamer": True, "has_gambling_breach": True,
                "telegram_profile": {"bio": "BIO"}},
      "memory": {"name": "Juan", "work": "plomero"}, "risk": {"self_exclusion": False, "minor": False}}

FULL = dict(chat_id=111, message_id=777, user=User(), regime="agent", model="model-used", intended_model="model-wanted",
            flow_version="v38", trigger="", experiment="v37_vs_v38_test", experiment_variant="v38",
            used_fallback=False, reply_text="hola " * 5, splitter=split, objection_signal="agent", client_state=CS)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        await migrate.ensure_schema(self.engine)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        recorder._seen.clear()
        recorder._state.update(fails=0, paused_until=0.0)
        self.env = mock.patch.dict(os.environ, {"COPILOT_DECISION_LEDGER_ENABLED": "true"})
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        await self.engine.dispose()

    async def drain(self):
        while recorder._tasks:
            await asyncio.gather(*list(recorder._tasks), return_exceptions=True)

    async def rows(self):
        async with self.sf() as s:
            return (await s.execute(select(store.production_decisions))).mappings().all()

    def rec(self, **over):
        args = {**FULL, "session_factory": self.sf}
        args.update(over)
        return record_production_decision(**args)


class TestContract(unittest.TestCase):
    def test_1_valid_decision_created(self):
        d = build_decision(**FULL, now=NOW)
        self.assertIsInstance(d, ProductionDecision)
        self.assertTrue(d.decision_id.startswith("pd_"))
        self.assertEqual((d.chat_id, d.created_at, d.decision_path), (111, NOW, "llm_reply"))
        json.dumps(d.to_dict())
        for bad in (dict(decision_id=""), dict(decision_id="xx_1"), dict(chat_id="1"), dict(message_id="7"),
                    dict(created_at=datetime(2026, 1, 1)), dict(reply_parts=-1), dict(used_fallback="no")):
            kw = dict(decision_id="pd_1", chat_id=1, message_id=1, created_at=NOW)
            kw.update(bad)
            with self.assertRaises((ValueError, TypeError), msg=str(bad)):
                ProductionDecision(**kw)

    def test_2_decision_id_unique(self):
        ids = {build_decision(chat_id=1, message_id=i).decision_id for i in range(2000)}
        self.assertEqual(len(ids), 2000)

    def test_3_message_id_kept_or_explicitly_missing(self):
        self.assertEqual(build_decision(**FULL).message_id, 777)
        d = build_decision(**{**FULL, "message_id": None})
        self.assertIsNone(d.message_id)
        self.assertEqual(d.metadata["unavailable"]["message_id"], "message_id_unavailable")

    def test_4_parameters_mapped(self):
        d = build_decision(**FULL)
        self.assertEqual((d.regime, d.model, d.flow_version, d.used_fallback), ("agent", "model-used", "v38", False))
        self.assertEqual((d.experiment_variant, d.objection_signal), ("v38", "agent"))
        self.assertEqual(d.metadata["intended_model"], "model-wanted")
        self.assertEqual(d.metadata["experiment_name"], "v37_vs_v38_test")
        self.assertEqual(d.reply_parts, 1)                      # 'hola hola ...' < 150 chars
        self.assertEqual(build_decision(**{**FULL, "reply_text": "x" * 400}).reply_parts, 2)
        self.assertIn(d.language_mode, {"off", "shadow", "active"})

    def test_5_null_and_unknown_handling(self):
        d = build_decision(chat_id=1, message_id=2)             # nothing else known
        for f in ("regime", "model", "flow_version", "used_fallback", "reply_parts", "trigger",
                  "experiment_variant", "objection_signal", "client_state_snapshot"):
            self.assertIsNone(getattr(d, f), f)
        u = d.metadata["unavailable"]
        for f in ("regime", "model", "flow_version", "used_fallback", "reply_parts", "trigger",
                  "experiment_variant", "objection_signal", "client_state_snapshot", "experiment_id"):
            self.assertIn(f, u, f)
        self.assertEqual(u["experiment_id"], "not_exposed_by_get_experiment_variant")
        # known-empty is NOT unknown
        k = build_decision(chat_id=1, message_id=2, trigger="", experiment_variant=None, objection_signal="")
        self.assertNotIn("trigger", k.metadata["unavailable"])
        self.assertEqual(k.metadata["status"], {"trigger": "none_detected", "experiment_variant": "no_assignment",
                                                "objection_signal": "no_marker"})
        self.assertEqual(k.metadata["not_captured"]["route"], "pre_override_route_not_captured")
        self.assertFalse(UNKNOWN)

    def test_7_immutable_dataclass_and_row_copy(self):
        d = build_decision(**FULL)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            d.regime = "casual"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            d.model = "x"
        row = d.to_row()
        row["metadata"]["unavailable"]["injected"] = 1
        row["client_state_snapshot"]["stage"] = "hacked"
        self.assertNotIn("injected", d.metadata["unavailable"])
        self.assertEqual(d.client_state_snapshot["stage"], "conversation")

    def test_9_snapshot_whitelist(self):
        s = contract.build_client_state_snapshot(CS)
        self.assertEqual(set(s), {"stage", "intent", "sentiment", "complexity", "messages_count", "region", "snapshot_version"})
        blob = json.dumps(s)
        for frag in ("SECRET", "uname", "Juan", "plomero", "BIO", "risk", "last_message", "memory", "gambling", "username"):
            self.assertNotIn(frag, blob)
        self.assertIsNone(contract.build_client_state_snapshot(None))
        self.assertIsNone(contract.build_client_state_snapshot("x"))
        self.assertIsNone(contract.build_client_state_snapshot({"identity": {"region": "??"}})["region"])
        self.assertIsNone(contract.build_client_state_snapshot({"identity": {"region": "Mexico City"}})["region"])

    def test_field_spec_complete(self):
        cols = {c.name for c in store.production_decisions.c} - {"id"}
        self.assertEqual(cols, set(contract.FIELD_SPEC))
        self.assertEqual(cols, {f.name for f in dataclasses.fields(ProductionDecision)})
        for name, spec in contract.FIELD_SPEC.items():
            self.assertEqual(set(spec), {"kind", "source", "meaning", "nullable"}, name)
            self.assertIn(spec["kind"], {"identity", "action", "observation", "meta"})
        kinds = {n: s["kind"] for n, s in contract.FIELD_SPEC.items()}
        for a in ("regime", "model", "flow_version", "reply_parts", "used_fallback"):
            self.assertEqual(kinds[a], "action")
        for o in ("trigger", "objection_signal", "language_mode", "experiment_variant", "client_state_snapshot"):
            self.assertEqual(kinds[o], "observation")

    def test_no_forbidden_imports_or_writes(self):
        for p in PKG.glob("*.py"):
            tree = ast.parse(p.read_text())
            for n in ast.walk(tree):
                mods = [a.name for a in n.names] if isinstance(n, ast.Import) else \
                       [n.module or ""] if isinstance(n, ast.ImportFrom) else []
                for m in mods:
                    self.assertNotIn(m.split(".")[0], {"main", "telethon", "scout", "classifier", "client_state", "copilot_external"}, p.name)
                if isinstance(n, ast.Attribute):
                    self.assertNotIn(n.attr, {"send_message", "reply", "on_conflict_do_update", "create_all", "execute_update"}
                                     if p.name in ("recorder.py", "store.py") else {"send_message", "reply"}, p.name)
        # write path (recorder/store): INSERT ... ON CONFLICT DO NOTHING only; no UPDATE, no DDL execution
        for name in ("recorder.py", "store.py"):
            tree = ast.parse((PKG / name).read_text())
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("sqlalchemy"):
                    self.assertNotIn("update", [a.name for a in n.names], name)
                    self.assertNotIn("delete", [a.name for a in n.names], name)
                if isinstance(n, ast.Attribute):
                    self.assertNotIn(n.attr, {"update", "delete", "create_all", "drop_all", "on_conflict_do_update"}, name)
                if isinstance(n, ast.Name):
                    self.assertNotIn(n.id, {"update", "delete", "text", "CreateTable"}, name)
        # DDL lives only in migrate.py and is never invoked on import
        for name in ("__init__.py", "recorder.py", "store.py", "contract.py"):
            for n in ast.walk(ast.parse((PKG / name).read_text())):
                if isinstance(n, ast.ImportFrom):
                    self.assertNotIn("migrate", (n.module or "") + ",".join(a.name for a in n.names), name)
                if isinstance(n, ast.Import):
                    self.assertNotIn("migrate", ",".join(a.name for a in n.names), name)


class TestRecorder(Base):
    async def test_4b_parameters_persisted(self):
        did = self.rec()
        await self.drain()
        (r,) = await self.rows()
        self.assertEqual(r["decision_id"], did)
        self.assertEqual((r["chat_id"], r["message_id"]), (111, 777))
        self.assertEqual((r["regime"], r["model"], r["flow_version"], r["used_fallback"], r["reply_parts"]),
                         ("agent", "model-used", "v38", False, 1))
        self.assertEqual((r["experiment_variant"], r["objection_signal"], r["trigger"]), ("v38", "agent", None))
        self.assertIsNone(r["experiment_id"])
        self.assertEqual(r["decision_path"], "llm_reply")
        self.assertEqual(r["client_state_snapshot"]["intent"], "question")
        self.assertEqual(r["metadata"]["contract_version"], "production-decision-1")
        self.assertIsNotNone(r["created_at"])

    async def test_6_errors_never_propagate(self):
        def boom():
            raise RuntimeError("db down")
        bad = mock.MagicMock(side_effect=boom)
        did = self.rec(session_factory=bad)                    # persist fails in the background
        self.assertTrue(did.startswith("pd_"))
        await self.drain()
        self.assertEqual(recorder._state["fails"], 1)
        self.assertIsNone(record_production_decision(session_factory=self.sf, chat_id="not-an-int", message_id=1))
        self.assertIsNone(record_production_decision(session_factory=self.sf, chat_id=None, message_id=None))

        async def slow(*a, **k):
            await asyncio.sleep(5)
        with mock.patch.object(recorder, "TIMEOUT_S", 0.05), mock.patch.object(store, "insert_decision", slow):
            self.assertTrue(self.rec(message_id=900).startswith("pd_"))
            await self.drain()
        self.assertEqual(await self.rows(), [])

    async def test_6b_circuit_breaker_no_retry_storm(self):
        bad = mock.MagicMock(side_effect=RuntimeError("x"))
        for i in range(3):
            self.rec(session_factory=bad, message_id=100 + i)
            await self.drain()
        self.assertGreater(recorder._state["paused_until"], time.monotonic())
        n = bad.call_count
        self.rec(session_factory=bad, message_id=200)           # paused: no new task, id still returned
        await self.drain()
        self.assertEqual(bad.call_count, n)

    async def test_missing_table_is_only_logged(self):
        eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)   # no migration run
        sf = async_sessionmaker(eng)
        self.assertTrue(self.rec(session_factory=sf).startswith("pd_"))
        await self.drain()                                      # no exception, and the recorder did NOT create the table
        async with sf() as s:
            n = (await s.execute(text("SELECT count(*) FROM sqlite_master WHERE name='production_decisions'"))).scalar()
        self.assertEqual(n, 0)
        await eng.dispose()

    async def test_7b_duplicate_never_rewrites_history(self):
        self.rec(regime="agent")
        await self.drain()
        recorder._seen.clear()
        self.rec(regime="casual", model="other", flow_version="v37")   # a later, different attempt for the same message
        await self.drain()
        (r,) = await self.rows()
        self.assertEqual((r["regime"], r["model"], r["flow_version"]), ("agent", "model-used", "v38"))

    async def test_8_shadow_receives_same_decision_id(self):
        did = self.rec()
        self.assertTrue(shadow_hook._build_row(111, 777, User(), CS, [], "agent", "x", did)["decision_id"] == did)
        self.assertTrue(shadow_hook._build_row(111, 777, User(), CS, [], "agent", "x")["decision_id"].startswith("shd_"))
        self.assertTrue(shadow_hook._build_row(111, 777, User(), CS, [], "agent", "x", "")["decision_id"].startswith("shd_"))
        # end to end through the shadow store
        await shadow_store.ensure_table(self.sf)
        shadow_hook._seen.clear()
        with mock.patch.dict(os.environ, {"COPILOT_SHADOW_ENABLED": "true"}):
            self.assertTrue(shadow_hook.schedule_shadow(session_factory=self.sf, chat_id=111, message_id=777,
                            user=User(), client_state=CS, history=[{"role": "user", "content": "hola"}],
                            regime="agent", reply_text="x", decision_id=did))
            while shadow_hook._tasks:
                await asyncio.gather(*list(shadow_hook._tasks), return_exceptions=True)
        await self.drain()
        async with self.sf() as s:
            sd = (await s.execute(select(shadow_store.shadow_decisions.c.decision_id))).scalar()
            pd = (await s.execute(select(store.production_decisions.c.decision_id))).scalar()
        self.assertEqual(sd, pd)
        self.assertEqual(sd, did)

    async def test_10_duplicate_and_retry_single_record(self):
        a = self.rec()
        b = self.rec()                                          # in-process retry: same id, no 2nd task
        self.assertEqual(a, b)
        await self.drain()
        recorder._seen.clear()                                  # simulate restart: DB constraint decides
        self.rec()
        await self.drain()
        self.assertEqual(len(await self.rows()), 1)

    async def test_null_message_id_records_each_time(self):
        self.rec(message_id=None)
        self.rec(message_id=None)
        await self.drain()
        rows = await self.rows()
        self.assertEqual(len(rows), 2)                          # NULL never conflicts; not deduplicated by guesswork
        self.assertEqual({r["metadata"]["unavailable"]["message_id"] for r in rows}, {"message_id_unavailable"})

    async def test_flag_disables_everything(self):
        for v in ("false", "", "0", "no"):
            with mock.patch.dict(os.environ, {"COPILOT_DECISION_LEDGER_ENABLED": v}):
                self.assertIsNone(self.rec(message_id=5))
        with mock.patch.dict(os.environ):
            os.environ.pop("COPILOT_DECISION_LEDGER_ENABLED", None)
            self.assertIsNone(self.rec(message_id=6))            # default OFF
        await self.drain()
        self.assertEqual(await self.rows(), [])

    async def test_inputs_not_mutated(self):
        u, cs = User(), copy.deepcopy(CS)
        before = (copy.deepcopy(u.external_signals), copy.deepcopy(cs))
        self.rec(user=u, client_state=cs)
        await self.drain()
        self.assertEqual(before, (u.external_signals, cs))

    async def test_migration_idempotent(self):
        await migrate.ensure_schema(self.engine)
        await migrate.ensure_schema(self.engine)
        async with self.sf() as s:
            idx = {r[1] for r in (await s.execute(text("PRAGMA index_list(production_decisions)"))).all()}
        self.assertTrue({"ix_pd_chat_created", "ix_pd_message_id"} <= idx)


if __name__ == "__main__":
    unittest.main()
