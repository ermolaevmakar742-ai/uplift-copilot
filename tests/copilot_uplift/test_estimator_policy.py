import copy
import dataclasses
import json
import random
import unittest
from datetime import datetime, timedelta, timezone

from copilot_evidence.contracts import ContractError
from copilot_prediction.contract import ObservationStatus as O
from copilot_uplift.actions import DEFAULT_REGISTRY
from copilot_uplift.contract import (ActionStats, DataQuality as Q, EstimateStatus as E, UpliftEstimate, UpliftEvaluation,
                                  evaluation_id_for)
from copilot_uplift.estimator import ESTIMATOR_VERSION, Sample, estimate
from copilot_uplift.features import FEATURE_VERSION, build_features
from copilot_uplift.policy import (LOCKED_FINGERPRINTS, POLICY_V1, PolicyError, UpliftPolicy, is_locked, recommend)

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
P = dataclasses.replace(POLICY_V1, min_samples_per_arm=3)         # small bar for unit tests only (fingerprint differs: not locked)


def samples(action, n_obs, n_no, n_unk=0, randomized=False, prob=None, start=0):
    out, i = [], start
    for status, n in ((O.OBSERVED, n_obs), (O.NO_OUTCOME, n_no), (O.UNKNOWN, n_unk)):
        for _ in range(n):
            out.append(Sample(f"pd_{action}_{i:04d}", action, status, randomized, prob))
            i += 1
    return out


