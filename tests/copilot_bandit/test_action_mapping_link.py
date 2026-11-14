import asyncio
import os
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select

from copilot_bandit import contract as C
from copilot_bandit import research
from copilot_bandit.backend import LinUCBBackend
from copilot_bandit.features import DIM, encode, feature_vector, features_ref
from copilot_bandit.replay import samples_from_rows
from copilot_decisions.store import metadata as ledger_md, production_decisions
from copilot_shadow import hook, store
from copilot_uplift.action_mapping import MAPPING_VERSION
from copilot_uplift.actions import DEFAULT_REGISTRY
from tests.copilot_shadow.test_shadow import Base

ROOT = Path(__file__).resolve().parents[2]


def meta(regime=None, trigger=None, path="llm_reply"):
    return {"regime": regime, "trigger": trigger, "decision_path": path}


class TestMapProduction(unittest.TestCase):
    def test_routing_regimes_are_never_actions(self):
        for regime in ("casual", "agent", "retention", "hard"):
            m = research.map_production(meta(regime))
            self.assertEqual((m["canonical_action"], m["action_mapping_status"]), (None, "UNMAPPED"), regime)
            self.assertEqual(m["action_mapping_version"], MAPPING_VERSION)
            self.assertIn(C.PRODUCTION_ACTION_UNMAPPED, m["flags"])
            self.assertNotIn(C.INSUFFICIENT_METADATA, m["flags"])           # metadata was there; there is just no action

    def test_exact_mapped_actions_come_from_actions_v1(self):
        s = research.map_production(meta("casual", "safety"))
        self.assertEqual((s["canonical_action"], s["action_mapping_status"], s["flags"]), ("BUILD_TRUST", "MAPPED", []))
        f = research.map_production(meta(None, None, "followup"))
        self.assertEqual((f["canonical_action"], f["action_mapping_status"]), ("FOLLOW_UP", "MAPPED"))
        for m in (s, f):
            self.assertIn(m["canonical_action"], DEFAULT_REGISTRY)

    def test_mapped_but_not_sample_capable_is_flagged(self):
        m = research.map_production(meta(None, "fraud", "fixed_trigger"))
        self.assertEqual((m["canonical_action"], m["action_mapping_status"]), ("BUILD_TRUST", "MAPPED"))
        self.assertIn(C.ACTION_NOT_SAMPLE_CAPABLE, m["flags"])

    def test_ambiguous_has_no_canonical_action(self):
        m = research.map_production(meta("casual", "other_platform"))
        self.assertEqual((m["canonical_action"], m["action_mapping_status"]), (None, "AMBIGUOUS"))
        self.assertIn(C.PRODUCTION_ACTION_AMBIGUOUS, m["flags"]); self.assertNotIn(C.PRODUCTION_ACTION_UNMAPPED, m["flags"])

    def test_missing_or_unknown_metadata_is_safe_unmapped(self):
        for bad in (None, {}, {"regime": "casual"}, "x", {"decision_path": ""}):
            m = research.map_production(bad)
            self.assertEqual((m["canonical_action"], m["action_mapping_status"], m["action_mapping_reason"]),
                             (None, "UNMAPPED", "ledger_metadata_missing"))
            self.assertEqual(set(m["flags"]), {C.PRODUCTION_ACTION_UNMAPPED, C.INSUFFICIENT_METADATA})
        self.assertEqual(research.map_production(meta("casual", "weird"))["action_mapping_status"], "UNMAPPED")
        self.assertEqual(research.map_production(meta("x", None, "novel_path"))["action_mapping_status"], "UNMAPPED")

    def test_deterministic(self):
        self.assertEqual(research.map_production(meta("hard", "safety")), research.map_production(meta("hard", "safety")))


