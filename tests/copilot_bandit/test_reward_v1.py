import copy
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from copilot_bandit import reward as R
from copilot_bandit.backend import LinUCBBackend
from copilot_bandit.reward import DelayedEvent, RewardInput, RewardPolicy, correct_v1, resolve_v1
from copilot_shadow import store
from tests.copilot_shadow.test_shadow import Base

D0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
O0 = D0 + timedelta(hours=1)
# EXPLICIT TEST PARAMETERS (illustrative, not tuned on any data)
P = RewardPolicy(decay=0.9, max_touches_before_penalty=3, penalty_per_extra_touch=0.05, chargeback_multiplier=1.0,
                 amount_unit=100.0, complaint_penalty=0.3, block_penalty=0.4, params_confirmed=True)


def inp(outcome="DEPOSIT", touches=1, status="observed", events=(), now=None, amount=100.0):
    return RewardInput(status, outcome, D0, O0, touches, amount, tuple(events), now or O0 + timedelta(days=40))


class TestRewardV1(unittest.TestCase):
    def test_deposit_base_and_version(self):
        r = resolve_v1(inp(touches=1), P)
        self.assertEqual((r.reward, r.base_reward, r.reward_version, r.outcome, r.status), (1.0, 1.0, "reward.v1", "DEPOSIT", "final"))
        self.assertEqual((r.touch_penalty, r.touch_decay_factor, r.negative_adjustments), (0.0, 1.0, ()))
        self.assertEqual(r.time_to_outcome_seconds, 3600.0)

    def test_silence_is_zero_never_negative_even_with_many_touches(self):
        r = resolve_v1(RewardInput("expired", None, D0, None, 40, None, (), O0), P)
        self.assertEqual((r.reward, r.touch_penalty, r.outcome), (0.0, 0.0, "SILENCE"))
        self.assertIn("SILENCE_ZERO_REWARD", r.flags)

    def test_unknown_and_pending_have_no_reward(self):
        for st, flag in (("unknown", "OUTCOME_UNKNOWN"), ("pending", "OUTCOME_PENDING"), ("weird", "OUTCOME_STATUS_INVALID")):
            r = resolve_v1(RewardInput(st), P)
            self.assertIsNone(r.reward); self.assertEqual(r.status, "undefined"); self.assertIn(flag, r.flags)

    def test_touches_one_five_and_over_max(self):
        self.assertEqual(resolve_v1(inp(touches=1), P).reward, 1.0)
        r5 = resolve_v1(inp(touches=5), P)
        self.assertAlmostEqual(r5.touch_decay_factor, 0.9 ** 4, places=6)
        self.assertAlmostEqual(r5.touch_penalty, 0.05 * 2, places=6)                       # 5 - max(3)
        self.assertAlmostEqual(r5.reward, round(0.9 ** 4 - 0.10, 6), places=6)
        at_max = resolve_v1(inp(touches=3), P)
        self.assertEqual(at_max.touch_penalty, 0.0)                                         # <= max: no penalty
        self.assertGreater(at_max.reward, r5.reward)

    def test_decay_applies_only_to_positive_base(self):
        r = resolve_v1(inp("CONTINUED", touches=6), P)
        self.assertEqual((r.base_reward, r.touch_decay_factor), (0.0, 1.0))
        self.assertAlmostEqual(r.reward, -0.15, places=6)                                   # only the touch penalty acts

    def test_neutral_default_policy_changes_nothing_and_says_so(self):
        r = resolve_v1(inp(touches=9))
        self.assertEqual((r.reward, r.touch_penalty, r.touch_decay_factor), (1.0, 0.0, 1.0))
        self.assertIn("POLICY_PARAMS_UNCONFIRMED", r.flags)
        self.assertNotIn("POLICY_PARAMS_UNCONFIRMED", resolve_v1(inp(), P).flags)

    def test_complaint_and_block_penalties(self):
        c = resolve_v1(inp(events=[DelayedEvent("COMPLAINT", O0 + timedelta(days=2), "c1")]), P)
        b = resolve_v1(inp(events=[DelayedEvent("BLOCK", O0 + timedelta(hours=3), "b1")]), P)
        self.assertAlmostEqual(c.reward, 0.7, places=6); self.assertAlmostEqual(b.reward, 0.6, places=6)
        self.assertEqual(c.negative_adjustments[0]["kind"], "COMPLAINT")

    def test_chargeback_penalty_and_amount(self):
        full = resolve_v1(inp(events=[DelayedEvent("CHARGEBACK", O0 + timedelta(days=10), "x1", 100.0)]), P)
        part = resolve_v1(inp(events=[DelayedEvent("REFUND", O0 + timedelta(days=10), "x2", 25.0)]), P)
        self.assertAlmostEqual(full.reward, 0.0, places=6); self.assertAlmostEqual(part.reward, 0.75, places=6)
        self.assertAlmostEqual(full.negative_adjustments[0]["adjustment"], -1.0, places=6)
        big = resolve_v1(inp(events=[DelayedEvent("CHARGEBACK", O0 + timedelta(days=10), "x3", 900.0)]), P)
        self.assertEqual(big.reward, -1.0); self.assertIn("REWARD_CLAMPED", big.flags)
        dbl = RewardPolicy(chargeback_multiplier=2.0, amount_unit=100.0, params_confirmed=True)
        self.assertAlmostEqual(resolve_v1(inp(events=[DelayedEvent("CHARGEBACK", O0 + timedelta(days=1), "x4", 25.0)]), dbl).reward, 0.5, places=6)

    def test_missing_amount_or_unit_is_not_invented(self):
        ev = [DelayedEvent("CHARGEBACK", O0 + timedelta(days=1), "x5", None)]
        r = resolve_v1(inp(events=ev), P)
        self.assertEqual(r.reward, 1.0); self.assertIn("CHARGEBACK_AMOUNT_UNSCALED", r.flags)
        ev2 = [DelayedEvent("REFUND", O0 + timedelta(days=1), "x6", 10.0)]
        self.assertIn("REFUND_AMOUNT_UNSCALED", resolve_v1(inp(events=ev2), RewardPolicy(amount_unit=None)).flags)

    def test_missing_data_sources_are_flagged_not_invented(self):
        r = resolve_v1(inp(), P)
        for k in ("CHARGEBACK", "REFUND", "COMPLAINT", "BLOCK"):
            self.assertIn(k + "_DATA_UNAVAILABLE", r.flags)
        self.assertEqual(r.delayed_window["sources_available"], {k: False for k in R.NEGATIVE_KINDS})
        self.assertEqual(r.negative_adjustments, ())

    def test_delayed_window_30_days(self):
        self.assertEqual(R.DELAYED_WINDOW_SECONDS, 30 * 86400)
        r = resolve_v1(inp(now=O0 + timedelta(days=3)), P)
        self.assertEqual((r.status, r.delayed_window["seconds"]), ("provisional", 2592000))
        self.assertEqual(r.delayed_window["closes_at"], (O0 + timedelta(days=30)).isoformat())
        self.assertIn("DELAYED_NEGATIVE_WINDOW_OPEN", r.flags)
        self.assertEqual(resolve_v1(inp(now=O0 + timedelta(days=31)), P).status, "final")
        self.assertEqual(resolve_v1(inp("CONTINUED", now=O0 + timedelta(days=1)), P).status, "final")   # not money
        late = resolve_v1(inp(events=[DelayedEvent("CHARGEBACK", O0 + timedelta(days=45), "late", 100.0)]), P)
        self.assertEqual(late.reward, 1.0); self.assertIn("DELAYED_EVENT_OUTSIDE_WINDOW:CHARGEBACK", late.flags)

    def test_undefined_base_and_unknown_outcome(self):
        r = resolve_v1(inp("REGISTRATION"), P)
        self.assertIsNone(r.reward); self.assertIn("BASE_REWARD_UNDEFINED:REGISTRATION", r.flags)
        self.assertIsNone(resolve_v1(inp("UNKNOWN"), P).reward)

    def test_deterministic_and_pure(self):
        i = inp(touches=5, events=[DelayedEvent("COMPLAINT", O0 + timedelta(days=2), "c1")])
        self.assertEqual(resolve_v1(i, P), resolve_v1(i, P))
        self.assertEqual(resolve_v1(i, P).to_dict(), resolve_v1(copy.deepcopy(i), P).to_dict())

    def test_params_are_reproducible_from_the_record(self):
        d = resolve_v1(inp(touches=5), P).to_dict()
        rebuilt = RewardPolicy(**{k: v for k, v in d["params"].items()})
        self.assertEqual(rebuilt.params_id(), d["params_id"])
        self.assertNotEqual(P.params_id(), RewardPolicy(decay=0.8, max_touches_before_penalty=3, penalty_per_extra_touch=0.05,
                                                        amount_unit=100.0, complaint_penalty=0.3, block_penalty=0.4,
                                                        params_confirmed=True).params_id())

    def test_invalid_policy_rejected(self):
        for kw in ({"decay": 0}, {"decay": 1.5}, {"penalty_per_extra_touch": -1}, {"amount_unit": 0},
                   {"max_touches_before_penalty": -1}, {"base_rewards": {"DEPOSIT": 5}}, {"base_rewards": {"UNKNOWN": 0.1}},
                   {"chargeback_multiplier": -1}):
            with self.assertRaises(ValueError):
                RewardPolicy(**kw)