class EstimatorTests(unittest.TestCase):
    def test_insufficient_data_is_not_zero_uplift(self):
        r = estimate(samples("ADDRESS_OBJECTION", 1, 1), P)
        self.assertEqual((r.data_quality, r.eligible, r.control_status), (Q.INSUFFICIENT_DATA, 2, "CONTROL_UNAVAILABLE"))
        e = r.estimates[0]
        self.assertEqual((e.status, e.value), (E.CONTROL_UNAVAILABLE, None))          # None, never 0.0
        empty = estimate([], P)
        self.assertEqual((empty.data_quality, empty.eligible, empty.stats, empty.estimates), (Q.INSUFFICIENT_DATA, 0, (), ()))

    def test_observational_without_control(self):
        r = estimate(samples("ADDRESS_OBJECTION", 4, 4) + samples("FOLLOW_UP", 1, 5), P)
        self.assertEqual(r.data_quality, Q.OBSERVATIONAL)
        self.assertEqual({e.action_id: (e.status, e.value, e.reason) for e in r.estimates},
                         {"ADDRESS_OBJECTION": (E.CONTROL_UNAVAILABLE, None, "no_randomized_control"),
                          "FOLLOW_UP": (E.CONTROL_UNAVAILABLE, None, "no_randomized_control")})
        st = {s.action_id: s for s in r.stats}
        self.assertEqual((st["ADDRESS_OBJECTION"].rate, st["FOLLOW_UP"].rate), (0.5, round(1 / 6, 6)))
        self.assertEqual((r.control_n, r.treatment_n, r.actions), (0, 0, 2))

    def test_do_nothing_without_randomization_is_not_a_control(self):
        r = estimate(samples("DO_NOTHING", 2, 4) + samples("ASK_QUESTION", 3, 3), P)
        self.assertEqual((r.control_n, r.control_status), (0, "CONTROL_UNAVAILABLE"))   # a missing message is not a control group
        self.assertEqual([e.action_id for e in r.estimates], ["ASK_QUESTION"])
        self.assertEqual({s.action_id for s in r.stats}, {"DO_NOTHING", "ASK_QUESTION"})   # still reported as an action
        self.assertEqual(r.treatment_n, 0)

    def test_experimental_with_randomized_control(self):
        r = estimate(samples("DO_NOTHING", 1, 5, randomized=True) + samples("ASK_QUESTION", 4, 2, randomized=True), P)
        self.assertEqual((r.data_quality, r.control_status, r.control_n), (Q.EXPERIMENTAL, "AVAILABLE", 6))
        e = r.estimates[0]
        self.assertEqual((e.status, e.value, e.treatment_n, e.control_n), (E.ESTIMATED, round(4 / 6 - 1 / 6, 6), 6, 6))
        self.assertEqual(e.reason, "randomized_index_decision_rate_difference_vs_control")

    def test_negative_uplift_is_reported_as_is(self):
        r = estimate(samples("DO_NOTHING", 5, 1, randomized=True) + samples("ASK_QUESTION", 1, 5, randomized=True), P)
        self.assertLess(r.estimates[0].value, 0)

    def test_arm_below_minimum_is_insufficient(self):
        r = estimate(samples("DO_NOTHING", 3, 3, randomized=True) + samples("ASK_QUESTION", 1, 0, randomized=True), P)
        self.assertEqual((r.estimates[0].status, r.estimates[0].value, r.estimates[0].reason), (E.INSUFFICIENT_DATA, None, "below_min_samples_per_arm"))

    def test_causal_ready_needs_recorded_assignment_probabilities(self):
        base = samples("DO_NOTHING", 2, 4, randomized=True, prob=0.5) + samples("ASK_QUESTION", 3, 3, randomized=True, prob=0.5)
        self.assertEqual(estimate(base, P).data_quality, Q.CAUSAL_READY)
        base[0] = dataclasses.replace(base[0], assignment_probability=None)
        self.assertEqual(estimate(base, P).data_quality, Q.EXPERIMENTAL)
        bad = [dataclasses.replace(s, assignment_probability=1.0) for s in base]
        self.assertEqual(estimate(bad, P).data_quality, Q.EXPERIMENTAL)

    def test_unknown_status_is_excluded_not_counted_as_outcome_or_non_outcome(self):
        r = estimate(samples("ADDRESS_OBJECTION", 2, 2, n_unk=5), P)
        s = r.stats[0]
        self.assertEqual((s.eligible, s.observed, s.no_outcome, s.unknown, s.rate), (4, 2, 2, 5, 0.5))
        self.assertEqual(estimate(samples("ADDRESS_OBJECTION", 0, 0, n_unk=9), P).stats[0].rate, None)   # rate None, not 0
        self.assertEqual(estimate(samples("ADDRESS_OBJECTION", 0, 0, n_unk=9), P).eligible, 0)

    def test_unmapped_and_unregistered_actions_are_not_samples(self):
        r = estimate([Sample("pd_1", None, O.OBSERVED), Sample("pd_2", "NOT_REGISTERED", O.OBSERVED)], P)
        self.assertEqual((r.stats, r.eligible), ((), 0))

    def test_deterministic_and_order_independent(self):
        s = samples("DO_NOTHING", 1, 5, randomized=True) + samples("ASK_QUESTION", 4, 2) + samples("FOLLOW_UP", 2, 4)
        base = estimate(s, P)
        rnd = random.Random(8)
        for _ in range(20):
            t = s[:]
            rnd.shuffle(t)
            self.assertEqual(estimate(t, P), base)
        self.assertEqual(ESTIMATOR_VERSION, "estimator.v2")
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "copilot_uplift" / "estimator.py").read_text(encoding="utf-8")
        for w in ("import random", "datetime.now", "uuid", "requests", "openai", "anthropic"):
            self.assertNotIn(w, src)

    def test_observational_rows_cannot_contaminate_randomized_estimate(self):
        control = samples("DO_NOTHING", 0, 3, randomized=True, prob=0.1)
        treatment = samples("ASK_QUESTION", 3, 0, randomized=True, prob=0.9)
        historical = samples("ASK_QUESTION", 0, 30, randomized=False, prob=None, start=100)
        r = estimate(control + treatment + historical, P)
        estimate_row = r.estimates[0]
        self.assertEqual((estimate_row.treatment_n, estimate_row.control_n), (3, 3))
        self.assertEqual(estimate_row.value, 1.0)
        self.assertEqual({s.action_id: s.eligible for s in r.stats}["ASK_QUESTION"], 33)  # descriptive rate retains historical rows

    def test_multiple_treatment_actions_are_not_claimed_as_independent_effects(self):
        rows = (samples("DO_NOTHING", 3, 0, randomized=True, prob=0.1)
                + samples("ASK_QUESTION", 3, 0, randomized=True, prob=0.9)
                + samples("BUILD_TRUST", 3, 0, randomized=True, prob=0.9, start=100))
        r = estimate(rows, P)
        self.assertTrue(all(e.status is E.INSUFFICIENT_DATA for e in r.estimates))
        self.assertTrue(all(e.value is None for e in r.estimates))
        self.assertTrue(all(e.reason == "multiple_treatment_actions_not_independently_randomized" for e in r.estimates))