class TestEligibility(unittest.TestCase):
    def row(self, **k):
        r = {"action_mapping_status": "MAPPED", "canonical_action": "BUILD_TRUST", "bandit_action": "DO_NOTHING",
             "eligibility_flags": [], "production_propensity": None, "production_propensity_source": "unknown"}
        r.update(k)
        return r

    def test_comparison_only_on_mapped_canonical(self):
        self.assertTrue(C.comparison_eligible(self.row()))
        for k in ({"action_mapping_status": "UNMAPPED", "canonical_action": None}, {"action_mapping_status": "AMBIGUOUS", "canonical_action": None},
                  {"bandit_action": None}, {"production_action": "CONTINUE_DIALOGUE", "canonical_action": None, "action_mapping_status": "UNMAPPED"}):
            self.assertFalse(C.comparison_eligible(self.row(**k)), k)

    def test_unmapped_and_ambiguous_are_ope_ineligible(self):
        self.assertTrue(C.action_ope_eligible(self.row()))
        self.assertFalse(C.action_ope_eligible(self.row(action_mapping_status="UNMAPPED", canonical_action=None)))
        self.assertFalse(C.action_ope_eligible(self.row(action_mapping_status="AMBIGUOUS", canonical_action=None)))
        self.assertFalse(C.action_ope_eligible(self.row(eligibility_flags=[C.ACTION_NOT_SAMPLE_CAPABLE])))

    def test_mapped_still_needs_known_propensity(self):
        self.assertFalse(C.ope_eligible(self.row()))                                    # propensity unknown today
        self.assertTrue(C.ope_eligible(self.row(production_propensity=0.4, production_propensity_source="known")))
        self.assertFalse(C.ope_eligible(self.row(production_propensity=0.4, production_propensity_source="surrogate")))


