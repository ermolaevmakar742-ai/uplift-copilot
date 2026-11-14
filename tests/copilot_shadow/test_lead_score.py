import copy
import unittest

from copilot_shadow import lead_score as L


def inp(**k):
    base = {"stage": "conversation", "intent": "ready", "sentiment": 0.7,
            "reply_classification": {"classification": "POSITIVE", "confidence": 0.85},
            "momentum": {"score": 0.5, "direction": "RISING", "user_turns_seen": 3},
            "risk": {}, "deposit_reported": False, "payment_signal": False, "contradiction": {"detected": False},
            "canonical_state": {}, "user_turns": 3}
    base.update(k)
    return base


class TestLeadScore(unittest.TestCase):
    def test_range_and_shape(self):
        r = L.score(inp())
        self.assertTrue(0 <= r["lead_score"] <= 100)
        self.assertEqual(set(r), {"lead_score", "score_band", "score_components", "reason_codes", "scorer_version"})
        self.assertEqual(r["scorer_version"], L.SCORER_VERSION)
        self.assertEqual(L.score({})["lead_score"], int(L.score({})["lead_score"]))

    def test_deterministic_and_input_not_mutated(self):
        i = inp(); snap = copy.deepcopy(i)
        self.assertEqual(L.score(i), L.score(i))
        self.assertEqual(i, snap)

    def test_components_sum_to_score(self):
        r = L.score(inp())
        self.assertEqual(r["lead_score"], int(round(sum(r["score_components"].values()))))
        self.assertEqual(r["score_components"]["stage"], 8)           # conversation
        self.assertEqual(r["score_components"]["intent"], 10)
        self.assertAlmostEqual(r["score_components"]["reply"], 15 * 0.85, delta=0.06)

    def test_reason_codes(self):
        r = L.score(inp(deposit_reported=True))
        for c in ("stage:conversation", "intent:ready", "reply:positive", "momentum:rising", "deposit_reported"):
            self.assertIn(c, r["reason_codes"])

    def test_edge_cases(self):
        self.assertEqual(L.score({})["lead_score"], 0)                       # no data -> 0, never invented
        self.assertEqual(L.score({})["score_band"], "COLD")
        self.assertIn("sentiment_unavailable", L.score({})["reason_codes"])
        self.assertIn("stage_unmapped", L.score({"stage": "weird"})["reason_codes"])
        top = L.score(inp(stage="deposited", deposit_reported=True, sentiment=1.0, momentum={"score": 1, "user_turns_seen": 9},
                          user_turns=9, canonical_state={"a": {"status": "resolved", "confidence": 1.0}}))
        self.assertEqual(top["lead_score"], 100)
        self.assertEqual(top["score_band"], "HOT")
        weird = L.score(inp(sentiment=7, momentum={"score": -9}, user_turns=True, stage=object()))
        self.assertTrue(0 <= weird["lead_score"] <= 100)

    def test_risk_excludes(self):
        r = L.score(inp(risk={"minor": True}))
        self.assertEqual((r["lead_score"], r["score_band"]), (0, "EXCLUDED"))
        self.assertIn("risk_excluded", r["reason_codes"])

    def test_contradictory_signals_lower_score(self):
        base = L.score(inp())["lead_score"]
        ctr = L.score(inp(contradiction={"detected": True}))
        self.assertEqual(ctr["lead_score"], base - L.CONTRADICTION_PENALTY)
        self.assertIn("contradiction_detected", ctr["reason_codes"])
        conflict = {"region": {"status": "unresolved", "contradiction": True}}
        self.assertEqual(L.score(inp(canonical_state=conflict))["lead_score"], base - L.CANONICAL_CONFLICT_PENALTY)
        # not interested + high stage: signals disagree, score stays bounded and below a clean positive lead
        mixed = L.score(inp(stage="offer", reply_classification={"classification": "NOT_INTERESTED", "confidence": 0.9}))
        clean = L.score(inp(stage="offer"))
        self.assertLess(mixed["lead_score"], clean["lead_score"])

    def test_bands(self):
        self.assertEqual([L.band(x) for x in (0, 24, 25, 49, 50, 74, 75, 100)],
                         ["COLD", "COLD", "COOL", "COOL", "WARM", "WARM", "HOT", "HOT"])
