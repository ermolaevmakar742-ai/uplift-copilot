import json
import unittest
from unittest import mock

import language_layer as L
from language_layer import service
from language_layer.config import LanguageConfig, reset_config_cache

OFF = LanguageConfig()
SHADOW = LanguageConfig(shadow=True)
ACTIVE = LanguageConfig(enabled=True)
ACTIVE_SHADOW = LanguageConfig(enabled=True, shadow=True)

SAMPLES = {
    "es-MX": ("MX", "Oye  , el depósito mínimo es $500 MXN!!!  Entra a https://casa.com/r?id=7 con BONO500"),
    "es-CO": ("CO", "Listo parce , son 20.000 COP en www.casa.com/co  ¿Le damos???"),
    "es-PE": ("PE", "Ya, depósito de S/ 50 nomás , escríbeme a @valeria_pe  "),
    "es-EC": ("EC", "Dime pues , son $10 USD , código PANA2024  facilito!!"),
    "pt-BR": ("BR", "Beleza , o depósito é R$ 50 , entra em https://casa.com.br/ref  topa???"),
}


class DefaultInertTest(unittest.TestCase):
    def test_flag_off_returns_same_object_and_logs_nothing(self):
        reply = "Hola  parce !!!"
        with self.assertNoLogs("bot.language", level="DEBUG"):
            out = L.process_reply(reply, user_ref=1, declared_profile={"region": "CO"}, cfg=OFF)
        self.assertIs(out, reply)

    def test_flag_off_prompt_identity(self):
        s = "SYSTEM PROMPT"
        self.assertIs(L.apply_prompt(s, "new", "casual", [], {"region": "CO"}, cfg=OFF), s)
        self.assertIs(L.apply_prompt(s, "new", "casual", [], {"region": "CO"}, cfg=SHADOW), s)   # shadow never edits prompt

    def test_default_config_from_env_is_off(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            c = LanguageConfig.from_env()
        self.assertEqual((c.enabled, c.shadow, c.repair, c.default_geo), (False, False, False, "es-MX"))

    def test_env_flags_parse(self):
        with mock.patch.dict("os.environ", {"LANGUAGE_LAYER": "true", "LANGUAGE_LAYER_SHADOW": "1", "LANGUAGE_LAYER_REPAIR": "no"}, clear=True):
            c = LanguageConfig.from_env()
        self.assertEqual((c.enabled, c.shadow, c.repair), (True, True, False))

    def test_cached_config_reset(self):
        reset_config_cache()
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(L.get_config().enabled)
        reset_config_cache()


class ShadowTest(unittest.TestCase):
    def test_shadow_logs_but_never_changes_client_output(self):
        reply = "Hola  parce !!!  $500"
        with self.assertLogs("bot.language", level="INFO") as cm:
            out = L.process_reply(reply, user_ref=42, stage="new", regime="casual", history=[],
                                  declared_profile={"region": "CO"}, cfg=SHADOW)
        self.assertIs(out, reply)
        line = cm.records[0].getMessage()
        self.assertTrue(line.startswith("LANGLAYER "))
        rec = json.loads(line[len("LANGLAYER "):])
        for k in ("original", "localized", "geo", "language", "confidence", "flags", "meaning_guard", "reverted", "mode"):
            self.assertIn(k, rec)
        self.assertEqual((rec["mode"], rec["geo"], rec["language"]), ("shadow", "es-CO", "es"))
        self.assertEqual(rec["localized"], "Hola parce! $500")
        self.assertEqual(rec["meaning_guard"]["ok"], True)
        self.assertNotIn("42", rec["user"])           # hashed

    def test_log_has_no_client_text(self):
        hist = [{"role": "user", "content": "mi clave secreta es zzz-777"}]
        with self.assertLogs("bot.language", level="INFO") as cm:
            L.process_reply("Listo parce", user_ref=1, history=hist, declared_profile={"region": "CO"}, cfg=SHADOW)
        self.assertNotIn("zzz-777", cm.output[0])

    def test_unchanged_reply_logs_localized_null(self):
        with self.assertLogs("bot.language", level="INFO") as cm:
            L.process_reply("Listo, miremos", user_ref=1, declared_profile={"region": "CO"}, cfg=SHADOW)
        rec = json.loads(cm.records[0].getMessage()[len("LANGLAYER "):])
        self.assertIsNone(rec["localized"])
        self.assertFalse(rec["changed"])


class ActiveTest(unittest.TestCase):
    def test_active_applies_only_safe_cleanup(self):
        out = L.process_reply("Hola  !!!  $500", user_ref=1, cfg=ACTIVE)
        self.assertEqual(out, "Hola! $500")

    def test_active_prompt_block_appended_after_system(self):
        s = "BASE"
        out = L.apply_prompt(s, "new", "casual", [], {"region": "CO"}, cfg=ACTIVE)
        self.assertTrue(out.startswith("BASE"))
        self.assertIn("Target locale: es-CO", out)
        self.assertTrue(out.rstrip().endswith("previous messages."))   # block is last -> highest priority

    def test_active_prompt_mx_unchanged(self):
        s = "BASE"
        self.assertEqual(L.apply_prompt(s, "new", "casual", [], {"region": "MX"}, cfg=ACTIVE), s)
        self.assertEqual(L.apply_prompt(s, "new", "casual", [], None, cfg=ACTIVE), s)

    def test_meaning_guard_reverts_corrupting_cleanup(self):
        with mock.patch.object(service, "safe_cleanup", lambda t: t.replace("500", "50")):
            r = L.localize(L.LocalizationRequest("Deposita $500 ya", declared_profile={"region": "CO"}), ACTIVE)
        self.assertTrue(r.reverted)
        self.assertEqual(r.text, "Deposita $500 ya")
        self.assertIn("meaning_guard_revert", r.flags)
        self.assertFalse(r.meaning_guard["ok"])
        with mock.patch.object(service, "safe_cleanup", lambda t: t.replace("500", "50")):
            out = L.process_reply("Deposita $500 ya", cfg=ACTIVE)
        self.assertEqual(out, "Deposita $500 ya")

    def test_internal_error_returns_input(self):
        with mock.patch.object(service, "localize", side_effect=RuntimeError("boom")):
            with self.assertLogs("bot.language", level="ERROR"):
                self.assertEqual(L.process_reply("Hola", cfg=ACTIVE), "Hola")
        with mock.patch.object(service, "resolve_geo", side_effect=RuntimeError("boom")):
            with self.assertLogs("bot.language", level="ERROR"):
                self.assertEqual(L.apply_prompt("S", cfg=ACTIVE), "S")

    def test_non_string_reply_passthrough(self):
        self.assertIsNone(L.process_reply(None, cfg=ACTIVE))


class PerGeoMeaningTest(unittest.TestCase):
    def test_every_geo_preserves_critical_tokens(self):
        from language_layer.guard import protected_tokens
        for code, (region, text) in SAMPLES.items():
            with self.subTest(code):
                r = L.localize(L.LocalizationRequest(text, "qualified", "agent", [], {"region": region}), ACTIVE)
                self.assertEqual(r.geo_profile, code)
                self.assertEqual(protected_tokens(r.original), protected_tokens(r.text))
                self.assertTrue(r.meaning_guard["ok"])
                self.assertFalse(r.reverted)
                self.assertEqual(r.language, "pt" if code == "pt-BR" else "es")
                self.assertEqual("?" in r.original, "?" in r.text)

    def test_no_llm_or_network_in_layer(self):
        import pathlib, re
        root = pathlib.Path(L.__file__).parent
        src = "\n".join(p.read_text() for p in root.rglob("*.py"))
        self.assertIsNone(re.search(r"^\s*(?:import|from)\s+(?:aiohttp|httpx|requests|openai|urllib\.request)", src, re.M))
        self.assertNotIn("llm.generate", src)


if __name__ == "__main__":
    unittest.main()
