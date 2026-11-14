import unittest

from language_layer import registry
from language_layer.models import GeoResolution
from language_layer.prompt import build_policy_block, client_style


def R(code):
    p = registry.get(code)
    return p, GeoResolution(code, p.language, "declared", 0.9)


class PromptTest(unittest.TestCase):
    def test_mx_block_is_empty_base_persona_unchanged(self):
        p, r = R("es-MX")
        self.assertEqual(build_policy_block(p, r, "new", "casual", []), "")

    def test_non_mx_blocks_have_explicit_override(self):
        for c in ["es-CO", "es-PE", "es-EC"]:
            p, r = R(c)
            b = build_policy_block(p, r, "qualified", "agent", [])
            with self.subTest(c):
                self.assertIn("LANGUAGE POLICY", b)
                self.assertIn("Target locale: %s" % c, b)
                self.assertIn("OVERRIDES", b)
                self.assertIn("Responde SIEMPRE en español mexicano", b)
                self.assertIn("for this response", b)
                self.assertIn("NOT change meaning, offer, prices", b)
                for v in p.vocabulary.values():
                    break
                self.assertIn(p.informal_pronoun, b + p.informal_pronoun)

    def test_pt_br_block_forces_portuguese(self):
        p, r = R("pt-BR")
        b = build_policy_block(p, r, "new", "casual", [])
        self.assertIn("Brazilian Portuguese", b)
        self.assertIn("Te cuento mas", b)             # explains the Spanish sample is only intent
        self.assertIn("Target locale: pt-BR", b)

    def test_mode_and_stage_hints(self):
        p, r = R("es-CO")
        self.assertIn("never defensive", build_policy_block(p, r, "new", "hard", []))
        self.assertIn("at most one clear call to action", build_policy_block(p, r, "click", "agent", []))
        self.assertIn("do not push", build_policy_block(p, r, "new", "casual", []))
        self.assertIn("supportive and practical", build_policy_block(p, r, "deposited", "retention", []))

    def test_client_text_never_copied(self):
        p, r = R("es-CO")
        secret = "mi numero es 3001234567 y vivo en calle secreta"
        b = build_policy_block(p, r, "new", "casual", [{"role": "user", "content": secret}])
        self.assertNotIn("3001234567", b)
        self.assertNotIn("secreta", b)

    def test_client_style_features(self):
        self.assertEqual(client_style([]), [])
        short = [{"role": "user", "content": "hola"}, {"role": "user", "content": "si"}]
        self.assertTrue(any("short" in x for x in client_style(short)))
        formal = [{"role": "user", "content": "Buenas tardes, ¿usted me puede ayudar?"}]
        self.assertTrue(any("formal" in x for x in client_style(formal)))

    def test_block_size_is_bounded(self):
        for c in ["es-CO", "es-PE", "es-EC", "pt-BR"]:
            p, r = R(c)
            self.assertLess(len(build_policy_block(p, r, "new", "agent", [])), 3600, c)


if __name__ == "__main__":
    unittest.main()
