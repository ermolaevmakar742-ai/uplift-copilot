import ast
import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select, text

from copilot_learning import contract as C
from copilot_learning import learner as L
from copilot_learning import store as LS
from copilot_learning.critic import critique
from copilot_shadow import hook, store
from tests.copilot_shadow.test_shadow import Base

ROOT = Path(__file__).resolve().parents[2]
NBQ = {"recommended": True, "question_intent": "ASK_PAYMENT_BARRIER", "target_unknown": "PAYMENT"}
PRICE = {"classification": "PRICING_OBJECTION", "confidence": 0.8}
BAD, GOOD = "Regístrate aquí ahora mismo y deposita hoy mismo.", "Entiendo, es normal. ¿Qué te preocupa más del pago: el método o el monto?"


def srow(did, reward=None, rv=None, detail=None, corr=None, msg=1):
    return {"decision_id": did, "chat_id": 1, "message_id": msg, "state_snapshot": {}, "candidate_actions": [], "chosen_action": "WAIT",
            "wait_baseline": {}, "decision_status": "x", "shadow_mode": True, "model_name": "m", "model_version": "1",
            "analyzer_version": "a", "code_version": "c", "dedup_key": "dk_" + did, "reward": reward, "reward_version": rv,
            "reward_detail": detail, "reward_corrections": corr}


class LBase(Base):
    n = 0

    async def asyncSetUp(self):
        await Base.asyncSetUp(self)
        LS._ready.clear()

    async def seed(self, text_, reward, did=None, rv="reward.v1", **kw):
        LBase.n += 1
        did = did or f"pd_{LBase.n}"
        await store.save_decision(self.sf, srow(did, reward, rv if reward is not None else None, {"params_id": "rp_1"} if reward is not None else None,
                                                msg=LBase.n))
        c = critique(response_text=text_, client_text="muy caro", stage="conversation", reply_classification=PRICE,
                     next_best_question=NBQ, state_ref="st_1", decision_id=did)
        await LS.save_critique(self.sf, c)
        return did

    async def rows(self, table):
        async with self.sf() as s:
            return (await s.execute(select(table))).mappings().all()