class PolicyTests(unittest.TestCase):
    def test_policy_v1_locked_only_by_fingerprint(self):
        self.assertTrue(is_locked(POLICY_V1))
        self.assertEqual((POLICY_V1.min_unique_leads, POLICY_V1.target_outcomes), (500, ("CONTINUED",)))
        self.assertEqual(LOCKED_FINGERPRINTS[POLICY_V1.policy_version], POLICY_V1.fingerprint())
        for change in (dict(min_unique_leads=499), dict(min_samples_per_arm=29), dict(max_evaluation_age_seconds=1),
                       dict(target_outcomes=("CONTINUED", "DEPOSIT")), dict(min_quality_production="OBSERVATIONAL")):
            self.assertFalse(is_locked(dataclasses.replace(POLICY_V1, **change)), change)       # any change breaks the lock
        self.assertFalse(is_locked(dataclasses.replace(POLICY_V1, locked=False)))
        self.assertFalse(is_locked(dataclasses.replace(POLICY_V1, policy_version="uplift-policy.v2")))
        self.assertFalse(is_locked(None))

    def test_malformed_policy_rejected(self):
        for kw in (dict(policy_version=""), dict(locked="yes"), dict(min_unique_leads=0), dict(min_samples_per_arm=-1),
                   dict(min_samples_per_arm=True), dict(target_outcomes=()), dict(min_quality_shadow="BEST"),
                   dict(min_quality_production="INSUFFICIENT_DATA"), dict(max_evaluation_age_seconds=0),
                   dict(min_quality_shadow="EXPERIMENTAL", min_quality_production="OBSERVATIONAL")):
            with self.assertRaises(PolicyError, msg=str(kw)):
                dataclasses.replace(POLICY_V1, **kw)

    def _evaluation(self, quality=Q.OBSERVATIONAL, estimates=(), ts=T0, policy_version=POLICY_V1.policy_version, integrity=None, missing=None, truncated=False):
        metrics = dict(unique_leads=1, decisions=1, response_boundaries=1, outcomes=1, attributed_outcomes=1, actions=1,
                       eligible_samples=0, control_samples=0, treatment_samples=0)
        cov = dict(response_boundary_coverage=1.0, response_boundary_coverage_since_capture=1.0, attribution_coverage=1.0)
        integ = integrity or dict(boundary_violations=0, attribution_integrity_errors=0, leakage_violations=0)
        eid = evaluation_id_for("estimator.v1", FEATURE_VERSION, policy_version, "ds_1", ts.isoformat(), quality.value, 0, "CONTROL_UNAVAILABLE", metrics, (), tuple(estimates), missing or {}, truncated)
        return UpliftEvaluation(eid, "estimator.v1", FEATURE_VERSION, policy_version, "ds_1", ts, quality, 0, "CONTROL_UNAVAILABLE",
                                metrics, cov, integ, (), tuple(estimates), missing or {}, truncated)

    def vec(self):
        return build_features("pd_1", 5, T0, {"stage": "new", "intent": "question"})

    def est(self, a, v, q=Q.EXPERIMENTAL):
        return UpliftEstimate(a, E.ESTIMATED, v, 40, 40, q, "x")

    def test_recommend_none_cases_never_raise(self):
        now = T0 + timedelta(hours=1)
        cases = [(None, "no_evaluation"), (self._evaluation(), "no_positive_estimate"),
                 (self._evaluation(Q.INSUFFICIENT_DATA), "data_quality_insufficient"),
                 (self._evaluation(policy_version="uplift-policy.v0"), "policy_version_mismatch"),
                 (self._evaluation(ts=T0 - timedelta(days=30)), "stale_evaluation"),
                 (self._evaluation(estimates=[UpliftEstimate("ASK_QUESTION", E.CONTROL_UNAVAILABLE, None, 5, 0, Q.OBSERVATIONAL, "x")]), "no_positive_estimate")]
        for ev, reason in cases:
            r = recommend(self.vec(), ev, POLICY_V1, now=now)
            self.assertEqual((r.recommended_action, r.estimated_uplift, r.reason, r.eligible_for_production), (None, None, reason, False), reason)
        self.assertEqual(recommend(None, self._evaluation(), POLICY_V1).reason, "no_features")
        self.assertEqual(recommend(build_features("pd_1", 5, T0, None), self._evaluation(), POLICY_V1).reason, "no_features")
        self.assertEqual(recommend(self.vec(), "garbage", POLICY_V1).reason, "no_evaluation")
        self.assertEqual(recommend(self.vec(), self._evaluation(), "not a policy").reason, "missing_policy")

    def test_recommend_positive_unique_and_ties(self):
        ev = self._evaluation(Q.EXPERIMENTAL, [self.est("ASK_QUESTION", 0.2), self.est("BUILD_TRUST", 0.05), self.est("FOLLOW_UP", -0.1)])
        r = recommend(self.vec(), ev, POLICY_V1, now=T0 + timedelta(hours=1))
        self.assertEqual((r.recommended_action, r.alternative_action, r.estimated_uplift, r.eligible_for_production),
                         ("ASK_QUESTION", "BUILD_TRUST", 0.2, True))
        tie = self._evaluation(Q.EXPERIMENTAL, [self.est("ASK_QUESTION", 0.2), self.est("BUILD_TRUST", 0.2)])
        self.assertEqual(recommend(self.vec(), tie, POLICY_V1).reason, "tie_between_actions")     # no arbitrary winner
        shuffled = self._evaluation(Q.EXPERIMENTAL, [self.est("BUILD_TRUST", 0.05), self.est("ASK_QUESTION", 0.2)])
        self.assertEqual(recommend(self.vec(), shuffled, POLICY_V1).recommended_action, "ASK_QUESTION")

    def test_incomplete_evaluation_never_marks_recommendation_production_eligible(self):
        ev = self._evaluation(Q.EXPERIMENTAL, [self.est("ASK_QUESTION", 0.2)])
        vector = self.vec()
        for bad in (self._evaluation(Q.EXPERIMENTAL, [self.est("ASK_QUESTION", 0.2)], truncated=True),
                    self._evaluation(Q.EXPERIMENTAL, [self.est("ASK_QUESTION", 0.2)], missing={"provider_unreadable_rows:decisions": 1}),
                    self._evaluation(Q.EXPERIMENTAL, [self.est("ASK_QUESTION", 0.2)], missing={"ambiguous_experiment_assignments": 1})):
            rec = recommend(vector, bad, POLICY_V1, now=T0 + timedelta(hours=1))
            self.assertEqual(rec.recommended_action, "ASK_QUESTION")
            self.assertFalse(rec.eligible_for_production)

    def test_observational_estimate_is_shadow_only_never_production(self):
        ev = self._evaluation(Q.OBSERVATIONAL, [self.est("ASK_QUESTION", 0.2, Q.OBSERVATIONAL)])
        r = recommend(self.vec(), ev, POLICY_V1)
        self.assertEqual((r.recommended_action, r.eligible_for_production), ("ASK_QUESTION", False))

    def test_baseline_and_unregistered_never_recommended(self):
        ev = self._evaluation(Q.EXPERIMENTAL, [self.est("DO_NOTHING", 0.5), self.est("NOT_REGISTERED", 0.9)])
        self.assertEqual(recommend(self.vec(), ev, POLICY_V1).reason, "no_positive_estimate")


