import re
import unittest

import language_layer as L
from language_layer import registry
from language_layer.antitranslationese import validate
from language_layer.profiles._es_base import ES_BASE

CODES = ["es-MX", "es-CO", "es-PE", "es-EC", "pt-BR"]


class ProfilesTest(unittest.TestCase):
    def test_all_required_profiles_registered(self):
        for c in CODES:
            self.assertIsNotNone(registry.get(c), c)

    def test_profiles_define_all_required_sections(self):
        for c in CODES:
            p = registry.get(c)
            with self.subTest(c):
                self.assertTrue(p.vocabulary, "vocabulary")
                self.assertTrue(p.informal_pronoun and p.formal_pronoun, "pronouns")
                self.assertTrue(p.address_rule, "address_rule")
                self.assertTrue(p.patterns, "patterns")
                self.assertTrue(p.forbidden, "forbidden")
                self.assertTrue(p.cta and all(p.cta.values()), "cta")
                self.assertTrue(p.tone_rules, "tone_rules")
                self.assertGreaterEqual(len(p.examples), 3, "examples")
                self.assertTrue(p.lexical_markers, "lexical_markers")
                for r in p.forbidden:
                    re.compile(r.pattern)

    def test_own_examples_and_cta_are_not_flagged(self):
        for c in CODES:
            p = registry.get(c)
            texts = list(p.examples) + [x for v in p.cta.values() for x in v]
            for t in texts:
                with self.subTest(c=c, t=t):
                    self.assertEqual(validate(t, p), [])

    def test_languages(self):
        self.assertEqual({c: registry.get(c).language for c in CODES},
                         {"es-MX": "es", "es-CO": "es", "es-PE": "es", "es-EC": "es", "pt-BR": "pt"})

    def test_new_geo_without_touching_core(self):
        ar = registry.register(ES_BASE.derive(code="es-AR", label="Español rioplatense", country_codes=("AR", "ARGENTINA"),
                                              informal_pronoun="vos", prompt_override=True,
                                              lexical_markers={"che": 0.6, "boludo": 1.0}))
        uy = registry.register(ar.derive(code="es-UY", label="Español de Uruguay", country_codes=("UY",)))
        try:
            self.assertEqual(registry.code_for_country("ar"), "es-AR")
            self.assertEqual(uy.informal_pronoun, "vos")          # inherited
            self.assertEqual(uy.forbidden, ar.forbidden)
            r = L.localize(L.LocalizationRequest("Hola che", declared_profile={"region": "UY"}))
            self.assertEqual(r.geo_profile, "es-UY")
        finally:
            registry._PROFILES.pop("es-AR", None); registry._PROFILES.pop("es-UY", None)
            registry._COUNTRY_INDEX.pop("AR", None); registry._COUNTRY_INDEX.pop("ARGENTINA", None); registry._COUNTRY_INDEX.pop("UY", None)


if __name__ == "__main__":
    unittest.main()
