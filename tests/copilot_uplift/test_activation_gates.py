import dataclasses
import unittest
from datetime import datetime, timedelta, timezone

from copilot_uplift.activation import (CONFIRMATION, ActivationError, StoredState, effective_status, influence_allowed, replay,
                                    request_disable, request_enable)
from copilot_uplift.contract import (ActionStats, ActivationEvent, ActivationState as S, DataQuality as Q, EstimateStatus as E, UpliftEstimate,
                                  UpliftEvaluation, evaluation_id_for, new_event)
from copilot_uplift.features import FEATURE_VERSION
from copilot_uplift.gates import KILL_SWITCH_ENV, Gate, GateReport, compute_gates, kill_switch_on
from copilot_uplift.policy import POLICY_V1
from copilot_evidence.contracts import ContractError

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
GOOD_METRICS = dict(unique_leads=500, decisions=900, response_boundaries=800, outcomes=300, attributed_outcomes=120, actions=3,
                    eligible_samples=400, control_samples=0, treatment_samples=400)


def make_eval(metrics=None, quality=Q.EXPERIMENTAL, integrity=None, coverage=None, ts=T0, policy_version=POLICY_V1.policy_version, estimates=(),
              observed=("ASK_QUESTION", "BUILD_TRUST", "FOLLOW_UP"), stats=None, missing=None, truncated=False):
    m = {**GOOD_METRICS, "actions": len(observed), **(metrics or {})}
    stats = tuple(stats) if stats is not None else tuple(ActionStats(a, 10, 3, 7, 0, 0.3) for a in observed)
    cov = coverage or dict(response_boundary_coverage=0.9, response_boundary_coverage_since_capture=0.95, attribution_coverage=0.4)
    integ = integrity or dict(boundary_violations=0, attribution_integrity_errors=0, leakage_violations=0)
    eid = evaluation_id_for("estimator.v1", FEATURE_VERSION, policy_version, "ds_x", ts.isoformat(), quality.value, m["eligible_samples"],
                            "CONTROL_UNAVAILABLE", m, stats, tuple(estimates))
    return UpliftEvaluation(eid, "estimator.v1", FEATURE_VERSION, policy_version, "ds_x", ts, quality, m["eligible_samples"],
                            "CONTROL_UNAVAILABLE", m, cov, integ, stats, tuple(estimates), missing or {}, truncated)


def gates_for(**kw):
    storage = kw.pop("storage", True)
    return compute_gates(make_eval(**kw), POLICY_V1, activation_storage_ok=storage)