class TestPipeline(LBase):
    async def test_missing_reward_example_stays_unevaluated_and_critic_data_kept(self):
        await self.seed(BAD, None)
        out = await L.run_once(self.sf)
        (e,) = await self.rows(LS.examples)
        self.assertEqual((e["quality"], e["usable"], e["example_version"], e["critic_version"]), ("unevaluated", True, "example-1", "dialogue-critic-1"))
        self.assertEqual(len(await self.rows(LS.critiques)), 1)
        self.assertEqual(out["learner_version"], "learner-1")

    async def test_unknown_decision_in_shadow_store_is_not_labelled(self):
        c = critique(response_text=BAD, stage="conversation", reply_classification=PRICE, decision_id="pd_ghost")
        await LS.save_critique(self.sf, c); await L.run_once(self.sf)
        self.assertEqual((await self.rows(LS.examples))[0]["quality"], "unevaluated")

    async def test_outcome_and_reward_linkage_and_labels(self):
        await self.seed(BAD, 1.0, "pd_win"); await self.seed(BAD, 0.0, "pd_flat"); await self.seed(BAD, -0.5, "pd_loss")
        await L.run_once(self.sf)
        q = {e["decision_id"]: (e["quality"], e["reward"], e["reward_version"], e["reward_basis"]) for e in await self.rows(LS.examples)}
        self.assertEqual(q["pd_win"], ("success", 1.0, "reward.v1", "original"))
        self.assertEqual(q["pd_flat"][0], "neutral"); self.assertEqual(q["pd_loss"][0], "failure")

    async def test_delayed_correction_is_reflected_without_rewriting_history(self):
        await self.seed(BAD, 1.0, "pd_c")
        await L.run_once(self.sf)
        async with self.sf() as s:
            await s.execute(store.shadow_decisions.update().where(store.shadow_decisions.c.decision_id == "pd_c").values(
                reward_corrections=[{"corrected_reward": -0.2, "reward_version": "reward.v1", "params_id": "rp_1", "event_ids": ["cb1"]}]))
            await s.commit()
        await L.run_once(self.sf); await L.run_once(self.sf)                       # idempotent
        (e,) = await self.rows(LS.examples)
        self.assertEqual((e["quality"], e["reward"], e["reward_basis"]), ("failure", -0.2, "corrected"))
        self.assertEqual([h["quality"] for h in e["history"]], ["success", "failure"])        # original label kept in the audit trail
        (sd,) = [r for r in await self.rows(store.shadow_decisions) if r["decision_id"] == "pd_c"]
        self.assertEqual(sd["reward"], 1.0)                                                    # shadow reward untouched

    async def test_duplicate_decision_and_rerun_do_not_duplicate(self):
        did = await self.seed(BAD, 1.0)
        c = critique(response_text=BAD, stage="conversation", decision_id=did)
        self.assertFalse(await LS.save_critique(self.sf, c))
        for _ in range(3):
            await L.run_once(self.sf)
        self.assertEqual((len(await self.rows(LS.critiques)), len(await self.rows(LS.examples))), (1, 1))

    async def test_low_confidence_critique_is_unreliable_and_outside_patterns(self):
        c = critique(response_text="", stage=None, decision_id="pd_low")
        await LS.save_critique(self.sf, c); await L.run_once(self.sf)
        (e,) = await self.rows(LS.examples)
        self.assertEqual((e["quality"], e["usable"], e["pattern_id"]), ("unreliable", False, None))
        self.assertEqual(await self.rows(LS.patterns), [])

    async def test_pattern_aggregation_from_observed_data(self):
        for i in range(4):
            await self.seed(BAD, 1.0 if i == 0 else 0.0)
        for i in range(3):
            await self.seed(GOOD, 0.0)
        await self.seed(BAD, None)
        await L.run_once(self.sf)
        pats = {p["signature_text"]: p for p in await self.rows(LS.patterns)}
        bad = next(p for t, p in pats.items() if "neg=" in t and "IGNORED_OBJECTION" in t)
        self.assertEqual((bad["sample_count"], bad["success"], bad["neutral"], bad["failure"], bad["unevaluated"]), (5, 1, 3, 0, 1))
        self.assertEqual(bad["pattern_version"], "pattern-1"); self.assertIsNotNone(bad["first_seen"]); self.assertLessEqual(bad["first_seen"], bad["last_seen"])
        good = next(p for t, p in pats.items() if "neg=NONE" in t)
        self.assertEqual(good["sample_count"], 3)
        self.assertTrue(bad["pattern_id"].startswith("pt_"))

    async def test_pattern_versions_coexist_on_the_same_sample(self):
        for _ in range(3):
            await self.seed(BAD, 0.0)
        await L.run_once(self.sf)
        L.SIGNATURES["pattern-test2"] = lambda stage, rc, reasons, f: {"stage": stage, "reply_class": rc, "neg": [], "question": False, "offer": False}
        try:
            await L.mine_patterns(self.sf, "pattern-test2")
        finally:
            del L.SIGNATURES["pattern-test2"]
        by = {}
        for p in await self.rows(LS.patterns):
            by.setdefault(p["pattern_version"], []).append(p["sample_count"])
        self.assertEqual(by, {"pattern-1": [3], "pattern-test2": [3]})

    async def test_improvement_lifecycle_stops_at_offline_evaluated(self):
        for i in range(25):
            await self.seed(BAD, 0.0 if i % 5 else 1.0)          # 5 success / 20 neutral with the problem
        for i in range(12):
            await self.seed(GOOD, 1.0 if i % 2 else 0.0)         # 6 success / 6 neutral without
        await self.seed(BAD, None, "pd_few")
        await L.run_once(self.sf)
        imps = await self.rows(LS.improvements)
        cand = [i for i in imps if i["state"] == "CANDIDATE"]
        self.assertEqual(len(cand), 1)
        i = cand[0]
        self.assertEqual((i["improvement_version"], i["learner_version"], i["problem"]), ("improvement-1", "learner-1", "IGNORED_OBJECTION"))
        self.assertGreaterEqual(i["evidence_count"], 20); self.assertTrue(i["supporting_examples"]); self.assertTrue(i["contraindications"])
        self.assertIn("unknown until offline evaluation", i["expected_effect"])
        ev = await L.offline_evaluate(self.sf, i["improvement_id"])
        self.assertEqual((ev["causal"], ev["kind"], ev["sufficient"]), (False, "observational", True))
        self.assertEqual(ev["with_reasons"]["n"], 25); self.assertEqual(ev["without_reasons"]["n"], 12)
        self.assertLess(ev["with_reasons"]["rate"], ev["without_reasons"]["rate"])
        self.assertEqual([s for s in (await self.rows(LS.improvements)) if s["improvement_id"] == i["improvement_id"]][0]["state"], "OFFLINE_EVALUATED")
        with self.assertRaises(L.LifecycleError):
            await L.offline_evaluate(self.sf, i["improvement_id"])                           # not CANDIDATE any more
        for st in ("HUMAN_REVIEW", "APPROVED", "REJECTED", "OBSERVED", "BOGUS"):
            with self.assertRaises(L.LifecycleError):
                await L.advance(self.sf, i["improvement_id"], st)
        await L.run_once(self.sf)                                                             # never demoted / duplicated
        self.assertEqual(len(await self.rows(LS.improvements)), len(imps))

    async def test_low_evidence_is_only_observed(self):
        for _ in range(4):
            await self.seed(BAD, 0.0)
        await L.run_once(self.sf)
        (i,) = await self.rows(LS.improvements)
        self.assertEqual(i["state"], "OBSERVED"); self.assertIn("LOW_LABEL_COVERAGE", i["contraindications"])
        with self.assertRaises(L.LifecycleError):
            await L.offline_evaluate(self.sf, i["improvement_id"])

    async def test_retrieval_and_calibration_are_research_only(self):
        await self.seed(GOOD, 1.0, "pd_ok"); await self.seed(BAD, 0.0); await self.seed(GOOD, -1.0)
        await L.run_once(self.sf)
        r = await L.retrieve(self.sf, "conversation", "PRICING_OBJECTION", ["GOOD_OBJECTION_HANDLING"])
        self.assertEqual([x["decision_id"] for x in r], ["pd_ok"]); self.assertTrue(r[0]["shadow_only"])
        self.assertNotIn("text", r[0])
        cal = await L.calibration(self.sf)
        self.assertEqual(sum(b["n"] for b in cal), 3)