class TestDelayedCorrection(unittest.TestCase):
    def test_correction_never_edits_the_original(self):
        orig = {**resolve_v1(inp(now=O0 + timedelta(days=1)), P).to_dict(), "outcome_at": O0.isoformat()}
        snap = copy.deepcopy(orig)
        ev = [DelayedEvent("CHARGEBACK", O0 + timedelta(days=12), "cb1", 40.0)]
        c = correct_v1(orig, ev, P)
        self.assertEqual(orig, snap)
        self.assertEqual((c["delta"], c["corrected_reward"], c["event_ids"]), (-0.4, 0.6, ["cb1"]))
        self.assertIsNone(correct_v1(orig, ev, P, applied_event_ids=["cb1"]))             # idempotent
        self.assertIsNone(correct_v1(orig, [], P))
        late = [DelayedEvent("CHARGEBACK", O0 + timedelta(days=40), "cb2", 40.0)]
        self.assertEqual(correct_v1(orig, late, P)["delta"], 0.0)                          # outside the 30-day window

    def test_policy_change_is_a_new_params_id_not_a_silent_rewrite(self):
        orig = {**resolve_v1(inp(), P).to_dict(), "outcome_at": O0.isoformat()}
        p2 = RewardPolicy(amount_unit=100.0, chargeback_multiplier=2.0, params_confirmed=True)
        c = correct_v1(orig, [DelayedEvent("REFUND", O0 + timedelta(days=2), "r1", 10.0)], p2)
        self.assertNotEqual(c["params_id"], orig["params_id"]); self.assertEqual(c["supersedes_params_id"], orig["params_id"])