class GateTests(unittest.TestCase):
    def test_all_pass(self):
        r = gates_for()
        self.assertTrue(r.all_passed, r.missing)
        self.assertEqual((r.missing, r.integrity_failed), ((), False))
        self.assertEqual(len(r.gates), 12)

    def test_below_500_leads_blocks_exactly_at_500_passes(self):
        for n, ok in ((0, False), (1, False), (499, False), (500, True), (501, True), (5000, True)):
            r = gates_for(metrics={"unique_leads": n})
            self.assertEqual(next(g for g in r.gates if g.name == "unique_leads").passed, ok, n)
            self.assertEqual(r.all_passed, ok, n)
        self.assertIn("unique_leads", gates_for(metrics={"unique_leads": 499}).missing)

    def test_500_leads_alone_never_passes_other_mandatory_gates(self):
        for changes, gate in (({"outcomes": 0}, "outcome_pipeline_operational"), ({"attributed_outcomes": 0}, "eligible_outcomes_nonzero"),
                              ({"response_boundaries": 0}, "response_boundary_integrity"),
                              ({"response_boundaries": 0}, "attribution_v2_operational")):
            r = gates_for(metrics=changes)
            self.assertFalse(r.all_passed, gate)
            self.assertIn(gate, r.missing)

    def test_action_variation_counts_observed_mapped_actions_not_the_registry(self):
        from copilot_uplift.actions import Action, DEFAULT_REGISTRY
        from copilot_uplift.gates import observed_actions
        one = make_eval(observed=("BUILD_TRUST",))
        r = compute_gates(one, POLICY_V1, activation_storage_ok=True)
        self.assertIn("action_variation", r.missing)
        self.assertIn("1 observed mapped actions", next(g for g in r.gates if g.name == "action_variation").detail)
        # growing the registry to 50 actions changes nothing: only actions with eligible samples count
        big = DEFAULT_REGISTRY.extended(*(Action(f"EXTRA_{i}", "d") for i in range(50)), version="actions.v9")
        self.assertIn("action_variation", compute_gates(one, POLICY_V1, activation_storage_ok=True, registry=big).missing)
        self.assertEqual(observed_actions(one, big), ("BUILD_TRUST",))
        self.assertEqual(len(DEFAULT_REGISTRY.ids()), 9)
        # a metrics counter that claims variation but is not backed by data does not pass
        liar = make_eval(metrics={"actions": 5}, observed=("BUILD_TRUST",))
        self.assertIn("action_variation", compute_gates(liar, POLICY_V1, activation_storage_ok=True).missing)
        # stats with zero eligible samples (or an unregistered action) are not observations
        zero = make_eval(observed=("BUILD_TRUST", "FOLLOW_UP"),
                         stats=(ActionStats("BUILD_TRUST", 4, 1, 3, 0, 0.25), ActionStats("FOLLOW_UP", 0, 0, 0, 7, None)))
        self.assertEqual(observed_actions(zero), ("BUILD_TRUST",))
        two = make_eval(observed=("BUILD_TRUST", "FOLLOW_UP"))
        self.assertTrue(compute_gates(two, POLICY_V1, activation_storage_ok=True).all_passed)
        unreg = make_eval(observed=("BUILD_TRUST", "NOT_REGISTERED"),
                          stats=(ActionStats("BUILD_TRUST", 4, 1, 3, 0, 0.25), ActionStats("NOT_REGISTERED", 4, 1, 3, 0, 0.25)))
        self.assertEqual(observed_actions(unreg), ("BUILD_TRUST",))

    def test_other_failure_modes(self):
        self.assertIn("offline_evaluator", gates_for(quality=Q.INSUFFICIENT_DATA).missing)
        r = gates_for(integrity=dict(boundary_violations=0, attribution_integrity_errors=1, leakage_violations=0))
        self.assertIn("no_attribution_integrity_errors", r.missing)
        self.assertTrue(r.integrity_failed)
        r = gates_for(integrity=dict(boundary_violations=0, attribution_integrity_errors=0, leakage_violations=2))
        self.assertIn("no_data_leakage", r.missing)
        self.assertTrue(r.integrity_failed)
        r = gates_for(integrity=dict(boundary_violations=3, attribution_integrity_errors=0, leakage_violations=0))
        self.assertIn("response_boundary_integrity", r.missing)
        self.assertIn("activation_storage", gates_for(storage=False).missing)
        self.assertIn("policy_locked", compute_gates(make_eval(policy_version="uplift-policy.v0"), POLICY_V1, activation_storage_ok=True).missing)
        self.assertIn("policy_locked", compute_gates(make_eval(), dataclasses.replace(POLICY_V1, min_samples_per_arm=29), activation_storage_ok=True).missing)
        self.assertIn("attribution_v2_operational", gates_for(coverage=dict(response_boundary_coverage=0.0, response_boundary_coverage_since_capture=None, attribution_coverage=None)).missing)

    def test_incomplete_provider_data_blocks_activation(self):
        for evaluation in (
            make_eval(truncated=True),
            make_eval(missing={"provider_unreadable_rows:decisions": 1}),
            make_eval(missing={"provider_required_table_unavailable:uplift_assignments": 1}),
            make_eval(missing={"ambiguous_experiment_assignments": 1}),
        ):
            report = compute_gates(evaluation, POLICY_V1, activation_storage_ok=True)
            self.assertIn("provider_data_complete", report.missing)
            self.assertFalse(report.all_passed)
            self.assertTrue(report.integrity_failed)

    def test_observational_evaluation_cannot_pass_production_gate(self):
        report = gates_for(quality=Q.OBSERVATIONAL)
        self.assertIn("offline_evaluator", report.missing)
        self.assertFalse(report.all_passed)

    def test_no_evaluation_or_failed_evaluator_blocks_everything(self):
        for bad in (None, "x", {}):
            r = compute_gates(bad, POLICY_V1, activation_storage_ok=True)
            self.assertFalse(r.all_passed)
            self.assertEqual(len(r.missing), 12)
            self.assertFalse(r.integrity_failed)
        self.assertFalse(GateReport((), False).all_passed)                          # an empty report never passes


def report(ok=True, failed=False):
    return GateReport(tuple(Gate(f"g{i}", ok or i != 0, "d") for i in range(3)), failed)