class TestHookIntegration(LBase):
    async def test_shadow_hook_writes_critique_only_when_flag_on_and_no_raw_text(self):
        secret = "SECRETREPLY juan@mail.com +52 55 1234 5678 https://t.me/juan1 ahora mismo"
        self.sched(msg=1, reply_text=secret); await self.drain()
        async with self.sf() as s:                                                           # flag off: no learning table at all
            with self.assertRaises(Exception):
                await s.execute(select(LS.critiques))
        hook._seen.clear(); store._ready.clear()
        with mock.patch.dict(os.environ, {C.FLAG: "true", "COPILOT_SHADOW_ENABLED": "true"}):
            self.sched(msg=2, reply_text=secret); await self.drain()
        (c,) = await self.rows(LS.critiques)
        self.assertEqual(c["critic_version"], "dialogue-critic-1")
        self.assertIn("TOO_PUSHY", c["reasons"])
        dump = json.dumps([dict(r) for r in await self.rows(LS.critiques)], default=str)
        for needle in ("SECRETREPLY", "juan", "5512345678", "mail.com", "t.me"):
            self.assertNotIn(needle, dump)

    async def test_learning_cannot_mutate_the_production_decision(self):
        self.sched(msg=1, regime="hard", reply_text=BAD); await self.drain()
        hook._seen.clear()
        with mock.patch.dict(os.environ, {C.FLAG: "true", "COPILOT_SHADOW_ENABLED": "true"}):
            self.sched(msg=2, regime="hard", reply_text=BAD); await self.drain()
        a, b = await self.rows(store.shadow_decisions)
        for k in ("production_action", "production_regime", "production_action_proxy", "production_response", "chosen_action",
                  "shadow_recommendation", "canonical_action", "production_propensity_source"):
            self.assertEqual(a[k], b[k], k)
        reply = BAD
        history = [{"role": "user", "content": "x"}]
        snap = json.dumps(history)
        with mock.patch.dict(os.environ, {C.FLAG: "true", "COPILOT_SHADOW_ENABLED": "true"}):
            hook._seen.clear(); self.sched(msg=3, reply_text=reply, history=history); await self.drain()
        self.assertEqual((reply, json.dumps(history)), (BAD, snap))

    async def test_critic_failure_never_loses_the_shadow_record(self):
        with mock.patch.dict(os.environ, {C.FLAG: "true", "COPILOT_SHADOW_ENABLED": "true"}), \
             mock.patch("copilot_learning.critic.critique", side_effect=RuntimeError("x")):
            self.sched(msg=1, reply_text=BAD); await self.drain()
        self.assertEqual(await self.count(), 1)

    async def test_pii_scan_of_all_learning_tables(self):
        pii = "contacto juan@mail.com tel +52 55 1234 5678 @juan_perez https://t.me/x1 ahora mismo regístrate"
        c = critique(response_text=pii, client_text="soy Juan 5512345678", stage="conversation", reply_classification=PRICE,
                     next_best_question=NBQ, decision_id="pd_pii", state_ref="st_1")
        await LS.save_critique(self.sf, c); await L.run_once(self.sf)
        dump = ""
        for t in (LS.critiques, LS.examples, LS.patterns, LS.improvements):
            dump += json.dumps([dict(r) for r in await self.rows(t)], default=str)
        for needle in ("juan", "5512345678", "mail.com", "t.me", "contacto", "soy Juan"):
            self.assertNotIn(needle, dump)