class TestLive(Base):
    async def asyncSetUp(self):
        await Base.asyncSetUp(self)
        async with self.engine.begin() as c:
            await c.run_sync(ledger_md.create_all)

    async def ledger(self, did, **k):
        async with self.sf() as s:
            await s.execute(production_decisions.insert().values(decision_id=did, chat_id=111, metadata={}, **k)); await s.commit()

    async def rows(self):
        async with self.sf() as s:
            return (await s.execute(select(store.shadow_decisions).order_by(store.shadow_decisions.c.id))).mappings().all()

    async def test_continue_dialogue_casual_is_unmapped_and_raw_label_kept(self):
        await self.ledger("pd_1", regime="casual", decision_path="llm_reply")
        self.sched(msg=1, regime="casual", decision_id="pd_1"); await self.drain()
        (r,) = await self.rows()
        self.assertEqual(r["production_action"], "CONTINUE_DIALOGUE")                   # raw / internal label, kept for audit
        self.assertIsNone(r["canonical_action"])
        self.assertEqual((r["action_mapping_status"], r["action_mapping_version"]), ("UNMAPPED", MAPPING_VERSION))
        self.assertEqual(r["action_mapping_reason"], "regime_selects_no_distinct_behaviour")
        self.assertIn("PRODUCTION_ACTION_UNMAPPED", r["eligibility_flags"])
        self.assertNotIn("INSUFFICIENT_METADATA", r["eligibility_flags"])
        self.assertTrue(set(r["available_actions"]["ids"]) <= set(DEFAULT_REGISTRY.ids()))
        self.assertNotIn("CONTINUE_DIALOGUE", r["available_actions"]["ids"])

    async def test_mapped_canonical_differs_from_raw_production_action(self):
        await self.ledger("pd_2", regime="casual", trigger="safety", decision_path="llm_reply")
        self.sched(msg=1, regime="casual", decision_id="pd_2"); await self.drain()
        (r,) = await self.rows()
        self.assertEqual((r["canonical_action"], r["action_mapping_status"]), ("BUILD_TRUST", "MAPPED"))
        self.assertEqual(r["production_action"], "CONTINUE_DIALOGUE")                   # different vocabularies: that is fine
        self.assertIn(r["canonical_action"], r["available_actions"]["ids"])
        self.assertNotIn("PRODUCTION_ACTION_UNMAPPED", r["eligibility_flags"])

    async def test_ambiguous_row(self):
        await self.ledger("pd_3", regime="casual", trigger="other_platform", decision_path="llm_reply")
        self.sched(msg=1, decision_id="pd_3"); await self.drain()
        (r,) = await self.rows()
        self.assertEqual((r["canonical_action"], r["action_mapping_status"]), (None, "AMBIGUOUS"))
        self.assertIn("PRODUCTION_ACTION_AMBIGUOUS", r["eligibility_flags"])

    async def test_no_ledger_row_or_non_ledger_id_is_insufficient_metadata(self):
        hook.LEDGER_JOIN_WAIT_S = 0.01
        self.sched(msg=1, decision_id="pd_missing"); await self.drain()
        self.sched(msg=2, decision_id=None); await self.drain()
        for r in await self.rows():
            self.assertEqual((r["canonical_action"], r["action_mapping_status"], r["action_mapping_reason"]), (None, "UNMAPPED", "ledger_metadata_missing"))
            self.assertIn("INSUFFICIENT_METADATA", r["eligibility_flags"])
            self.assertIsNotNone(r["production_action"])                                  # record kept in the dataset

    async def test_ledger_table_missing_does_not_break_shadow(self):
        async with self.engine.begin() as c:
            await c.run_sync(ledger_md.drop_all)
        hook.LEDGER_JOIN_WAIT_S = 0.01
        self.sched(msg=1, decision_id="pd_x"); await self.drain()
        (r,) = await self.rows()
        self.assertEqual(r["action_mapping_status"], "UNMAPPED"); self.assertIsNotNone(r["lead_score"])

    async def test_ledger_lag_is_tolerated(self):
        hook.LEDGER_JOIN_WAIT_S = 0.05
        async def late():
            await asyncio.sleep(0.08); await self.ledger("pd_late", regime="casual", trigger="safety", decision_path="llm_reply")
        t = asyncio.create_task(late())
        self.sched(msg=1, decision_id="pd_late"); await self.drain(); await t
        (r,) = await self.rows()
        self.assertEqual(r["canonical_action"], "BUILD_TRUST")

    async def test_bandit_unmapped_keeps_infrastructure_and_blocks_comparison(self):
        await self.ledger("pd_4", regime="agent", decision_path="llm_reply")
        backend = LinUCBBackend(DEFAULT_REGISTRY.ids(), DIM, baseline="DO_NOTHING")
        real = research.v2_fields
        with mock.patch.dict(os.environ, {"COPILOT_BANDIT_SHADOW_ENABLED": "true", "COPILOT_SHADOW_ENABLED": "true"}), \
             mock.patch.object(research, "v2_fields", lambda **k: real(backend=backend, **k)):
            self.sched(msg=1, regime="agent", decision_id="pd_4"); await self.drain()
        (r,) = await self.rows()
        self.assertIn(r["bandit_action"], DEFAULT_REGISTRY.ids())                       # bandit speaks actions.v1
        self.assertFalse(C.comparison_eligible(dict(r))); self.assertFalse(C.ope_eligible(dict(r)))
        self.assertEqual(r["production_action"], "PRESENT_OFFER")                       # untouched by the bandit

    async def test_production_fields_identical_with_and_without_mapping_metadata(self):
        await self.ledger("pd_5", regime="hard", trigger="safety", decision_path="llm_reply")
        self.sched(msg=1, regime="hard", decision_id="pd_5"); await self.drain()
        self.sched(msg=2, regime="hard"); await self.drain()
        a, b = await self.rows()
        for k in ("production_action", "production_regime", "production_action_proxy", "production_response", "chosen_action"):
            self.assertEqual(a[k], b[k], k)
        self.assertEqual(a["production_action"], "HANDLE_OBJECTION")


