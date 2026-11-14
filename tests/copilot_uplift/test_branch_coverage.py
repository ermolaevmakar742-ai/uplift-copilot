import ast
import dataclasses
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from copilot_attribution.contract import Interruption
from copilot_attribution.rules_v2 import BoundaryRef
from copilot_outcomes.contracts import Outcome
from copilot_uplift import action_mapping as AM
from copilot_uplift.action_mapping import MappingStatus as MS, map_decision, rules_fingerprint
from copilot_uplift.evaluator import UpliftData, evaluate_uplift
from copilot_uplift.experiments import audit_flow_experiment
from copilot_uplift.gates import compute_gates, observed_actions
from copilot_uplift.policy import POLICY_V1, POLICY_V2

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
SEC, MIN, H = timedelta(seconds=1), timedelta(minutes=1), timedelta(hours=1)
NOW = T0 + 100 * H


def dec(i, path="llm_reply", trigger=None, regime="casual", chat=None, with_boundary=True):
    d = {"decision_id": f"pd_{i:03d}", "chat_id": chat or 800 + i, "created_at": T0 + i * MIN, "regime": regime, "trigger": trigger,
         "decision_path": path, "state": None if path == "followup" else {"stage": "new", "intent": "question", "sentiment": 0.7, "region": "MX", "messages_count": 3}}
    return d, (BoundaryRef(f"rb_{i:03d}", d["decision_id"], d["chat_id"], d["created_at"] + 14 * SEC, "SENT", True) if with_boundary else None)


def data(*pairs, ints=()):
    decs = [p[0] for p in pairs]
    bs = [p[1] for p in pairs if p[1] is not None]
    evidence = Outcome(outcome_id="o_evidence", decision_id=None, chat_id=9999, observed_at=T0 - H, outcome_type="CONTINUED", value=None,
                       confidence=None, source="TELEGRAM_EVENT", metadata={})
    return UpliftData(tuple(decs), tuple(bs), (evidence,), tuple(ints))


class BranchMappingTests(unittest.TestCase):
    def test_followup_is_a_sample_capable_action(self):
        r = map_decision(None, None, decision_path="followup")
        self.assertEqual((r.status, r.action_id, r.behaviour_id, r.sample_capable), (MS.MAPPED, "FOLLOW_UP", "followup_worker_fu", True))
        self.assertEqual(map_decision("hard", "safety", decision_path=" Followup ").action_id, "FOLLOW_UP")      # the branch decides, not regime / trigger

    def test_fixed_fraud_is_mapped_but_not_a_sample(self):
        r = map_decision(None, "fraud", decision_path="fixed_trigger")
        self.assertEqual((r.status, r.action_id, r.behaviour_id, r.sample_capable, r.reason),
                         (MS.MAPPED, "BUILD_TRUST", "fixed_fraud_license", False, "mapped_not_sample_capable:second_behaviour_for_same_action"))
        self.assertNotEqual(r.behaviour_id, map_decision("casual", "safety").behaviour_id)                      # different mechanism: never one arm

    def test_other_fixed_triggers(self):
        for name in ("how_much", "why_me"):
            r = map_decision(None, name, decision_path="fixed_trigger")
            self.assertEqual((r.status, r.action_id, r.candidates, r.sample_capable), (MS.AMBIGUOUS, None, ("BUILD_TRUST", "ADDRESS_OBJECTION"), False))
        for name in ("loan", "driving", "working"):
            r = map_decision(None, name, decision_path="fixed_trigger")
            self.assertEqual((r.status, r.action_id), (MS.UNMAPPED, None), name)
        self.assertEqual(map_decision(None, "driving", decision_path="fixed_trigger").reason, "deferral_message_not_do_nothing")
        self.assertEqual(map_decision(None, None, decision_path="fixed_trigger").reason, "fixed_trigger_name_unknown")
        self.assertEqual(map_decision(None, "brand_new", decision_path="fixed_trigger").reason, "fixed_trigger_not_in_mapping:brand_new")

    def test_unknown_paths_and_unchanged_llm_path(self):
        for path in ("insult_block", "manual", "human_handoff", "a3", "weird", "followup_fu1"):
            r = map_decision("casual", "safety", decision_path=path)
            self.assertEqual((r.status, r.action_id, r.reason), (MS.UNMAPPED, None, "decision_path_not_in_mapping:" + path), path)   # never guessed
        self.assertEqual(map_decision("casual", "safety").action_id, "BUILD_TRUST")
        self.assertEqual(map_decision("casual", "safety", decision_path="llm_reply").action_id, "BUILD_TRUST")
        self.assertEqual(map_decision("hard", None).status, MS.UNMAPPED)                                        # regime is still not an action
        self.assertEqual(map_decision("hard", None, version=AM.MAPPING_VERSION_V1).action_id, "ADDRESS_OBJECTION")   # legacy only for old data

    def test_human_handoff_has_no_mapping_without_a_real_event(self):
        self.assertFalse([r for r in AM.RULES_V2 if r.action_id == "HUMAN_HANDOFF" or "HUMAN_HANDOFF" in r.candidates])
        self.assertEqual(AM.map_event("manual_mode").status, MS.UNMAPPED)
        self.assertEqual(map_decision("manual", None, decision_path="manual").status, MS.UNMAPPED)

    def test_rules_fingerprint_makes_rule_changes_visible_with_the_same_version(self):
        fp = rules_fingerprint()
        self.assertEqual(fp, rules_fingerprint())
        changed = tuple(dataclasses.replace(r, sample_capable=False) if r.key == "path:followup" else r for r in AM.RULES_V2)
        with mock.patch.object(AM, "RULES_V2", changed):
            self.assertNotEqual(rules_fingerprint(), fp)
        self.assertNotEqual(rules_fingerprint(AM.MAPPING_VERSION_V1), fp)
        AM.validate_rules()

    def test_registry_and_mapping_version_untouched(self):
        self.assertEqual(AM.MAPPING_VERSION, "action-mapping.v2")
        self.assertEqual(POLICY_V2.mapping_version, "action-mapping.v2")
        self.assertEqual(AM.summary(), {"MAPPED": 6, "AMBIGUOUS": 5, "UNMAPPED": 13})