class EvaluationContractTests(unittest.TestCase):
    def _ev(self, **over):
        return PolicyTests()._evaluation(**over)

    def test_round_trip_strict_and_tamper_evident(self):
        e = self._ev(estimates=[UpliftEstimate("ASK_QUESTION", E.CONTROL_UNAVAILABLE, None, 5, 0, Q.OBSERVATIONAL, "no_randomized_control")])
        d = json.loads(json.dumps(e.to_dict()))
        self.assertEqual(UpliftEvaluation.from_dict(d), e)
        for k in list(d):
            bad = copy.deepcopy(d)
            del bad[k]
            with self.assertRaises(ContractError, msg=k):
                UpliftEvaluation.from_dict(bad)
        for path, v in ((("evaluation_id",), "ue_forged"), (("metrics", "unique_leads"), 999), (("data_quality",), "CAUSAL_READY"),
                        (("contract_version",), "x"), (("coverage", "attribution_coverage"), 2), (("integrity", "leakage_violations"), -1),
                        (("sample_count",), 7), (("extra",), 1), (("truncated",), "no"), (("truncated",), True), (("missing_signals",), {"tampered": 1}), (("control_status",), "MAYBE")):
            bad = copy.deepcopy(d)
            cur = bad
            for p in path[:-1]:
                cur = cur[p]
            cur[path[-1]] = v
            with self.assertRaises(ContractError, msg=str(path)):
                UpliftEvaluation.from_dict(bad)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            e.data_quality = Q.CAUSAL_READY
        with self.assertRaises(TypeError):
            e.metrics["unique_leads"] = 1

    def test_estimate_and_stats_invariants(self):
        with self.assertRaises(ContractError):
            UpliftEstimate("A", E.ESTIMATED, None, 1, 1, Q.EXPERIMENTAL, "x")           # estimated without a value
        with self.assertRaises(ContractError):
            UpliftEstimate("A", E.INSUFFICIENT_DATA, 0.0, 1, 1, Q.EXPERIMENTAL, "x")    # a value without ESTIMATED
        with self.assertRaises(ContractError):
            UpliftEstimate("A", E.ESTIMATED, 0.1, 1, 1, Q.INSUFFICIENT_DATA, "x")
        with self.assertRaises(ContractError):
            UpliftEstimate("A", E.ESTIMATED, 1.5, 1, 1, Q.EXPERIMENTAL, "x")
        with self.assertRaises(ContractError):
            ActionStats("A", 3, 1, 1, 0, 0.5)                                            # eligible != observed + no_outcome
        with self.assertRaises(ContractError):
            ActionStats("A", 0, 0, 0, 2, 0.0)                                            # rate must be None with no samples
        self.assertEqual(ActionStats("A", 0, 0, 0, 2, None).rate, None)


if __name__ == "__main__":
    unittest.main()
