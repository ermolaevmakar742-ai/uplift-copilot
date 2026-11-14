import math
import unittest

from copilot_bandit.backend import LinUCBBackend
from copilot_bandit.features import DIM

ACTS = ["A_ACT", "B_ACT", "C_ACT", "DO_NOTHING"]


def ctx(i):
    x = [0.0] * DIM
    x[i] = 1.0
    x[-1] = 1.0
    return x


def bk(**k):
    return LinUCBBackend(ACTS, DIM, baseline="DO_NOTHING", **k)


class TestLinUCB(unittest.TestCase):
    def test_deterministic_across_instances(self):
        runs = []
        for _ in range(2):
            b = bk()
            seq = []
            for i in range(30):
                r = b.recommend(ctx(i % 3), ACTS, f"d{i}")
                seq.append((r["action"], r["scores"]))
                b.update(f"d{i}", 1.0 if r["action"] == "B_ACT" else 0.0)
            runs.append(seq)
        self.assertEqual(runs[0], runs[1])

    def test_cold_start_prefers_baseline_and_is_flagged(self):
        r = bk().recommend(ctx(0), ACTS)
        self.assertEqual((r["status"], r["action"], r["propensity"], r["cold_start"]), ("ok", "DO_NOTHING", 1.0, True))

    def test_unknown_or_empty_available_actions(self):
        b = bk()
        for av in (None, [], ["NOPE"], [1, None]):
            r = b.recommend(ctx(0), av)
            self.assertEqual((r["status"], r["action"], r["propensity"]), ("no_available_actions", None, None))
        self.assertEqual(b.recommend(ctx(0), ["NOPE", "B_ACT"])["action"], "B_ACT")   # unknown ids ignored

    def test_one_action(self):
        r = bk().recommend(ctx(1), ["C_ACT"])
        self.assertEqual((r["action"], list(r["scores"])), ("C_ACT", ["C_ACT"]))

    def test_multiple_actions_learns_best_arm(self):
        b = bk(alpha=0.3)
        for i in range(120):
            a = ACTS[i % 4]
            x = ctx(i % 3)
            b.recommend(x, ACTS, f"d{i}")
            b._pending[f"d{i}"] = (b._x(x), a)             # log a uniformly random action, reward only for B_ACT
            b.update(f"d{i}", 1.0 if a == "B_ACT" else 0.0)
        self.assertEqual(b.recommend(ctx(0), ACTS)["action"], "B_ACT")
        self.assertFalse(b.recommend(ctx(0), ACTS)["cold_start"])

    def test_malformed_context(self):
        b = bk()
        for bad in (None, "abc", [1, 2], [float("nan")] * DIM, [None] * DIM, [[1]] * DIM, 5):
            self.assertEqual(b.recommend(bad, ACTS)["status"], "malformed_context")

    def test_missing_or_invalid_reward_never_learns(self):
        b = bk()
        b.recommend(ctx(0), ACTS, "d1")
        for bad in (None, float("nan"), -0.1, 1.5, "1", True):
            self.assertFalse(b.update("d1", bad))
        self.assertEqual(sum(b.n.values()), 0)
        self.assertFalse(b.update("unknown", 1.0))
        self.assertTrue(b.update("d1", 0.5))
        self.assertFalse(b.update("d1", 0.5))                 # one update per recommendation
        self.assertEqual(sum(b.n.values()), 1)

    def test_fit_skips_invalid_samples(self):
        b = bk()
        n = b.fit([(ctx(0), "A_ACT", 1.0), (ctx(0), "ZZZ", 1.0), ([1], "A_ACT", 1.0), (ctx(0), "A_ACT", None),
                   (ctx(0), "A_ACT", 2.0)])
        self.assertEqual(n, 1)
        self.assertTrue(all(math.isfinite(v) for v in b.recommend(ctx(0), ACTS)["scores"].values()))