class EvaluatorBranchTests(unittest.TestCase):
    def ev(self, *pairs, ints=(), policy=POLICY_V2):
        return evaluate_uplift(data(*pairs, ints=ints), policy, evaluated_at=NOW)

    def test_followup_decision_with_boundary_is_an_eligible_sample(self):
        e = self.ev(dec(1, "followup"), dec(2, "followup"))
        self.assertEqual({s.action_id: s.eligible for s in e.conversion_by_action}, {"FOLLOW_UP": 2})
        self.assertEqual((e.metrics["eligible_samples"], e.metrics["actions"]), (2, 1))

    def test_followup_without_boundary_is_not_a_sample(self):
        e = self.ev(dec(1, "followup", with_boundary=False))
        self.assertEqual(e.metrics["eligible_samples"], 0)
        st = e.conversion_by_action[0]
        self.assertEqual((st.action_id, st.eligible, st.unknown), ("FOLLOW_UP", 0, 1))           # reported as unknown, never as a sample
        self.assertEqual(e.missing_signals["decisions_observation_unknown:no_sent_response_boundary"], 1)

    def test_historical_events_without_decisions_are_never_samples(self):
        e = self.ev(ints=[Interruption("FOLLOWUP", 5, T0 + i * MIN) for i in range(13)] + [Interruption("FIXED_TRIGGER", 5, T0)])
        self.assertEqual((e.metrics["decisions"], e.metrics["eligible_samples"], e.metrics["actions"], e.conversion_by_action), (0, 0, 0, ()))

    def test_fixed_branch_decisions_are_recorded_but_only_unambiguous_sample_actions_count(self):
        e = self.ev(dec(1, "fixed_trigger", "fraud"), dec(2, "fixed_trigger", "how_much"), dec(3, "fixed_trigger", "loan"), dec(4, "fixed_trigger", None))
        self.assertEqual((e.metrics["decisions"], e.metrics["eligible_samples"], e.conversion_by_action), (4, 0, ()))
        ms = e.missing_signals
        self.assertEqual(ms["decisions_mapped_not_sample_capable:fixed_fraud_license"], 1)
        self.assertEqual(ms["decisions_action_ambiguous:ambiguous_trust_or_objection_expectation"], 1)
        self.assertEqual(ms["decisions_action_unmapped:declines_loan_not_a_registry_action"], 1)
        self.assertEqual(ms["decisions_action_unmapped:fixed_trigger_name_unknown"], 1)
        self.assertEqual(ms["decisions_with_unmapped_action"], 4)

    def test_variation_needs_two_real_sample_capable_actions_with_the_shipped_mapping(self):
        only_llm = self.ev(*[dec(i, "llm_reply", "safety") for i in range(1, 6)])
        self.assertEqual(observed_actions(only_llm), ("BUILD_TRUST",))
        self.assertIn("action_variation", compute_gates(only_llm, POLICY_V2, activation_storage_ok=True).missing)
        mixed = self.ev(*[dec(i, "llm_reply", "safety") for i in range(1, 4)], *[dec(i, "followup") for i in range(4, 7)])
        self.assertEqual(observed_actions(mixed), ("BUILD_TRUST", "FOLLOW_UP"))                  # no patched rules: real mapping, real behaviours
        self.assertNotIn("action_variation", compute_gates(mixed, POLICY_V2, activation_storage_ok=True).missing)
        regimes_only = self.ev(*[dec(i, "llm_reply", None, r) for i, r in enumerate(("hard", "agent", "casual", "retention"), start=1)])
        self.assertEqual(observed_actions(regimes_only), ())                                      # the regime label never creates variation

    def test_dataset_version_reflects_the_rule_set(self):
        pairs = [dec(1, "followup")]
        base = self.ev(*pairs).dataset_version
        changed = tuple(dataclasses.replace(r, sample_capable=False) if r.key == "path:followup" else r for r in AM.RULES_V2)
        with mock.patch.object(AM, "RULES_V2", changed):
            self.assertNotEqual(self.ev(*pairs).dataset_version, base)

    def test_legacy_policy_ignores_branch_decisions(self):
        e = self.ev(dec(1, "followup"), policy=POLICY_V1)
        self.assertEqual(e.metrics["eligible_samples"], 0)                                        # v1 maps only the llm regime label


