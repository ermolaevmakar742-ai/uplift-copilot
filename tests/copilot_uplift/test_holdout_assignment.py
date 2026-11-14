"""Holdout assignment: deterministic coin, kill switch, estimator uplift of data quality (unit tests, no DB)."""
import dataclasses
import unittest
from datetime import datetime, timedelta, timezone

from copilot_prediction.contract import ObservationStatus as O
from copilot_uplift.actions import BASELINE_ACTION_ID
from copilot_uplift.assignment import (CONTROL, CONTROL_SHARE, KILL_SWITCH_ENV, SALT, TREATMENT, arm_for,
                                    assigned_arm, assignment_id, probability)
from copilot_uplift.evaluator import UpliftData, evaluate_uplift
from copilot_uplift.estimator import Sample, estimate
from copilot_uplift.policy import POLICY_V2, UpliftPolicy

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
P = dataclasses.replace(POLICY_V2, min_samples_per_arm=5)     # small bar for unit tests only (fingerprint differs: not locked)


class ArmTests(unittest.TestCase):
    def test_deterministic_and_replayable(self):
        self.assertEqual(arm_for(12345), arm_for(12345))
        self.assertEqual(arm_for(12345, salt=SALT), arm_for(12345))
        self.assertEqual(arm_for(-77), arm_for(-77))

    def test_distribution_is_ten_percent_control(self):
        n = 20000
        control = sum(1 for cid in range(n) if arm_for(cid) == CONTROL)
        share = control / n
        self.assertGreater(share, CONTROL_SHARE - 0.015)
        self.assertLess(share, CONTROL_SHARE + 0.015)

    def test_kill_switch_off_means_no_assignment(self):
        self.assertIsNone(assigned_arm(1, env={}))
        self.assertIsNone(assigned_arm(1, env={KILL_SWITCH_ENV: "false"}))
        control_chat = next(cid for cid in range(10000) if arm_for(cid) == CONTROL)
        self.assertIsNone(assigned_arm(control_chat, env={KILL_SWITCH_ENV: "false"}))
        self.assertEqual(assigned_arm(control_chat, env={KILL_SWITCH_ENV: "true"}), CONTROL)
        self.assertIn(assigned_arm(7, env={KILL_SWITCH_ENV: "true"}), (CONTROL, TREATMENT))

    def test_assignment_id_stable_and_probability(self):
        self.assertEqual(assignment_id(5, CONTROL), assignment_id(5, CONTROL))
        self.assertNotEqual(assignment_id(5, CONTROL), assignment_id(6, CONTROL))
        self.assertAlmostEqual(probability(CONTROL), 0.10)
        self.assertAlmostEqual(probability(TREATMENT), 0.90)


class EstimatorUpliftTests(unittest.TestCase):
    def test_randomized_control_raises_quality_to_experimental(self):
        samples = ([Sample(f"pd_c_{i}", BASELINE_ACTION_ID, O.NO_OUTCOME, True, 0.1) for i in range(6)]
                   + [Sample(f"pd_t_{i}", "FOLLOW_UP", O.OBSERVED if i < 2 else O.NO_OUTCOME, False, None) for i in range(6)])
        r = estimate(samples, P)
        self.assertEqual((r.control_status, r.data_quality), ("AVAILABLE", "EXPERIMENTAL"))
        e = {x.action_id: x for x in r.estimates}["FOLLOW_UP"]
        self.assertEqual(e.status, "ESTIMATED")
        self.assertAlmostEqual(e.value, round(2 / 6 - 0.0, 6))

    def test_all_armed_samples_reach_causal_ready(self):
        samples = ([Sample(f"pd_c_{i}", BASELINE_ACTION_ID, O.NO_OUTCOME, True, 0.1) for i in range(6)]
                   + [Sample(f"pd_t_{i}", "FOLLOW_UP", O.OBSERVED if i < 2 else O.NO_OUTCOME, True, 0.9) for i in range(6)])
        r = estimate(samples, P)
        self.assertEqual(r.data_quality, "CAUSAL_READY")

    def test_do_nothing_without_randomization_is_not_a_control(self):
        samples = [Sample(f"pd_c_{i}", BASELINE_ACTION_ID, O.NO_OUTCOME, False, None) for i in range(6)]
        r = estimate(samples, P)
        self.assertEqual(r.control_status, "CONTROL_UNAVAILABLE")


