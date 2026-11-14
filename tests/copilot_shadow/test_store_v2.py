import ast
import os
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select, text

from copilot_bandit import research
from copilot_bandit.backend import LinUCBBackend
from copilot_bandit.features import DIM, encode, feature_vector, features_ref
from copilot_bandit.replay import samples_from_rows
from copilot_shadow import hook, store
from copilot_uplift.actions import DEFAULT_REGISTRY
from tests.copilot_shadow.test_shadow import Base, make_cs, make_user

ROOT = Path(__file__).resolve().parents[2]
V2_COLS = ("feature_version", "features_ref", "available_actions", "production_policy_version", "production_propensity",
           "production_propensity_source", "bandit_action", "bandit_policy_version", "bandit_propensity", "uplift_score",
           "uplift_version", "reward", "reward_version", "reward_reason", "observation_window", "eligibility_flags")


class TestStoreV2(Base):
    async def rows(self):
        async with self.sf() as s:
            return (await s.execute(select(store.shadow_decisions).order_by(store.shadow_decisions.c.id))).mappings().all()

    async def test_fields_written_with_bandit_off_by_default(self):
        self.assertTrue(self.sched(msg=1, regime="casual")); await self.drain()
        (r,) = await self.rows()
        self.assertEqual(r["feature_version"], "features.v1")
        self.assertTrue(r["features_ref"].startswith("fx_"))
        self.assertEqual(r["available_actions"], {"registry_version": DEFAULT_REGISTRY.version, "ids": list(DEFAULT_REGISTRY.ids())})
        self.assertIsNone(r["production_propensity"]); self.assertEqual(r["production_propensity_source"], "unknown")
        self.assertEqual(r["production_policy_version"], "production-routing-unversioned")
        self.assertEqual(r["observation_window"], 86400)
        self.assertIn("PROPENSITY_UNKNOWN", r["eligibility_flags"])
        self.assertIn("PRODUCTION_ACTION_UNMAPPED", r["eligibility_flags"])      # casual has no registry action
        for c in ("bandit_action", "bandit_policy_version", "bandit_propensity", "uplift_score", "uplift_version",
                  "reward", "reward_version"):
            self.assertIsNone(r[c], c)
        self.assertEqual(r["contract_version"], "shadow-contract-4")

    async def test_bandit_shadow_records_recommendation_but_never_replaces_production(self):
        backend = LinUCBBackend(DEFAULT_REGISTRY.ids(), DIM, baseline="DO_NOTHING")
        real = research.v2_fields
        with mock.patch.dict(os.environ, {"COPILOT_BANDIT_SHADOW_ENABLED": "true", "COPILOT_SHADOW_ENABLED": "true"}), \
             mock.patch.object(research, "v2_fields", lambda **k: real(backend=backend, **k)):
            self.sched(msg=1, regime="hard"); await self.drain()
        (r,) = await self.rows()
        self.assertEqual(r["bandit_action"], "DO_NOTHING")                      # cold-start baseline
        self.assertEqual(r["bandit_policy_version"], "linucb-1")
        self.assertEqual(r["bandit_propensity"], 1.0)
        self.assertIsNone(r["production_propensity"])                           # bandit propensity is a separate thing
        self.assertEqual(r["production_action"], "HANDLE_OBJECTION")            # production proxy untouched
        self.assertEqual(r["production_response"], "RESPONDED")
        self.assertIn("INSUFFICIENT_DATA", r["eligibility_flags"])
        self.assertNotEqual(r["bandit_action"], r["production_action"])

    async def test_production_action_identical_with_and_without_bandit(self):
        self.sched(msg=1, regime="agent"); await self.drain()
        with mock.patch.dict(os.environ, {"COPILOT_BANDIT_SHADOW_ENABLED": "true", "COPILOT_SHADOW_ENABLED": "true"}):
            self.sched(msg=2, regime="agent"); await self.drain()
        a, b = await self.rows()
        for k in ("production_action", "production_regime", "production_action_proxy", "production_response", "chosen_action"):
            self.assertEqual(a[k], b[k], k)

    async def test_safety_restricts_available_actions(self):
        self.sched(msg=1, user=make_user(manual_mode=True)); await self.drain()
        (r,) = await self.rows()
        self.assertEqual(r["available_actions"]["ids"], ["HUMAN_HANDOFF"])
        self.assertIn("SAFETY_RESTRICTED", r["eligibility_flags"])

    async def test_dedup_and_restart(self):
        self.sched(msg=1); await self.drain()
        hook._seen.clear(); store._ready.clear()
        self.sched(msg=1); await self.drain()
        self.assertEqual(await self.count(), 1)

    async def test_legacy_table_migration_adds_nullable_v2_columns(self):
        async with self.engine.begin() as c:
            await c.execute(text("CREATE TABLE shadow_decisions (id INTEGER PRIMARY KEY, decision_id VARCHAR(40) NOT NULL UNIQUE,"
                                 " chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL, created_at TIMESTAMP, state_snapshot JSON NOT NULL,"
                                 " candidate_actions JSON NOT NULL, chosen_action VARCHAR(32) NOT NULL, chosen_rule VARCHAR(128),"
                                 " wait_baseline JSON NOT NULL, decision_status VARCHAR(24) NOT NULL, shadow_mode BOOLEAN NOT NULL,"
                                 " model_name VARCHAR(64) NOT NULL, model_version VARCHAR(32) NOT NULL, analyzer_version VARCHAR(32) NOT NULL,"
                                 " code_version VARCHAR(64) NOT NULL, production_regime VARCHAR(16), production_action_proxy VARCHAR(32),"
                                 " reply_chars INTEGER, contradiction JSON, momentum JSON, next_best_question JSON, deposit_path JSON)"))
            await c.execute(text("INSERT INTO shadow_decisions (decision_id, chat_id, message_id, state_snapshot, candidate_actions,"
                                 " chosen_action, wait_baseline, decision_status, shadow_mode, model_name, model_version,"
                                 " analyzer_version, code_version) VALUES ('old',1,1,'{}','[]','WAIT','{}','x',1,'m','1','a','c')"))
        await store.ensure_table(self.sf)
        (r,) = await self.rows()
        for c in V2_COLS:
            self.assertIn(c, r.keys()); self.assertIsNone(r[c], c)
        self.assertEqual(r["decision_id"], "old")

    async def test_set_reward_is_versioned_and_set_once(self):
        self.sched(msg=1); await self.drain()
        did = (await self.rows())[0]["decision_id"]
        self.assertTrue(await store.set_reward(self.sf, did, None, "reward.v0-undefined", "continued_is_not_a_conversion"))
        self.assertFalse(await store.set_reward(self.sf, did, 1.0, "reward.v1-test", "x"))      # never overwritten
        (r,) = await self.rows()
        self.assertEqual((r["reward"], r["reward_version"], r["reward_reason"]),
                         (None, "reward.v0-undefined", "continued_is_not_a_conversion"))
        with self.assertRaises(ValueError):
            await store.set_reward(self.sf, did, 2.0, "v")
        self.assertFalse(await store.set_reward(self.sf, "missing", 0.5, "v"))