class TestStoreCorrection(Base):
    async def test_set_once_and_append_only_corrections(self):
        self.sched(msg=1); await self.drain()
        async with self.sf() as s:
            did = (await s.execute(select(store.shadow_decisions.c.decision_id))).scalar()
        d = resolve_v1(inp(now=O0 + timedelta(days=1)), P).to_dict()
        self.assertTrue(await store.set_reward(self.sf, did, d["reward"], d["reward_version"], "x", d))
        self.assertFalse(await store.set_reward(self.sf, did, 0.0, "reward.v1", "other", {}))    # never overwritten
        orig = {**d, "outcome_at": O0.isoformat()}
        c = correct_v1(orig, [DelayedEvent("CHARGEBACK", O0 + timedelta(days=5), "cb1", 100.0)], P)
        self.assertTrue(await store.add_reward_correction(self.sf, did, c))
        self.assertFalse(await store.add_reward_correction(self.sf, did, c))                      # idempotent per event
        self.assertFalse(await store.add_reward_correction(self.sf, "missing", c))
        async with self.sf() as s:
            r = (await s.execute(select(store.shadow_decisions).where(store.shadow_decisions.c.decision_id == did))).mappings().one()
        self.assertEqual(r["reward"], 1.0)                                                       # original untouched
        self.assertEqual(len(r["reward_corrections"]), 1); self.assertEqual(r["reward_corrections"][0]["corrected_reward"], 0.0)
        with self.assertRaises(ValueError):
            await store.set_reward(self.sf, did, 1.5, "v")


class TestBanditAcceptsReward(unittest.TestCase):
    def test_negative_reward_within_v1_range(self):
        b = LinUCBBackend(["A_ACT"], 3, reward_range=(-1.0, 1.0))
        b.recommend([1, 0, 1], ["A_ACT"], "d1")
        self.assertTrue(b.update("d1", -0.5))
        b.recommend([1, 0, 1], ["A_ACT"], "d2")
        self.assertFalse(b.update("d2", -1.5))
        c = LinUCBBackend(["A_ACT"], 3)                                                           # default range unchanged
        c.recommend([1, 0, 1], ["A_ACT"], "d3"); self.assertFalse(c.update("d3", -0.5))
