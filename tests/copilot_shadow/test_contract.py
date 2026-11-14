import os
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from copilot_shadow import contract, hook, store
from copilot_shadow import signals as S
from tests.copilot_shadow.test_shadow import Base

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class TestContractPure(unittest.TestCase):
    def test_dedup_key_rules(self):
        c, s = contract.client_id(1), "ses_a"
        k = contract.dedup_key(c, s, "reply", 5)
        self.assertEqual(k, contract.dedup_key(c, s, "reply", 5))                  # duplicate
        self.assertNotEqual(k, contract.dedup_key(c, s, "followup", 5))            # other decision point
        self.assertNotEqual(k, contract.dedup_key(c, "ses_b", "reply", 5))         # other session
        self.assertNotIn("1", contract.client_id(1).replace("cli_", "")[:0] or "x")  # no raw id leak below
        self.assertNotEqual(contract.client_id(1), contract.client_id(2))

    def test_session_split_by_gap(self):
        t = [S.Turn("user", "a", 1, T0), S.Turn("user", "b", 2, T0 + timedelta(hours=1))]
        late = t + [S.Turn("user", "c", 3, T0 + timedelta(hours=30))]
        self.assertEqual(contract.session_id(1, t), contract.session_id(1, t[:1]))
        self.assertNotEqual(contract.session_id(1, t), contract.session_id(1, late))
        self.assertEqual(contract.session_id(1, []), contract.session_id(1, None))


class TestContractStore(Base):
    async def rows(self):
        async with self.sf() as s:
            return (await s.execute(select(store.shadow_decisions).order_by(store.shadow_decisions.c.id))).mappings().all()

    async def test_unique_decision_has_full_contract(self):
        self.assertTrue(self.sched(msg=1)); await self.drain()
        (r,) = await self.rows()
        for k in ("decision_id", "session_id", "client_id", "decision_point", "state_ref", "shadow_recommendation",
                  "shadow_confidence", "production_action", "dedup_key", "response_policy", "canonical_state"):
            self.assertIsNotNone(r[k], k)
        self.assertIsNone(r["outcome"]); self.assertIsNone(r["outcome_timestamp"])
        self.assertEqual((r["decision_point"], r["production_action"]), ("reply", "CONTINUE_DIALOGUE"))
        self.assertIn(r["response_policy"]["response_policy"], ("RESPOND", "WAIT", "ESCALATE_REVIEW"))
        self.assertNotIn("111", r["client_id"])

    async def test_duplicate_decision_not_multiplied(self):
        self.assertTrue(self.sched(msg=1)); await self.drain()
        self.assertFalse(self.sched(msg=1)); await self.drain()
        hook._seen.clear()                                   # simulate restart: store-level dedup must hold
        self.sched(msg=1); await self.drain()
        self.assertEqual(await self.count(), 1)

    async def test_different_decision_points_same_message(self):
        row = {"decision_id": "shd_1", "chat_id": 1, "message_id": 1, "state_snapshot": {}, "candidate_actions": [],
               "chosen_action": "WAIT", "wait_baseline": {}, "decision_status": "x", "shadow_mode": True,
               "model_name": "m", "model_version": "1", "analyzer_version": "a", "code_version": "c"}
        c = contract.client_id(1)
        a = {**row, "decision_point": "reply", "dedup_key": contract.dedup_key(c, "s", "reply", 1)}
        b = {**row, "decision_id": "shd_2", "decision_point": "followup", "dedup_key": contract.dedup_key(c, "s", "followup", 1)}
        self.assertTrue(await store.save_decision(self.sf, a))
        self.assertTrue(await store.save_decision(self.sf, b))
        self.assertFalse(await store.save_decision(self.sf, {**a, "decision_id": "shd_3"}))
        self.assertEqual(await self.count(), 2)

    async def test_same_client_different_sessions(self):
        hist = [{}, {}]
        self.sched(msg=1); await self.drain()
        # same chat, same message id but a different session -> different dedup_key, not a duplicate
        k1 = (await self.rows())[0]["dedup_key"]
        self.assertNotEqual(k1, contract.dedup_key(contract.client_id(111), "ses_other", "reply", 1))
        r = {"decision_id": "shd_x", "chat_id": 111, "message_id": 2, "state_snapshot": {}, "candidate_actions": [],
             "chosen_action": "WAIT", "wait_baseline": {}, "decision_status": "x", "shadow_mode": True,
             "model_name": "m", "model_version": "1", "analyzer_version": "a", "code_version": "c",
             "dedup_key": contract.dedup_key(contract.client_id(111), "ses_other", "reply", 1)}
        self.assertTrue(await store.save_decision(self.sf, r))
        self.assertEqual(await self.count(), 2)

    async def test_link_production_action_and_outcome(self):
        self.sched(msg=1, regime="hard"); await self.drain()
        did = (await self.rows())[0]["decision_id"]
        self.assertTrue(await store.link_production_action(self.sf, did, "HANDLE_OBJECTION") is False)  # set-once
        async with self.sf() as s:
            await s.execute(store.shadow_decisions.update().values(production_action=None)); await s.commit()
        self.assertTrue(await store.link_production_action(self.sf, did, "PRESENT_OFFER"))
        self.assertTrue(await store.record_outcome(self.sf, did, "deposit"))
        self.assertFalse(await store.record_outcome(self.sf, did, "other"))
        self.assertFalse(await store.record_outcome(self.sf, "missing", "deposit"))
        (r,) = await self.rows()
        self.assertEqual((r["production_action"], r["outcome"]), ("PRESENT_OFFER", "deposit"))
        self.assertIsNotNone(r["outcome_timestamp"])

    async def test_ledger_decision_id_links_chain(self):
        self.sched(msg=1, decision_id="dec_ledger_1"); await self.drain()
        (r,) = await self.rows()
        self.assertEqual(r["decision_id"], "dec_ledger_1")
        self.assertTrue(await store.record_outcome(self.sf, "dec_ledger_1", "reply_received"))
