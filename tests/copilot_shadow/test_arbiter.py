import unittest
from datetime import datetime, timedelta, timezone

from copilot_shadow.arbiter import EvidenceItem as E, arbitrate, arbitrate_state, evidence_from_client_state
from types import SimpleNamespace

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


class TestArbiter(unittest.TestCase):
    def test_agreeing_ai_and_external(self):
        d = arbitrate("region", [E("a", "ai", "MX", 0.7, NOW, 0.5), E("o", "external", "mx", 0.7, NOW, 0.6, source_id="p1")], NOW)
        self.assertEqual((d.status, d.value, d.contradiction), ("resolved", "MX", False))
        self.assertEqual(sorted(d.supporting), ["a", "o"])
        self.assertEqual(d.reason, "independent_agreement")

    def test_ai_vs_external_contradiction_is_unresolved_and_visible(self):
        d = arbitrate("region", [E("a", "ai", "MX", 0.8, NOW, 0.7), E("o", "external", "AR", 0.8, NOW, 0.7)], NOW)
        self.assertEqual(d.status, "unresolved")
        self.assertIsNone(d.value)
        self.assertTrue(d.contradiction)
        self.assertEqual(sorted(d.supporting + d.opposing), ["a", "o"])
        self.assertEqual(len(d.alternatives), 2)

    def test_no_winner_by_source_type(self):
        a = arbitrate("x", [E("a", "ai", 1, 0.9, NOW, 0.9), E("o", "external", 2, 0.9, NOW, 0.9)], NOW)
        b = arbitrate("x", [E("a", "external", 1, 0.9, NOW, 0.9), E("o", "ai", 2, 0.9, NOW, 0.9)], NOW)
        self.assertEqual((a.status, b.status), ("unresolved", "unresolved"))

    def test_multiple_independent_external_agree_beats_single_dissent(self):
        items = [E(f"o{i}", "external", "MX", 0.6, NOW, 0.5, source_id=f"p{i}") for i in range(3)]
        items.append(E("ai", "ai", "AR", 0.3, NOW, 0.3))
        d = arbitrate("region", items, NOW)
        self.assertEqual((d.status, d.value, d.contradiction), ("resolved", "MX", True))
        single = arbitrate("region", items[:1], NOW)
        self.assertGreater(arbitrate("region", items[:3], NOW).confidence, single.confidence)

    def test_stale_vs_fresh(self):
        old = E("old", "external", "AR", 0.9, NOW - timedelta(days=60), 0.8)
        new = E("new", "telegram", "MX", 0.8, NOW, 0.8)
        d = arbitrate("region", [old, new], NOW)
        self.assertEqual((d.status, d.value), ("resolved", "MX"))
        self.assertEqual(d.opposing, ["old"])

    def test_no_evidence(self):
        for items in (None, [], [E("n", "ai", None)]):
            d = arbitrate("stage", items, NOW)
            self.assertEqual((d.status, d.value, d.confidence, d.contradiction), ("no_evidence", None, 0.0, False))

    def test_deterministic_and_order_independent(self):
        items = [E("a", "ai", "x", 0.6, NOW), E("b", "external", "y", 0.5, NOW), E("c", "telegram", "x", 0.7, NOW)]
        self.assertEqual(arbitrate("f", items, NOW).to_dict(), arbitrate("f", items[::-1], NOW).to_dict())

