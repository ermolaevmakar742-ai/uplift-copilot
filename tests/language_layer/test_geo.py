import unittest

from language_layer.config import LanguageConfig
from language_layer.geo import resolve_geo

CFG = LanguageConfig()


def U(*t):
    return [{"role": "user", "content": x} for x in t]


class GeoTest(unittest.TestCase):
    def test_external_regions(self):
        for region, code in [("MX", "es-MX"), ("CO", "es-CO"), ("PE", "es-PE"), ("EC", "es-EC"), ("BR", "pt-BR"),
                             ("colombia", "es-CO"), ("Perú", "es-PE"), ("brasil", "pt-BR")]:
            with self.subTest(region):
                r = resolve_geo({"region": region}, None, cfg=CFG)
                self.assertEqual((r.code, r.source), (code, "declared"))
                self.assertGreaterEqual(r.confidence, 0.9)

    def test_no_external_defaults_to_mx(self):
        for sig in (None, {}, {"region": "??"}, {"region": None}):
            r = resolve_geo(sig, None, cfg=CFG)
            self.assertEqual((r.code, r.source), ("es-MX", "default"))

    def test_unsupported_region_flagged_and_defaults(self):
        r = resolve_geo({"region": "AR"}, None, cfg=CFG)
        self.assertEqual(r.code, "es-MX")
        self.assertIn("geo_unsupported:AR", r.flags)

    def test_one_or_two_words_never_switch(self):
        self.assertEqual(resolve_geo(None, U("hola parce"), cfg=CFG).code, "es-MX")
        self.assertEqual(resolve_geo(None, U("hola parce", "bacano parce"), cfg=CFG).source, "default")

    def test_strong_lexical_evidence_switches_with_low_confidence(self):
        r = resolve_geo(None, U("hola parce", "eso está bacano", "parcero dale de una"), cfg=CFG)
        self.assertEqual((r.code, r.source), ("es-CO", "lexical"))
        self.assertLess(r.confidence, 0.9)
        self.assertGreaterEqual(r.confidence, 0.5)

    def test_external_beats_lexical(self):
        r = resolve_geo({"region": "PE"}, U("parce", "bacano", "parcero"), cfg=CFG)
        self.assertEqual((r.code, r.source), ("es-PE", "declared"))

    def test_portuguese_client_without_external(self):
        r = resolve_geo(None, U("oi, tudo bem?", "você pode me explicar? obrigado", "beleza, valeu"), cfg=CFG)
        self.assertEqual(r.code, "pt-BR")

    def test_mexican_and_colombian_mixed_stays_default(self):
        r = resolve_geo(None, U("parce", "órale güey"), cfg=CFG)
        self.assertEqual(r.source, "default")

    def test_stateless(self):
        h = U("parce", "bacano", "parcero")
        self.assertEqual(resolve_geo(None, h, cfg=CFG).code, "es-CO")
        self.assertEqual(resolve_geo(None, U("hola"), cfg=CFG).code, "es-MX")   # no memory of the previous call

    def test_only_client_messages_count(self):
        h = [{"role": "assistant", "content": "parce bacano parcero de una"}]
        self.assertEqual(resolve_geo(None, h, cfg=CFG).source, "default")

    def test_hint_wins(self):
        self.assertEqual(resolve_geo({"region": "MX"}, None, geo_hint="es-EC", cfg=CFG).code, "es-EC")


if __name__ == "__main__":
    unittest.main()
