import unittest
from datetime import datetime, timedelta, timezone

from copilot_shadow import response_policy as P
from copilot_shadow import signals as S


def rec(**k):
    return P.recommend(k)


class TestResponsePolicy(unittest.TestCase):
    def test_question_responds(self):
        r = rec(last_client_question=True)
        self.assertEqual(r["response_policy"], "RESPOND")
        self.assertIn("client_question_requires_response", r["reason_codes"])
        self.assertIsNone(r["suggested_wait_seconds"])

    def test_buying_signal_and_momentum(self):
        r = rec(buying_signal=True, momentum={"score": 0.8})
        self.assertEqual(r["response_policy"], "RESPOND")
        self.assertTrue({"buying_signal", "high_momentum"} <= set(r["reason_codes"]))

    def test_wait_when_already_waiting(self):
        r = rec(waiting_for_client=True, last_client_question=True)
        self.assertEqual(r["response_policy"], "WAIT")
        self.assertIn("already_waiting_for_client", r["reason_codes"])
        self.assertGreater(r["suggested_wait_seconds"], 0)

    def test_frequency_and_recent(self):
        r = rec(recent_client_msgs=5, last_client_age_s=2)
        self.assertEqual(r["response_policy"], "WAIT")
        self.assertTrue({"message_frequency_too_high", "recent_client_message"} <= set(r["reason_codes"]))
        self.assertLessEqual(r["suggested_wait_seconds"], P.MAX_WAIT_S)

    def test_contradiction_and_low_info(self):
        self.assertIn("contradiction_requires_pause", rec(contradiction=True)["reason_codes"])
        self.assertIn("low_information_value", rec(last_client_words=1)["reason_codes"])

    def test_escalate_on_risk(self):
        self.assertEqual(rec(risk={"minor": True})["response_policy"], "ESCALATE_REVIEW")
        self.assertEqual(rec(manual_mode=True)["response_policy"], "ESCALATE_REVIEW")

    def test_default_and_shadow_flag(self):
        r = rec()
        self.assertEqual((r["response_policy"], r["shadow_only"]), ("RESPOND", True))
        self.assertEqual(rec(**{"last_client_question": True}), rec(last_client_question=True))

    def test_signals_from_turns_no_fake_recency(self):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        turns = [S.Turn("assistant", "te animas?", 1, t0), S.Turn("user", "si", 2, t0 + timedelta(seconds=5))]
        sig = P.signals_from_turns(S.annotate(turns))
        self.assertNotIn("last_client_age_s", sig)
        self.assertTrue(sig["last_valeria_asked"])
