import ast
import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from copilot_contracts.stages import Stage
from copilot_decisions import migrate as ledger_migrate
from copilot_decisions import recorder, store as ledger_store
from copilot_evidence import capture, migrate as ev_migrate, repository as repo_mod
from copilot_evidence.contracts import Evidence, EvidenceSource as S, EvidenceType as T, Provenance
from copilot_evidence.repository import DecisionEvidenceRepository

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


class User:
    declared_profile = {"priority": "HIGH", "recommended_mode": "agent", "recommended_model": "A2", "region": "MX"}


CS = {"stage": "deposit_attempt", "regime": "casual", "user_id": 111,
      "identity": {"username": "uname", "name": "Juan", "region": "MX"},
      "conversation": {"intent": "question", "sentiment": 0.7, "complexity": 0.1, "messages_count": 3,
                       "last_message": "SECRET TEXT"},
      "external": {"telegram_profile": {"bio": "BIO"}}, "memory": {"name": "Juan"}, "risk": {}}
FIELDS = dict(chat_id=111, message_id=777, user=User(), regime="agent", model="model-used",
              intended_model="model-wanted", flow_version="v38", trigger="price",
              experiment="v37_vs_v38_test", experiment_variant="v38", used_fallback=False,
              reply_text="hola", splitter=lambda t: [t], objection_signal="", client_state=CS)


def foreign(eid, src, created, observed=None, typ=T.MOMENTUM, did="pd_x"):
    return Evidence(evidence_id=eid, chat_id=111, message_id=777, created_at=created, source=src,
                    evidence_type=typ, value={"k": 1}, confidence=None, reliability=None, observed_at=observed,
                    stage=Stage.UNKNOWN, provenance=Provenance(src, did), metadata={})


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        await ledger_migrate.ensure_schema(self.engine)
        await ev_migrate.ensure_schema(self.engine)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        recorder._seen.clear()
        recorder._state.update(fails=0, paused_until=0.0)
        capture._state.update(fails=0, paused_until=0.0)
        self.env = mock.patch.dict(os.environ, {"COPILOT_DECISION_LEDGER_ENABLED": "true"})
        self.env.start()
        os.environ.pop(capture.FLAG, None)

    async def asyncTearDown(self):
        self.env.stop()
        await self.engine.dispose()

    async def drain(self):
        while recorder._tasks or capture._tasks:
            await asyncio.gather(*list(recorder._tasks), *list(capture._tasks), return_exceptions=True)

    def rec(self, **over):
        return recorder.record_production_decision(session_factory=self.sf, **{**FIELDS, **over})

    async def snapshots(self):
        async with self.sf() as s:
            return (await s.execute(text("SELECT decision_id FROM decision_evidence"))).scalars().all()


