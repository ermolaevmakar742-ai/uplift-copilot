import os
import unittest
from unittest import mock

from copilot_bandit import contract as C
from copilot_bandit.reward import NEUTRAL_VERSION, OUTCOME_TYPES, Resolver, resolve


class TestReward(unittest.TestCase):
    def test_neutral_version_resolves_nothing(self):
        for ot in OUTCOME_TYPES:
            r = resolve(ot)
            self.assertIsNone(r["reward"], ot)
            self.assertEqual(r["reward_version"], NEUTRAL_VERSION)
            self.assertTrue(r["reward_reason"])

    def test_continued_is_not_deposit(self):
        self.assertEqual(resolve("CONTINUED")["reward_reason"], "continued_is_not_a_conversion")
        self.assertNotEqual(resolve("CONTINUED"), resolve("DEPOSIT"))
        self.assertIsNone(resolve("CONTINUED")["reward"])

    def test_unknown_missing_or_garbage_outcome(self):
        for ot in (None, "UNKNOWN", "WHATEVER", 5):
            self.assertEqual(resolve(ot)["reward_reason"], "outcome_unknown")
            self.assertIsNone(resolve(ot)["reward"])

    def test_new_version_with_explicit_table_old_unchanged(self):
        v2 = Resolver({"DEPOSIT": 1.0, "REGISTRATION": 0.2}, "reward.v1-test")
        self.assertEqual(v2.resolve("DEPOSIT"), {"reward": 1.0, "reward_version": "reward.v1-test", "reward_reason": "table:DEPOSIT"})
        self.assertIsNone(v2.resolve("CONTINUED")["reward"])           # still not a conversion
        self.assertIsNone(resolve("DEPOSIT")["reward"])                # default (old) version untouched
        for bad in ({"DEPOSIT": 2}, {"UNKNOWN": 0.5}, {"NOPE": 0.5}, {"DEPOSIT": True}):
            with self.assertRaises(ValueError):
                Resolver(bad, "reward.vX")
        with self.assertRaises(ValueError):
            Resolver({"DEPOSIT": 1.0})                                  # a table needs its own version


class TestContract(unittest.TestCase):
    def test_flags_default_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(C.bandit_shadow_enabled()); self.assertFalse(C.bandit_enabled()); self.assertFalse(C.ope_enabled())
        with mock.patch.dict(os.environ, {"COPILOT_BANDIT_SHADOW_ENABLED": "true"}, clear=True):
            self.assertTrue(C.bandit_shadow_enabled()); self.assertFalse(C.bandit_enabled())

    def test_propensity_semantics(self):
        self.assertEqual(C.validate_propensity(None, "unknown"), (None, "unknown"))
        with self.assertRaises(ValueError):
            C.validate_propensity(1.0, "unknown")                       # no 0/1 placeholder for unknown
        with self.assertRaises(ValueError):
            C.validate_propensity(None, "known")
        with self.assertRaises(ValueError):
            C.validate_propensity(0.0, "known")
        with self.assertRaises(ValueError):
            C.validate_propensity(0.5, "bogus")

    def test_only_known_propensity_is_ope_usable(self):
        self.assertTrue(C.ope_usable({"production_propensity": 0.5, "production_propensity_source": "known"}))
        self.assertFalse(C.ope_usable({"production_propensity": 0.5, "production_propensity_source": "surrogate"}))
        self.assertFalse(C.ope_usable({"production_propensity": None, "production_propensity_source": "unknown"}))
        self.assertFalse(C.ope_usable({"production_propensity": 1.0, "production_propensity_source": "unknown"}))
        self.assertFalse(C.ope_usable({}))

    def test_observation_window_env(self):
        with mock.patch.dict(os.environ, {"COPILOT_SHADOW_OUTCOME_WINDOW_SECONDS": "120"}):
            self.assertEqual(C.observation_window(), 120)
        with mock.patch.dict(os.environ, {"COPILOT_SHADOW_OUTCOME_WINDOW_SECONDS": "x"}):
            self.assertEqual(C.observation_window(), 86400)
