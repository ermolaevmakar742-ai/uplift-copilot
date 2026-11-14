import ast
import dataclasses
import inspect
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from copilot_attribution.rules_v2 import BoundaryRef
from copilot_outcomes.contracts import Outcome
from copilot_uplift import action_mapping as AM
from copilot_uplift.action_mapping import (LEDGER, MAPPING_VERSION, MAPPING_VERSION_V1, RULES_V2, MappingStatus as MS, map_decision,
                                        map_event, summary, validate_rules)
from copilot_uplift.actions import DEFAULT_REGISTRY, Action, ActionRegistry
from copilot_uplift.evaluator import UpliftData, evaluate_uplift
from copilot_uplift.policy import DEFAULT_POLICY, LOCKED_FINGERPRINTS, POLICY_V1, POLICY_V2, PolicyError, UpliftPolicy, is_locked

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
SEC, MIN, H = timedelta(seconds=1), timedelta(minutes=1), timedelta(hours=1)


class DecisionMappingTests(unittest.TestCase):
    def test_safety_trigger_is_mapped_to_build_trust(self):
        r = map_decision("casual", "safety")
        self.assertEqual((r.status, r.action_id, r.behaviour_id, r.sample_capable, r.unit), (MS.MAPPED, "BUILD_TRUST", "llm_reply_safety_suffix", True, LEDGER))
        self.assertEqual(map_decision(" CASUAL ", " Safety ").action_id, "BUILD_TRUST")          # normalised, still deterministic

    def test_trigger_takes_precedence_over_the_regime_label(self):
        for regime in ("hard", "agent", "casual", "retention", None, "weird"):
            self.assertEqual(map_decision(regime, "safety").action_id, "BUILD_TRUST", regime)
            self.assertEqual(map_decision(regime, "other_platform").status, MS.AMBIGUOUS, regime)

    def test_other_platform_is_ambiguous_and_no_candidate_is_chosen(self):
        r = map_decision("casual", "other_platform")
        self.assertEqual((r.status, r.action_id, r.candidates, r.sample_capable, r.reason), (MS.AMBIGUOUS, None, ("ASK_QUESTION", "ADDRESS_OBJECTION"), False, "ambiguous_ask_or_objection"))

    def test_no_regime_label_is_an_action(self):
        # the audit found regimes select no distinct prompt/model: none may map to an action (this forbids the old hard -> ADDRESS_OBJECTION)
        for regime in ("hard", "agent", "casual", "retention"):
            r = map_decision(regime, None)
            self.assertEqual((r.status, r.action_id, r.reason, r.sample_capable), (MS.UNMAPPED, None, "regime_selects_no_distinct_behaviour", False), regime)
            self.assertEqual(map_decision(regime, "").status, MS.UNMAPPED)
        for regime in (None, "", "   ", "analyst", "weird", 5):
            self.assertEqual(map_decision(regime, None).action_id, None)
        self.assertEqual(map_decision(None, None).reason, "regime_unknown")
        self.assertEqual(map_decision("weird", None).reason, "regime_not_in_mapping:weird")

    def test_fixed_and_unknown_triggers_are_never_llm_actions(self):
        for t in ("fraud", "driving", "working", "how_much", "why_me", "loan"):
            r = map_decision("casual", t)
            self.assertEqual((r.status, r.reason), (MS.UNMAPPED, "fixed_trigger_is_not_an_llm_decision"), t)
        self.assertEqual(map_decision("casual", "brand_new_trigger").reason, "trigger_not_in_mapping:brand_new_trigger")

    def test_legacy_v1_is_reproducible_but_not_the_default(self):
        legacy = map_decision("hard", None, version=MAPPING_VERSION_V1)
        self.assertEqual((legacy.status, legacy.action_id), (MS.MAPPED, "ADDRESS_OBJECTION"))
        self.assertEqual(map_decision("casual", None, version=MAPPING_VERSION_V1).status, MS.UNMAPPED)
        self.assertEqual(map_decision("hard", None).status, MS.UNMAPPED)                          # default (v2) refuses it
        self.assertEqual(MAPPING_VERSION, "action-mapping.v2")
        self.assertEqual(map_decision("hard", "safety", version="action-mapping.v9").reason, "unsupported_mapping_version")

    def test_mapped_action_must_exist_in_the_registry(self):
        small = ActionRegistry([Action("ASK_QUESTION", "d")])
        r = map_decision("casual", "safety", registry=small)
        self.assertEqual((r.status, r.reason), (MS.UNMAPPED, "action_not_in_registry:BUILD_TRUST"))

    def test_mapping_functions_take_no_message_text_and_never_raise(self):
        self.assertEqual(list(inspect.signature(map_decision).parameters)[:2], ["regime", "trigger"])
        for junk in (None, 5, [], {}, object(), "x" * 10000):
            map_decision(junk, junk)
            map_event(junk, junk)