class FlagTests(Base):
    async def test_flag_default_off_and_off_creates_nothing(self):
        self.assertFalse(capture.capture_enabled())
        did = self.rec()
        await self.drain()
        self.assertTrue(did.startswith("pd_"))
        self.assertEqual(await self.snapshots(), [])
        with mock.patch.dict(os.environ, {capture.FLAG: "false"}):
            self.assertFalse(capture.schedule_capture(object(), self.sf))
        async with self.sf() as s:                            # ledger itself unaffected
            self.assertEqual(len((await s.execute(select(ledger_store.production_decisions))).all()), 1)

    async def test_flag_on_creates_snapshot(self):
        with mock.patch.dict(os.environ, {capture.FLAG: "true"}):
            did = self.rec()
            await self.drain()
        self.assertEqual(await self.snapshots(), [did])
        snap = await DecisionEvidenceRepository(self.sf).get_by_decision_id(did)
        self.assertEqual((snap.decision_id, snap.chat_id, snap.message_id), (did, 111, 777))
        by_type = {}
        for e in snap.evidence:
            by_type.setdefault(e.evidence_type, []).append(e)
        act = by_type[T.ACTION][0]
        self.assertEqual(act.provenance.source_id, did)
        self.assertEqual(act.value["regime"], "agent")
        self.assertEqual(act.value["model"], "model-used")
        self.assertTrue(act.metadata["not_a_client_fact"])
        self.assertEqual(act.metadata["trigger"], "price")
        # Client State as of the decision, taken from the snapshot embedded in the ledger record
        st = by_type[T.STAGE][0]
        self.assertEqual((st.value["production_stage"], st.stage, st.source), ("deposit_attempt", Stage.PAYMENT, S.CLIENT_STATE))
        self.assertEqual(by_type[T.INTENT][0].value, "question")
        self.assertEqual(st.provenance.source_id, did)
        # decision-time guarantees
        async with self.sf() as s:
            row = (await s.execute(select(ledger_store.production_decisions))).mappings().one()
        dt = row["created_at"].replace(tzinfo=timezone.utc)
        for e in snap.evidence:
            self.assertLessEqual(e.created_at, dt)
        self.assertNotIn("SECRET", str(snap.to_dict()))        # no message text / PII

    async def test_action_not_turned_into_client_assertions(self):
        with mock.patch.dict(os.environ, {capture.FLAG: "1"}):
            did = self.rec(client_state=None)
            await self.drain()
        snap = await DecisionEvidenceRepository(self.sf).get_by_decision_id(did)
        self.assertEqual([e.evidence_type for e in snap.evidence], [T.ACTION])
        self.assertEqual(snap.conflicts, ())

    async def test_duplicate_snapshot_safe(self):
        with mock.patch.dict(os.environ, {capture.FLAG: "true"}):
            did = self.rec()
            await self.drain()
            self.rec()                                        # same (chat, message): ledger returns same id, no 2nd record
            await self.drain()
        self.assertEqual(await self.snapshots(), [did])
        d = recorder.build_decision(chat_id=111, message_id=5, **{k: v for k, v in FIELDS.items() if k not in ("chat_id", "message_id")})
        repo = DecisionEvidenceRepository(self.sf)
        self.assertTrue(await repo.save(capture.build_snapshot(d, now=NOW)))
        self.assertFalse(await repo.save(capture.build_snapshot(d, now=NOW)))
        with self.assertRaises(repo_mod.DecisionEvidenceConflict):   # different content never overwrites
            await repo.save(capture.build_snapshot(d, now=NOW + timedelta(seconds=1)))

    async def test_persistence_failure_does_not_break_production(self):
        class Boom(DecisionEvidenceRepository):
            async def save(self, snapshot):
                raise RuntimeError("db down")
        with mock.patch.dict(os.environ, {capture.FLAG: "true"}), \
                mock.patch("copilot_evidence.repository.DecisionEvidenceRepository", Boom):
            did = self.rec()
            await self.drain()
        self.assertTrue(did.startswith("pd_"))                # production got its id exactly as without capture
        async with self.sf() as s:                            # ledger row still written
            self.assertEqual(len((await s.execute(select(ledger_store.production_decisions))).all()), 1)
        self.assertEqual(capture._state["fails"], 1)
        self.assertEqual(await self.snapshots(), [])

    async def test_missing_table_does_not_break_production(self):
        async with self.sf() as s:
            await s.execute(text("DROP TABLE decision_evidence"))
            await s.commit()
        with mock.patch.dict(os.environ, {capture.FLAG: "true"}):
            did = self.rec()
            await self.drain()
        self.assertTrue(did.startswith("pd_"))

    async def test_build_failure_and_scheduling_failure_swallowed(self):
        with mock.patch.dict(os.environ, {capture.FLAG: "true"}), \
                mock.patch.object(capture, "build_snapshot", side_effect=ValueError("x")):
            did = self.rec()
            await self.drain()
        self.assertTrue(did.startswith("pd_"))
        with mock.patch.dict(os.environ, {capture.FLAG: "true"}), \
                mock.patch.object(capture.asyncio, "get_running_loop", side_effect=RuntimeError):
            self.assertFalse(capture.schedule_capture(object(), self.sf))

    async def test_return_value_identical_with_and_without_capture(self):
        with mock.patch.dict(os.environ, {capture.FLAG: "true"}):
            a = self.rec(message_id=1)
        await self.drain()
        b = self.rec(message_id=2)
        await self.drain()
        self.assertEqual((type(a), a[:3]), (type(b), b[:3]))
        async with self.sf() as s:
            rows = (await s.execute(select(ledger_store.production_decisions))).mappings().all()
        strip = lambda r: {k: v for k, v in r.items() if k not in ("id", "decision_id", "message_id", "created_at")}
        self.assertEqual(strip(rows[0]), strip(rows[1]))      # ledger rows identical regardless of the flag