class ActivationTests(unittest.TestCase):
    def enable(self, events=(), rep=None, confirm=CONFIRMATION, at=T0):
        return request_enable(events, changed_by="admin", confirmation=confirm, report=rep or report(), policy=POLICY_V1, now=at)

    def test_initial_state_off_and_locked_until_gates_pass(self):
        st = replay([])
        self.assertEqual((st.state, st.corrupt), (S.OFF, False))
        self.assertEqual(effective_status(st, report(ok=False)), S.OFF)              # <500 / gates failing -> OFF ("locked")
        self.assertEqual(effective_status(st, None), S.OFF)
        self.assertEqual(effective_status(st, report()), S.READY)                    # all gates pass -> READY, still not ACTIVE
        self.assertEqual(replay([]).state, S.OFF)

    def test_never_automatic_ready_stays_ready(self):
        st = replay([])
        for _ in range(3):
            self.assertEqual(effective_status(st, report()), S.READY)                 # no matter how often re-evaluated

    def test_enable_requires_exact_confirmation(self):
        for bad in ("", "enable uplift", "ENABLE UPLIFT ", "yes", None):
            with self.assertRaises(ActivationError) as cm:
                self.enable(confirm=bad)
            self.assertEqual(cm.exception.code, "confirmation_required")

    def test_enable_with_all_gates_becomes_active_and_event_is_auditable(self):
        ev, state = self.enable()
        self.assertEqual((state, ev.previous_state, ev.new_state, ev.changed_by, ev.policy_version), (S.ACTIVE, S.OFF, S.ACTIVE, "admin", "uplift-policy.v1"))
        self.assertIn("500+ unique leads confirmed", ev.reason)
        self.assertEqual(ev.changed_at, T0)
        self.assertTrue(ev.event_id.startswith("ua_"))
        self.assertTrue(ev.gates["all_passed"])
        st = replay([ev])
        self.assertEqual(st.state, S.ACTIVE)
        self.assertEqual(effective_status(st, report()), S.ACTIVE)

    def test_enable_with_failing_gate_is_blocked_and_recorded(self):
        ev, state = self.enable(rep=report(ok=False))
        self.assertEqual((state, ev.new_state), (S.BLOCKED, S.BLOCKED))
        self.assertIn("missing gates: g0", ev.reason)
        st = replay([ev])
        self.assertEqual(st.state, S.BLOCKED)
        self.assertEqual(effective_status(st, report(ok=False)), S.OFF)              # still locked, never ACTIVE
        ev2, state2 = self.enable([ev], at=T0 + H)                                   # retry after fixing -> allowed
        self.assertEqual((state2, ev2.previous_state), (S.ACTIVE, S.BLOCKED))
        ev3, st3 = self.enable(rep=report(failed=True))
        self.assertEqual(st3, S.BLOCKED)                                              # integrity failure blocks even with all gates ok

    def test_active_with_failing_gates_is_blocked(self):
        ev, _ = self.enable()
        self.assertEqual(effective_status(replay([ev]), report(ok=False)), S.BLOCKED)
        self.assertEqual(effective_status(replay([ev]), report(failed=True)), S.BLOCKED)
        self.assertEqual(effective_status(replay([ev]), None), S.BLOCKED)

    def test_cannot_enable_twice_or_disable_when_inactive(self):
        ev, _ = self.enable()
        with self.assertRaises(ActivationError) as cm:
            self.enable([ev], at=T0 + H)
        self.assertEqual(cm.exception.code, "already_active")
        with self.assertRaises(ActivationError) as cm:
            request_disable([], changed_by="admin", reason="x", policy=POLICY_V1, now=T0)
        self.assertEqual(cm.exception.code, "not_active")

    def test_disable_removes_influence_and_blocked_on_integrity(self):
        ev, _ = self.enable()
        off = request_disable([ev], changed_by="admin", reason="emergency", policy=POLICY_V1, now=T0 + H)
        self.assertEqual((off.previous_state, off.new_state, off.reason), (S.ACTIVE, S.OFF, "emergency"))
        self.assertEqual(replay([ev, off]).state, S.OFF)
        blocked = request_disable([ev], changed_by="admin", reason="integrity", policy=POLICY_V1, now=T0 + H, integrity_failure=True)
        self.assertEqual(blocked.new_state, S.BLOCKED)
        self.assertEqual(replay([ev, blocked]).state, S.BLOCKED)

    def test_events_are_append_only_immutable_and_chain_validated(self):
        ev, _ = self.enable()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ev.new_state = S.OFF
        with self.assertRaises(ContractError):
            new_event(S.READY, S.ACTIVE, changed_by="admin", changed_at=T0, reason="x", policy_version="v")      # READY is derived
        with self.assertRaises(ContractError):
            ActivationEvent.from_dict({**ev.to_dict(), "event_id": "ua_forged"})
        with self.assertRaises(ContractError):
            ActivationEvent.from_dict({**ev.to_dict(), "new_state": "OFF"})                                       # id is content-bound
        self.assertEqual(ActivationEvent.from_dict(ev.to_dict()), ev)
        broken = new_event(S.ACTIVE, S.OFF, changed_by="x", changed_at=T0 + H, reason="r", policy_version="v")   # no preceding ACTIVE event
        st = replay([broken])
        self.assertTrue(st.corrupt)
        self.assertEqual(effective_status(st, report()), S.BLOCKED)
        with self.assertRaises(ActivationError) as cm:
            self.enable([broken])
        self.assertEqual(cm.exception.code, "activation_state_corrupt")
        dup = replay([ev, new_event(S.OFF, S.ACTIVE, changed_by="b", changed_at=T0 + H, reason="r", policy_version="v")])
        self.assertTrue(dup.corrupt)                                                                              # second ACTIVE from OFF: chain broken
        a = replay([ev, request_disable([ev], changed_by="a", reason="r", policy=POLICY_V1, now=T0 + H)])
        self.assertEqual(replay(list(reversed([ev, request_disable([ev], changed_by="a", reason="r", policy=POLICY_V1, now=T0 + H)]))), a)


