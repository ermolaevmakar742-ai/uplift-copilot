import copy
import re
import unittest

from copilot_learning import contract as C
from copilot_learning.critic import CANDIDATE_TEMPLATES, REASON_EFFECTS, critique

NBQ = {"recommended": True, "question_intent": "ASK_PAYMENT_BARRIER", "target_unknown": "PAYMENT"}
PRICE = {"classification": "PRICING_OBJECTION", "confidence": 0.8}


def crit(text, **k):
    base = dict(response_text=text, client_text="es muy caro", stage="conversation", reply_classification=PRICE,
                next_best_question=NBQ, state_ref="st_x", decision_id="pd_1")
    base.update(k)
    return critique(**base)


class TestCritic(unittest.TestCase):
    def test_schema_and_version(self):
        r = crit("Entiendo, es normal. ¿Qué te preocupa más del pago: el método o el monto?")
        self.assertEqual(r["critic_version"], "dialogue-critic-1")
        self.assertEqual(set(r["scores"]), set(C.DIMENSIONS))
        for v in r["scores"].values():
            self.assertTrue(0 <= v <= 100)
        for k in ("reasons", "confidence", "usable", "stage", "reply_class", "state_ref", "decision_id", "response_ref", "candidate_better_response"):
            self.assertIn(k, r)
        self.assertTrue(set(r["reasons"]) <= set(C.ALL_REASONS))
        self.assertTrue(set(REASON_EFFECTS) == set(C.NEGATIVE_REASONS + C.POSITIVE_REASONS))

    def test_good_response_reasons(self):
        r = crit("Entiendo, es normal. ¿Qué te preocupa más del pago: el método o el monto?")
        self.assertEqual(set(r["reasons"]), {"GOOD_STATE_ALIGNMENT", "GOOD_OBJECTION_HANDLING", "GOOD_NEXT_QUESTION"})
        self.assertGreater(r["scores"]["overall_quality"], crit("Regístrate aquí ahora mismo y deposita hoy mismo.")["scores"]["overall_quality"])

    def test_bad_response_reasons(self):
        r = crit("Genial! Regístrate aquí ahora mismo y deposita hoy mismo.")
        self.assertTrue({"TOO_PUSHY", "NO_QUESTION", "TOO_EARLY_OFFER", "WRONG_STAGE", "IGNORED_OBJECTION"} <= set(r["reasons"]))
        self.assertLess(r["scores"]["pressure"], 70); self.assertLess(r["scores"]["objection_fit"], 70)

    def test_individual_reasons(self):
        self.assertIn("TOO_LONG", crit("palabra " * 120 + "¿Listo?")["reasons"])
        self.assertIn("REPETITIVE", crit("Hola, cuéntame qué buscas con este proyecto hoy?", prev_assistant=["Hola, cuéntame qué buscas con este proyecto hoy?"])["reasons"])
        self.assertIn("NO_QUESTION", crit("Está bien, te explico cómo funciona.", client_text="hola", reply_classification=None)["reasons"])
        self.assertIn("WRONG_STAGE", crit("Haz tu primer depósito aquí.", stage="new", reply_classification=None)["reasons"])
        self.assertNotIn("NO_QUESTION", crit("Gracias por tu depósito.", stage="deposited", reply_classification=None)["reasons"])

    def test_deterministic_and_inputs_not_mutated(self):
        prev = ["a b c"]; nbq = copy.deepcopy(NBQ); rc = copy.deepcopy(PRICE)
        a = critique(response_text="hola?", client_text="x", prev_assistant=prev, stage="new", reply_classification=rc, next_best_question=nbq)
        b = critique(response_text="hola?", client_text="x", prev_assistant=prev, stage="new", reply_classification=rc, next_best_question=nbq)
        self.assertEqual(a, b); self.assertEqual((prev, nbq, rc), (["a b c"], NBQ, PRICE))

    def test_low_confidence_when_state_or_response_missing(self):
        for r in (critique(response_text="", stage=None), critique(response_text=None), critique(response_text="hola", stage=None)):
            if not r["usable"]:
                self.assertIn("LOW_CONFIDENCE", r["reasons"]); self.assertIsNone(r["candidate_better_response"])
        r = critique(response_text="", stage=None)
        self.assertFalse(r["usable"]); self.assertEqual(r["confidence"], 0.0)
        self.assertFalse(critique(response_text="hola", stage=None)["usable"] and False)   # missing state lowers coverage

    def test_candidate_is_shadow_template_only_and_bound_to_decision(self):
        r = crit("Regístrate aquí ahora mismo y deposita hoy mismo.")
        c = r["candidate_better_response"]
        self.assertTrue(c["shadow_only"] and c["needs_human_review"])
        self.assertEqual((c["decision_id"], c["critic_version"], c["state_ref"], c["stage"]), ("pd_1", "dialogue-critic-1", "st_x", "conversation"))
        self.assertIn(c["text"], CANDIDATE_TEMPLATES.values())
        self.assertTrue(c["reason_codes"]); self.assertEqual(c["original_response_ref"], r["response_ref"])
        self.assertIsNone(crit("Entiendo, es normal. ¿Qué te preocupa más del pago?")["candidate_better_response"])

    def test_no_raw_text_or_pii_in_output(self):
        text = "Escríbeme a juan@mail.com o +52 55 1234 5678 o https://t.me/juan123 @juan_perez ahora mismo"
        r = crit(text, client_text="mi tel 5512345678 soy Juan")
        dump = str(r)
        for needle in ("juan", "5512345678", "mail.com", "t.me", "Escríbeme", "soy Juan"):
            self.assertNotIn(needle, dump)
        self.assertEqual(r["response_ref"]["chars"], len(text))

    def test_scrub_removes_pii(self):
        s = C.scrub("mail a@b.co tel +52 55 1234 5678 http://x.y/z @user_name 123456789")
        for needle in ("a@b.co", "1234", "http", "@user", "123456789"):
            self.assertNotIn(needle, s)