class EventMappingTests(unittest.TestCase):
    def test_event_level_mappings_are_never_samples(self):
        cases = {("followup_sent", None): ("FOLLOW_UP", "followup_worker_fu"), ("fixed_trigger", "fraud"): ("BUILD_TRUST", "fixed_fraud_license")}
        for (e, n), (a, b) in cases.items():
            r = map_event(e, n)
            self.assertEqual((r.status, r.action_id, r.behaviour_id, r.sample_capable, r.unit), (MS.MAPPED, a, b, False, "event"))
        for n in ("how_much", "why_me"):
            r = map_event("fixed_trigger", n)
            self.assertEqual((r.status, r.action_id, r.candidates), (MS.AMBIGUOUS, None, ("BUILD_TRUST", "ADDRESS_OBJECTION")))
        for e, n in (("fixed_trigger", "loan"), ("fixed_trigger", "driving"), ("fixed_trigger", "working"), ("insult_block", None),
                     ("manual_mode", None), ("a3", None), ("ai_call", None), ("fixed_trigger", "new"), (None, None)):
            r = map_event(e, n)
            self.assertEqual((r.status, r.action_id, r.sample_capable), (MS.UNMAPPED, None, False), (e, n))
        self.assertEqual(map_event("fixed_trigger", "driving").reason, "deferral_message_not_do_nothing")     # a deferral is not DO_NOTHING

    def test_same_action_different_behaviours_are_kept_apart(self):
        a, b = map_decision("casual", "safety"), map_event("fixed_trigger", "fraud")
        self.assertEqual((a.action_id, b.action_id), ("BUILD_TRUST", "BUILD_TRUST"))
        self.assertNotEqual(a.behaviour_id, b.behaviour_id)
        self.assertEqual((a.sample_capable, b.sample_capable), (True, False))                       # only one behaviour may be a sample