class KillSwitchAndInfluenceTests(unittest.TestCase):
    ACTIVE = replay([request_enable([], changed_by="admin", confirmation=CONFIRMATION, report=report(), policy=POLICY_V1, now=T0)[0]])
    NOW = T0 + H

    def allowed(self, **kw):
        a = dict(stored=self.ACTIVE, kill_switch=True, policy=POLICY_V1, evaluation=make_eval(quality=Q.EXPERIMENTAL), now=self.NOW)
        a.update(kw)
        return influence_allowed(a.pop("stored"), **a)

    def test_default_off_and_kill_switch_overrides_everything(self):
        self.assertFalse(kill_switch_on({}))
        for v in ("", "false", "0", "no", "off", "x", None):
            self.assertFalse(kill_switch_on({KILL_SWITCH_ENV: v}), v)
        for v in ("true", "TRUE", "1", "yes", "on"):
            self.assertTrue(kill_switch_on({KILL_SWITCH_ENV: v}))
        r = self.allowed(kill_switch=False)                                          # ACTIVE state + perfect evaluation + switch OFF
        self.assertEqual((r.allowed, r.reason), (False, "kill_switch_off"))
        self.assertTrue(self.allowed().allowed)                                       # all conditions together -> allowed

    def test_corrupt_or_inactive_state_never_influences(self):
        corrupt = StoredState(S.ACTIVE, None, True, "x")
        self.assertEqual(self.allowed(stored=corrupt).reason, "activation_state_corrupt")
        self.assertEqual(self.allowed(stored=replay([])).reason, "not_activated")
        self.assertEqual(self.allowed(stored=StoredState(S.BLOCKED, None)).reason, "not_activated")
        off = replay([*(e for e in [request_enable([], changed_by="a", confirmation=CONFIRMATION, report=report(), policy=POLICY_V1, now=T0)[0]])])
        disabled = replay([off.last_event, request_disable([off.last_event], changed_by="a", reason="r", policy=POLICY_V1, now=T0 + H)])
        self.assertEqual(self.allowed(stored=disabled).reason, "not_activated")      # disable removes influence immediately

    def test_policy_and_evaluation_must_be_valid_fresh_and_clean(self):
        self.assertEqual(self.allowed(policy=dataclasses.replace(POLICY_V1, min_samples_per_arm=29)).reason, "policy_not_locked")
        self.assertEqual(self.allowed(evaluation=None).reason, "no_evaluation")
        self.assertEqual(self.allowed(evaluation="junk").reason, "no_evaluation")
        self.assertEqual(self.allowed(evaluation=make_eval(policy_version="uplift-policy.v0", quality=Q.EXPERIMENTAL)).reason, "stale_policy")
        self.assertEqual(self.allowed(now=T0 + timedelta(days=8)).reason, "stale_evaluation")
        incomplete = dataclasses.replace(make_eval(quality=Q.EXPERIMENTAL), truncated=True)
        self.assertEqual(self.allowed(evaluation=incomplete).reason, "provider_data_incomplete")
        unreadable = dataclasses.replace(make_eval(quality=Q.EXPERIMENTAL),
                                         missing_signals={"provider_unreadable_rows:decisions": 1})
        self.assertEqual(self.allowed(evaluation=unreadable).reason, "provider_data_incomplete")
        bad = make_eval(quality=Q.EXPERIMENTAL, integrity=dict(boundary_violations=0, attribution_integrity_errors=1, leakage_violations=0))
        self.assertEqual(self.allowed(evaluation=bad).reason, "integrity_failure")
        self.assertEqual(self.allowed(evaluation=make_eval(quality=Q.OBSERVATIONAL)).reason, "data_quality_below_production_bar")
        self.assertEqual(self.allowed(evaluation=make_eval(quality=Q.INSUFFICIENT_DATA)).reason, "data_quality_below_production_bar")
        self.assertFalse(influence_allowed("garbage", kill_switch=True, policy=POLICY_V1, evaluation=None, now=self.NOW).allowed)   # never raises


if __name__ == "__main__":
    unittest.main()
