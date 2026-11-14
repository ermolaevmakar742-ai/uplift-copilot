import unittest

from copilot_shadow import reply_class as R

CASES = {
    "POSITIVE": "quiero empezar ya",
    "MEETING": "podemos agendar una videollamada",
    "PRICING_OBJECTION": "esta muy caro, no tengo dinero",
    "TECHNICAL_OBJECTION": "no funciona la app",
    "NOT_INTERESTED": "no gracias, no me interesa",
    "NOT_NOW": "luego te aviso",
    "REFERRAL": "mi amigo quiere entrar tambien",
    "QUESTION": "como funciona?",
    "SUPPORT": "necesito ayuda",
    "PAYMENT_PROBLEM": "ya hice el pago pero no se acredito",
    "TRUST_CONCERN": "es una estafa?",
    "OTHER": "hmm",
}


class TestReplyClass(unittest.TestCase):
    def test_all_twelve_categories(self):
        self.assertEqual(set(CASES), set(R.CATEGORIES))
        for cat, text in CASES.items():
            self.assertEqual(R.classify(text)["classification"], cat, text)

    def test_contract_fields_and_no_text(self):
        r = R.classify("es una estafa? mi numero es 5512345678")
        self.assertEqual(set(r), {"classification", "objection_type", "confidence", "reason_codes",
                                  "classifier_version", "confidence_type"})
        self.assertNotIn("5512345678", str(r))
        self.assertTrue(0 <= r["confidence"] <= 1)
        self.assertEqual(r["classifier_version"], R.CLASSIFIER_VERSION)

    def test_objection_type(self):
        self.assertEqual(R.classify("muy caro")["objection_type"], "price")
        self.assertEqual(R.classify("no funciona")["objection_type"], "technical")
        self.assertEqual(R.classify("es estafa")["objection_type"], "trust")
        self.assertEqual(R.classify("luego")["objection_type"], "timing")
        self.assertIsNone(R.classify("quiero empezar")["objection_type"])
        # reply class and objection type are independent: price objection hidden behind a deferral
        r = R.classify("no tengo dinero, luego te aviso")
        self.assertEqual(r["objection_type"], "price")

    def test_low_confidence_falls_back_to_other(self):
        for text in ("", "   ", None, "zzz qqq"):
            r = R.classify(text)
            self.assertEqual(r["classification"], "OTHER")
            self.assertLess(r["confidence"], R.MIN_CONFIDENCE)
        # ambiguity penalty pushes a weak winner under the threshold
        r = R.classify("ok?")
        self.assertIn(r["classification"], ("OTHER", "QUESTION", "POSITIVE"))
        low = R._out("OTHER", None, 0.2, ["low_confidence"])
        self.assertEqual(low["classification"], "OTHER")

    def test_suppressed_reports_original_candidate_without_promoting_it(self):
        old = R.MIN_CONFIDENCE
        try:
            R.MIN_CONFIDENCE = 0.99
            r = R.classify("quiero empezar ya")
        finally:
            R.MIN_CONFIDENCE = old
        self.assertEqual(r["classification"], "OTHER")
        self.assertIn("suppressed:POSITIVE", r["reason_codes"])

    def test_deterministic(self):
        for text in CASES.values():
            self.assertEqual(R.classify(text), R.classify(text))

    def test_intent_is_independent(self):
        # a 'ready'-style message that is a price objection: class is about the reply, not the intent
        self.assertEqual(R.classify("si quiero pero es muy caro")["classification"], "PRICING_OBJECTION")