class RuleSetTests(unittest.TestCase):
    def test_inventory(self):
        self.assertEqual(summary(), {"MAPPED": 6, "AMBIGUOUS": 5, "UNMAPPED": 13})
        mapped = {r.action_id for r in RULES_V2 if r.status is MS.MAPPED}
        sample = {r.action_id for r in RULES_V2 if r.status is MS.MAPPED and r.sample_capable}
        self.assertEqual((mapped, sample), ({"BUILD_TRUST", "FOLLOW_UP", "DO_NOTHING"}, {"BUILD_TRUST", "FOLLOW_UP", "DO_NOTHING"}))
        sample_behaviours = {r.behaviour_id for r in RULES_V2 if r.status is MS.MAPPED and r.sample_capable}
        self.assertEqual(sample_behaviours, {"llm_reply_safety_suffix", "followup_worker_fu", "followup_holdout_silence"})        # one behaviour per sample action
        never = set(DEFAULT_REGISTRY.ids()) - mapped
        self.assertEqual(never, {"ASK_QUESTION", "ADDRESS_OBJECTION", "CLARIFY_PAYMENT", "REGISTRATION_GUIDANCE", "DEPOSIT_GUIDANCE", "HUMAN_HANDOFF"})
        for r in RULES_V2:
            if r.status in (MS.MAPPED, MS.AMBIGUOUS):
                self.assertTrue(r.evidence.strip(), r.key)                                          # every positive claim cites production code

    def test_validate_rules_rejects_false_merges(self):
        validate_rules()
        a = AM._m(LEDGER, "trigger:safety", "BUILD_TRUST", "behaviour_a", "e")
        b = AM._m(LEDGER, "trigger:other_platform", "BUILD_TRUST", "behaviour_b", "e")
        with self.assertRaises(ValueError):
            validate_rules((a, b))                                                                   # two behaviours, one action: refused
        validate_rules((a, AM._m(LEDGER, "trigger:x", "BUILD_TRUST", "behaviour_a", "e")))           # the same behaviour twice is fine
        with self.assertRaises(ValueError):
            validate_rules((AM._m(LEDGER, "k", "NOT_REGISTERED", "b", "e"),))
        with self.assertRaises(ValueError):
            validate_rules((a, a))
        with self.assertRaises(ValueError):
            AM.MappingResult(MS.MAPPED, None, None, (), "x", LEDGER, True)
        with self.assertRaises(ValueError):
            AM.MappingResult(MS.AMBIGUOUS, None, None, ("ASK_QUESTION",), "x", LEDGER, False)

    def test_module_is_pure(self):
        src = (ROOT / "copilot_uplift" / "action_mapping.py").read_text(encoding="utf-8")
        for n in ast.walk(ast.parse(src)):
            mods = ([a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
            for m in mods:
                self.assertNotIn(m.split(".")[0], {"main", "sqlalchemy", "asyncio", "os", "random", "requests", "httpx", "openai", "anthropic",
                                                   "telethon", "fastapi", "copilot_decisions", "copilot_shadow", "copilot_external", "time", "datetime"}, m)


class PolicyVersioningTests(unittest.TestCase):
    def test_v2_is_the_default_and_both_are_locked_and_distinct(self):
        self.assertIs(DEFAULT_POLICY, POLICY_V2)
        self.assertEqual((POLICY_V2.policy_version, POLICY_V2.mapping_version, POLICY_V1.mapping_version),
                         ("uplift-policy.v2", "action-mapping.v2", "action-mapping.v1"))
        self.assertTrue(is_locked(POLICY_V1) and is_locked(POLICY_V2))
        self.assertNotEqual(POLICY_V1.fingerprint(), POLICY_V2.fingerprint())
        self.assertEqual(set(LOCKED_FINGERPRINTS), {"uplift-policy.v1", "uplift-policy.v2"})
        self.assertFalse(is_locked(dataclasses.replace(POLICY_V2, mapping_version="action-mapping.v1")))     # changing the mapping breaks the lock
        with self.assertRaises(PolicyError):
            dataclasses.replace(POLICY_V2, mapping_version="action-mapping.v9")


def dec(i, trigger, regime="casual", chat=None):
    return {"decision_id": f"pd_{i:03d}", "chat_id": chat or 500 + i, "created_at": T0 + i * MIN, "regime": regime, "trigger": trigger,
            "state": {"stage": "new", "intent": "question", "sentiment": 0.7, "region": "MX", "messages_count": 3}}


def data(decs, with_outcome_for=()):
    bs = [BoundaryRef(f"rb_{d['decision_id']}", d["decision_id"], d["chat_id"], d["created_at"] + 14 * SEC, "SENT", True, 0, 1) for d in decs]
    outs = [Outcome(outcome_id=f"o_{d['decision_id']}", decision_id=None, chat_id=d["chat_id"], observed_at=d["created_at"] + 10 * MIN,
                    outcome_type="CONTINUED", value=None, confidence=None, source="TELEGRAM_EVENT", metadata={})
            for d in decs if d["decision_id"] in with_outcome_for]
    evidence = Outcome(outcome_id="o_capture_evidence", decision_id=None, chat_id=9999, observed_at=T0 - H, outcome_type="CONTINUED", value=None,
                       confidence=None, source="TELEGRAM_EVENT", metadata={})        # shows the outcome pipeline was already running
    return UpliftData(tuple(decs), tuple(bs), tuple([evidence] + outs), ())


class EvaluatorUsesTheMappingTests(unittest.TestCase):
    NOW = T0 + 72 * H

    def run_eval(self, decs, policy=POLICY_V2, outs=("pd_001",)):
        return evaluate_uplift(data(decs, outs), policy, evaluated_at=self.NOW)

    def test_only_mapped_sample_capable_actions_become_samples(self):
        decs = [dec(1, "safety"), dec(2, "safety"), dec(3, "other_platform"), dec(4, None, "hard"), dec(5, None, "agent"), dec(6, "fraud"), dec(7, None, "casual")]
        e = self.run_eval(decs)
        self.assertEqual({s.action_id: s.eligible for s in e.conversion_by_action}, {"BUILD_TRUST": 2})
        self.assertEqual(e.metrics["actions"], 1)
        ms = e.missing_signals
        self.assertEqual(ms["decisions_with_unmapped_action"], 5)
        self.assertEqual(ms["decisions_action_ambiguous:ambiguous_ask_or_objection"], 1)
        self.assertEqual(ms["decisions_action_unmapped:regime_selects_no_distinct_behaviour"], 3)
        self.assertEqual(ms["decisions_action_unmapped:fixed_trigger_is_not_an_llm_decision"], 1)

    def test_regimes_alone_yield_no_samples_under_v2_but_do_under_legacy_v1(self):
        decs = [dec(i, None, "hard") for i in range(1, 6)]
        self.assertEqual(self.run_eval(decs).metrics["eligible_samples"], 0)
        legacy = self.run_eval(decs, POLICY_V1)
        self.assertEqual(legacy.metrics["eligible_samples"], 5)                                      # reproducible for old evaluations
        self.assertNotEqual(legacy.dataset_version, self.run_eval(decs).dataset_version)             # and the mapping version is auditable
        self.assertEqual(legacy.policy_version, "uplift-policy.v1")

    def test_ambiguous_decisions_do_not_pollute_estimates(self):
        e = self.run_eval([dec(i, "other_platform") for i in range(1, 8)])
        self.assertEqual((e.metrics["eligible_samples"], e.conversion_by_action, e.uplift_estimates), (0, (), ()))

    def test_variation_gate_needs_real_observed_actions(self):
        from copilot_uplift.gates import compute_gates, observed_actions
        decs = [dec(i, "safety") for i in range(1, 40)]
        e = self.run_eval(decs)
        self.assertEqual(observed_actions(e), ("BUILD_TRUST",))
        r = compute_gates(e, POLICY_V2, activation_storage_ok=True)
        self.assertIn("action_variation", r.missing)
        rules = tuple(AM._m(LEDGER, "trigger:other_platform", "ASK_QUESTION", "test_only_behaviour", "t") if x.key == "trigger:other_platform" else x for x in RULES_V2)
        with mock.patch.object(AM, "RULES_V2", rules):                                               # a second sample-capable behaviour
            mixed = self.run_eval([dec(i, "safety" if i % 2 else "other_platform") for i in range(1, 41)])
        self.assertEqual(observed_actions(mixed), ("ASK_QUESTION", "BUILD_TRUST"))
        self.assertNotIn("action_variation", compute_gates(mixed, POLICY_V2, activation_storage_ok=True).missing)


if __name__ == "__main__":
    unittest.main()