class TestIsolation(unittest.TestCase):
    def test_flags_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            from copilot_bandit import contract as C
            self.assertEqual((C.bandit_shadow_enabled(), C.bandit_enabled(), C.ope_enabled()), (False, False, False))

    def test_bandit_package_has_no_network_llm_or_shadow_import(self):
        for f in (ROOT / "copilot_bandit").glob("*.py"):
            for n in ast.walk(ast.parse(f.read_text())):
                mods = ([a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
                for m in mods:
                    self.assertNotIn(m.split(".")[0], {"main", "telethon", "requests", "httpx", "openai", "anthropic", "random",
                                                       "copilot_shadow", "copilot_decisions", "sqlalchemy", "socket"}, f"{f.name}:{m}")

    def test_features_reuse_uplift_features_v1(self):
        snap = {"stage": "offer", "intent": "ready", "sentiment": 0.7, "region": "MX", "messages_count": 4}
        fv = feature_vector("d1", 5, snap)
        self.assertEqual(fv.feature_version, "features.v1")
        self.assertEqual(features_ref(fv), features_ref(feature_vector("d2", 6, snap)))      # no ids in the reference
        self.assertNotEqual(features_ref(fv), features_ref(feature_vector("d1", 5, {**snap, "intent": "question"})))
        self.assertEqual(len(encode(fv)), DIM)
        self.assertEqual(len(encode(feature_vector("d", 1, None))), DIM)                     # malformed state -> zeros, no crash

    def test_replay_only_trustworthy_rows(self):
        snap = {"stage": "objection", "intent": "objection", "sentiment": 0.3, "region": None, "messages_count": 3}
        ref = features_ref(feature_vector("d1", 5, snap))
        good = {"decision_id": "d1", "chat_id": 5, "state_snapshot": snap, "features_ref": ref, "reward": 1.0,
                "canonical_action": "ADDRESS_OBJECTION", "action_mapping_status": "MAPPED"}
        rows = [good, {**good, "reward": None}, {**good, "canonical_action": None, "action_mapping_status": "UNMAPPED"},
                {**good, "action_mapping_status": "AMBIGUOUS"}, {**good, "features_ref": "fx_bad"},
                {**good, "state_snapshot": None}, {"decision_id": "x"}]
        out = list(samples_from_rows(rows))
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0][1], out[0][2]), ("ADDRESS_OBJECTION", 1.0))
        b = LinUCBBackend(DEFAULT_REGISTRY.ids(), DIM)
        self.assertEqual(b.fit(out), 1)