class ExperimentAuditTests(unittest.TestCase):
    def test_different_arms_still_need_recorded_assignment(self):
        a = audit_flow_experiment({"a": {"prompt_suffix": ""}, "b": {"prompt_suffix": "extra"}})
        self.assertEqual((a.status, a.causal, a.control), ("DIFFERENT_ARMS_UNRECORDED_ASSIGNMENT", False, "CONTROL_UNAVAILABLE"))
        for bad in ({}, {"only": {}}, None, "x", []):
            r = audit_flow_experiment(bad)
            self.assertEqual((r.status, r.causal), ("NO_ARMS", False))

    def test_audit_is_pure(self):
        src = (ROOT / "copilot_uplift" / "experiments.py").read_text(encoding="utf-8")
        for w in ("import random", "import main", "datetime", "uuid", "requests"):
            self.assertNotIn(w, src)


if __name__ == "__main__":
    unittest.main()


class CoverageAuditTests(unittest.TestCase):
    def audit(self, *pairs, flows=None, outcomes=()):
        d = data(*pairs)
        d = dataclasses.replace(d, outcomes=d.outcomes + tuple(outcomes))
        from copilot_uplift.audit import coverage_audit
        ev = evaluate_uplift(d, POLICY_V2, evaluated_at=NOW)
        return coverage_audit(d, ev, flows)

    def test_empty_dataset(self):
        a = self.audit(flows={"v37": {"prompt_suffix": ""}, "v38": {"prompt_suffix": ""}})
        self.assertEqual(a["decision_coverage"]["total"], 0)
        self.assertEqual((a["observed_mapped_actions"], a["action_variation"]["status"], a["causal_readiness"]["status"]), ([], "FAIL", "NOT_CAUSAL_READY"))
        self.assertIsNone(a["boundary_coverage"]["pct"])
        self.assertEqual(a["mapping"]["sample_capable_actions"], ["BUILD_TRUST", "DO_NOTHING", "FOLLOW_UP"])
        self.assertEqual(a["mapping"]["registry_actions_never_mapped"], ["ADDRESS_OBJECTION", "ASK_QUESTION", "CLARIFY_PAYMENT", "DEPOSIT_GUIDANCE", "HUMAN_HANDOFF", "REGISTRATION_GUIDANCE"])

    def test_counts_and_statuses(self):
        pairs = [dec(1, "llm_reply", "safety"), dec(2, "followup"), dec(3, "fixed_trigger", "fraud"), dec(4, "llm_reply", None, "hard"),
                 dec(5, "followup", with_boundary=False)]
        out = Outcome(outcome_id="o1", decision_id=None, chat_id=pairs[1][0]["chat_id"], observed_at=pairs[1][0]["created_at"] + 5 * MIN,
                      outcome_type="CONTINUED", value=None, confidence=None, source="TELEGRAM_EVENT", metadata={})
        a = self.audit(*pairs, outcomes=[out], flows={"v37": {"prompt_suffix": ""}, "v38": {"prompt_suffix": ""}})
        self.assertEqual(a["decision_coverage"]["by_decision_path"], {"fixed_trigger": 1, "followup": 2, "llm_reply": 2})
        self.assertEqual(a["decision_coverage"]["branch_decisions_recorded"], 3)
        self.assertEqual(a["boundary_coverage"]["decisions_with_sent_boundary"], 4)
        self.assertEqual(a["observed_mapped_actions"], ["BUILD_TRUST", "FOLLOW_UP"])
        self.assertEqual(a["action_variation"]["status"], "PASS")
        self.assertEqual((a["attribution_coverage"]["attributed_outcomes"], a["attribution_coverage"]["decisions_with_attributable_outcomes"]), (1, 1))
        cr = a["causal_readiness"]
        self.assertEqual((cr["status"], cr["control_status"], cr["experiment"]), ("NOT_CAUSAL_READY", "CONTROL_UNAVAILABLE", "IDENTICAL_ARMS"))
        self.assertTrue(any("flow_experiment_identical_arms" in r for r in cr["reasons"]))
        self.assertTrue(any(r.startswith("control_unavailable") for r in cr["reasons"]))
        self.assertNotIn("causal", " ".join(a["action_variation"]).lower())

    def test_audit_has_no_ranking_or_causal_claim_fields(self):
        keys = set(self.audit())
        self.assertFalse(keys & {"best_action", "ranking", "winner", "uplift", "recommendation"})