class BaselineEvaluationTests(unittest.TestCase):
    ASG = ("ua_x", 5, "CONTROL", 0.1, T0)

    def _dec(self, i, at, chat=5):
        return {"decision_id": f"pd_{i}", "chat_id": chat, "created_at": at, "regime": None, "trigger": None,
                "decision_path": "followup_holdout", "state": {"stage": "new", "intent": "question", "sentiment": 0.7,
                                                               "region": "MX", "messages": 3}}

    def _out(self, oid, at, chat=5, typ="CONTINUED"):
        from copilot_outcomes.contracts import Outcome
        return Outcome(outcome_id=oid, decision_id=None, chat_id=chat, observed_at=at, outcome_type=typ, value=None,
                       confidence=None, source="TELEGRAM_EVENT", metadata={})

    def _run(self, decisions, outcomes, *, assignments=(ASG,), now):
        return evaluate_uplift(UpliftData(tuple(decisions), (), tuple(outcomes), (), assignments=assignments), P,
                               evaluated_at=now)

    def test_holdout_outcome_in_window_is_observed(self):
        d = self._dec(1, T0)
        e = self._run([d], [self._out("o1", T0 + 2 * H)], now=T0 + 48 * H)
        self.assertEqual(e.conversion_by_action[0].action_id, "DO_NOTHING")
        self.assertEqual((e.conversion_by_action[0].observed, e.conversion_by_action[0].rate), (1, 1.0))
        self.assertEqual(e.metrics["control_samples"], 1)
        ms = e.missing_signals
        self.assertEqual(ms.get("holdout_baseline_decisions"), 1)
        self.assertNotIn("decisions_without_sent_boundary", ms)          # a holdout decision has no boundary by design

    def test_holdout_window_elapsed_no_outcome_is_no_outcome_not_unknown(self):
        d = self._dec(1, T0)
        ev_out = self._out("o0", T0 - 5 * H, chat=9)                    # capture evidence exists
        e0 = self._run([d], [ev_out], now=T0 + 48 * H)
        self.assertEqual(e0.conversion_by_action[0].no_outcome, 1)      # window elapsed, no outcome
        e2 = self._run([d], [ev_out], now=T0 + 6 * H)
        self.assertEqual(e2.missing_signals.get("decisions_observation_unknown:window_open"), 1)
        e1 = self._run([d], [], now=T0 + 48 * H)
        self.assertEqual(e1.missing_signals.get("decisions_observation_unknown:no_outcome_capture_evidence"), 1)

    def test_windowed_outcome_after_the_holdout_window_is_not_its_outcome(self):
        d = self._dec(1, T0)
        outs = [self._out("o1", T0 + 20 * H), self._out("o0", T0 - 5 * H, chat=9)]
        e = self._run([d], outs, now=T0 + 48 * H)
        self.assertEqual(e.conversion_by_action[0].no_outcome, 1)        # outside the 12h holdout window

    def test_control_plus_treatment_gives_estimated_uplift(self):
        hold = self._dec(1, T0)
        treated = {"decision_id": "pd_2", "chat_id": 6, "created_at": T0, "regime": None, "trigger": None,
                   "decision_path": "followup", "state": None}
        bnd = __import__("copilot_attribution.rules_v2", fromlist=["BoundaryRef"]).BoundaryRef(
            "rb_1", "pd_2", 6, T0 + timedelta(seconds=14), "SENT", True, None, None)
        outs = [self._out("o2", T0 + 2 * H, chat=6), self._out("o0", T0 - 5 * H, chat=9)]
        e = evaluate_uplift(UpliftData((hold, treated), (bnd,), tuple(outs), (),
                                       assignments=(self.ASG, ("ua_y", 6, "TREATMENT", 0.9, T0))),
                            dataclasses.replace(POLICY_V2, min_samples_per_arm=1), evaluated_at=T0 + 48 * H)
        est = {x.action_id: x for x in e.uplift_estimates}["FOLLOW_UP"]
        self.assertEqual(est.status, "ESTIMATED")
        self.assertAlmostEqual(est.value, 1.0)                           # 1/1 treated vs 0/1 holdout control
        self.assertEqual(e.metrics["control_samples"], 1)
        self.assertEqual(e.metrics["treatment_samples"], 1)

    def test_unassigned_holdout_decision_is_not_a_control(self):
        d = self._dec(1, T0)
        e = self._run([d], [], assignments=(), now=T0 + 48 * H)
        self.assertEqual((e.metrics["control_samples"], e.control_status), (0, "CONTROL_UNAVAILABLE"))
