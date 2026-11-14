import unittest

from language_layer import registry
from language_layer.antitranslationese import safe_cleanup, validate

MX, CO, PE, EC, BR = (registry.get(c) for c in ["es-MX", "es-CO", "es-PE", "es-EC", "pt-BR"])


class CleanupTest(unittest.TestCase):
    def test_hygiene(self):
        self.assertEqual(safe_cleanup("  Hola   amigo !!!  "), "Hola amigo!")
        self.assertEqual(safe_cleanup("En serio ???"), "En serio?")
        self.assertEqual(safe_cleanup("Pues.....ya"), "Pues...ya")
        self.assertEqual(safe_cleanup("a\r\n\r\n\r\n\r\nb"), "a\n\nb")

    def test_idempotent(self):
        t = safe_cleanup("Hola   !!")
        self.assertEqual(safe_cleanup(t), t)

    def test_does_not_rewrite_words(self):
        for t in ["Estimado cliente, no dude en escribirme", "A continuación te explico", "Hola tío, vale"]:
            self.assertEqual(safe_cleanup(t), t)          # flagged by validate, never rewritten

    def test_empty(self):
        self.assertEqual(safe_cleanup(""), "")
        self.assertIsNone(safe_cleanup(None))


class ValidateTest(unittest.TestCase):
    def test_corporate_and_spain(self):
        f = validate("Estimado cliente, no dude en escribirme. A continuación, vale.", CO)
        for x in ("forbidden:corp_estimado", "forbidden:corp_no_dude", "forbidden:corp_continuacion", "forbidden:spain_vale"):
            self.assertIn(x, f)

    def test_valid_vale_la_pena_not_flagged(self):
        self.assertNotIn("forbidden:spain_vale", validate("Sí vale la pena probarlo", MX))

    def test_calques(self):
        self.assertIn("forbidden:calque_sentido", validate("Eso hace sentido", PE))
        self.assertIn("forbidden:calque_llamar", validate("Te voy a llamar de vuelta", EC))

    def test_foreign_markers(self):
        self.assertIn("foreign_marker:es-MX", validate("Órale, qué bueno", CO))
        self.assertIn("foreign_marker:es-CO", validate("Hola parce, todo bien?", MX))
        self.assertIn("foreign_marker:es-MX", validate("no manches, qué chido", PE))
        self.assertEqual([f for f in validate("¿Qué más? Listo, miremos", CO) if f.startswith("foreign")], [])

    def test_language_mismatch(self):
        self.assertTrue(any(f.startswith("lang_mismatch:es->pt") for f in validate("Hola, ¿cómo estás? Quieres probar con el bono?", BR)))
        self.assertTrue(any(f.startswith("lang_mismatch:pt->es") for f in validate("Oi, você quer experimentar? Não tem pressa, tá bom", CO)))
        self.assertFalse(any(f.startswith("lang_mismatch") for f in validate("Oi, você quer experimentar? Tá bom, sem pressa", BR)))

    def test_pt_br_specific(self):
        f = validate("Tu estás aí? Tens tempo?", BR)
        self.assertIn("forbidden:pt_tu_forms", f)
        self.assertIn("forbidden:pt_spanish_leak", validate("¿Você quer?", BR))

    def test_over_polite(self):
        self.assertIn("over_polite", validate("Gracias, por favor, gracias, disculpa", MX))

    def test_repeat_template(self):
        h = [{"role": "assistant", "content": "Oye, todo bien? Cuéntame cómo vas."}]
        self.assertIn("repeat_template", validate("Oye, todo bien? Cuéntame cómo vas!", MX, h))
        h2 = [{"role": "assistant", "content": "Oye cómo vas con todo eso"}]
        self.assertIn("repeat_opening", validate("Oye cómo vas hoy amigo mío", MX, h2))
        self.assertEqual([f for f in validate("Listo, cuéntame más", MX, h) if f.startswith("repeat")], [])

    def test_validate_never_mutates(self):
        t = "Estimado cliente"
        validate(t, MX)
        self.assertEqual(t, "Estimado cliente")


if __name__ == "__main__":
    unittest.main()