class TemporalTests(unittest.TestCase):
    T0 = NOW

    def test_pre_decision_kept_and_exact_time_kept(self):
        a = foreign("a", S.CLIENT_STATE, self.T0 - timedelta(seconds=5), self.T0 - timedelta(seconds=9))
        b = foreign("b", S.EXTERNAL, self.T0, self.T0)
        kept, ex = capture.filter_pre_decision([a, b], self.T0)
        self.assertEqual({e.evidence_id for e in kept}, {"a", "b"})
        self.assertEqual(ex, ())

    def test_post_decision_rejected_in_mixed_set(self):
        pre = foreign("pre", S.CLIENT_STATE, self.T0 - timedelta(seconds=1))
        post = foreign("post", S.CLIENT_STATE, self.T0 + timedelta(microseconds=1))
        obs_post = foreign("obs", S.EXTERNAL, self.T0, self.T0 + timedelta(seconds=1))
        kept, ex = capture.filter_pre_decision([post, pre, obs_post], self.T0)
        self.assertEqual([e.evidence_id for e in kept], ["pre"])
        self.assertEqual({x.evidence_id: x.reason for x in ex},
                         {"post": "created_after_decision", "obs": "observed_after_decision"})

    def test_shadow_always_excluded_even_if_earlier(self):
        sh = foreign("sh", S.SHADOW, self.T0 - timedelta(days=1))
        kept, ex = capture.filter_pre_decision([sh], self.T0)
        self.assertEqual(kept, ())
        self.assertEqual(ex[0].reason, "shadow_is_post_decision_analytics")

    def test_no_timestamp_fabrication(self):
        e = foreign("a", S.EXTERNAL, self.T0)
        for bad in (None, datetime(2026, 10, 2), "2026-10-02"):
            kept, ex = capture.filter_pre_decision([e], bad)
            self.assertEqual(kept, ())
            self.assertEqual(ex[0].reason, "decision_time_unreliable")
        kept, ex = capture.filter_pre_decision(["junk", None], self.T0)
        self.assertEqual([x.reason for x in ex], ["not_an_evidence"] * 2)
        src = (ROOT / "copilot_evidence" / "capture.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("datetime.now("), 1)        # only the snapshot's own created_at

    def test_collect_applies_gate_to_extra_evidence(self):
        d = recorder.build_decision(chat_id=111, message_id=1, now=self.T0,
                                    **{k: v for k, v in FIELDS.items() if k not in ("chat_id", "message_id")})
        extra = [foreign("late", S.EXTERNAL, self.T0 + timedelta(seconds=3)),
                 foreign("sh", S.SHADOW, self.T0 - timedelta(seconds=3)),
                 foreign("early", S.EXTERNAL, self.T0 - timedelta(seconds=3))]
        kept, ex = capture.collect_pre_decision_evidence(d, extra)
        ids = {e.evidence_id for e in kept}
        self.assertIn("early", ids)
        self.assertFalse(ids & {"late", "sh"})
        self.assertEqual({x.evidence_id for x in ex}, {"late", "sh"})
        self.assertTrue(all(e.created_at <= self.T0 for e in kept))
        snap = capture.build_snapshot(d, extra, now=NOW + timedelta(minutes=1))
        self.assertEqual(snap.created_at, NOW + timedelta(minutes=1))     # snapshot time != evidence time
        self.assertEqual({e.evidence_id for e in snap.evidence}, ids)


class SafetyTests(unittest.TestCase):
    def test_capture_imports(self):
        banned = {"main", "telethon", "fastapi", "aiohttp", "httpx", "requests", "socket", "urllib", "pickle",
                  "subprocess", "openai", "anthropic", "copilot_shadow", "copilot_decisions", "scout",
                  "client_state"}
        for n in ast.walk(ast.parse((ROOT / "copilot_evidence" / "capture.py").read_text(encoding="utf-8"))):
            mods = ([a.name for a in n.names] if isinstance(n, ast.Import)
                    else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
            for m in mods:
                self.assertNotIn(m.split(".")[0], banned, m)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                self.assertNotIn(n.func.id, {"open", "eval", "exec"})

    def test_flag_default_off(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(capture.FLAG, None)
            self.assertFalse(capture.capture_enabled())
        for v in ("", "0", "no", "false", "off", "x"):
            with mock.patch.dict(os.environ, {capture.FLAG: v}):
                self.assertFalse(capture.capture_enabled(), v)
        self.assertNotEqual(capture.FLAG, "COPILOT_DECISION_LEDGER_ENABLED")


if __name__ == "__main__":
    unittest.main()
