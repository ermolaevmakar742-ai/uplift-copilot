import ast
import asyncio
import copy
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sqlalchemy import select, text

from copilot_shadow import analysis, hook, store
from copilot_shadow.signals import Turn
from tests.copilot_shadow.test_shadow import Base, make_cs, make_user

PKG = Path(__file__).resolve().parents[2] / "copilot_shadow"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

CTA = "Perfecto, regístrate aquí http://x.co ¿lo hacemos?"
PAYCTA = "Genial, haz tu primer depósito ¿lo hacemos?"


def conv(*pairs, gap_min=1):
    """('u'|'a', text) -> Turns with ids and 1-minute timestamps."""
    out, ts = [], T0
    for i, (r, t) in enumerate(pairs, 1):
        out.append(Turn("user" if r == "u" else "assistant", t, id=i, ts=ts))
        ts = ts + timedelta(minutes=gap_min)
    return out


def run(turns, stage="conversation"):
    return analysis.analyze(turns, stage).to_dict()


class TestContradiction(unittest.TestCase):
    def test_1_high_intent_negative_behaviour(self):
        r = run(conv(("u", "Sí, quiero empezar"), ("a", CTA), ("u", "jaja qué clima hace hoy")))
        c = r["contradiction"]
        self.assertTrue(c["detected"])
        self.assertEqual(c["type"], "INTENT_BEHAVIOR_MISMATCH")
        self.assertEqual((c["intent_signal"], c["behavioral_commitment"]), ("HIGH", "LOW"))
        self.assertEqual(c["confidence_type"], "rule_based_coverage")
        self.assertEqual([e["dialog_id"] for e in c["evidence"]], [1, 3])
        self.assertTrue(all(e["ts"] for e in c["evidence"]))

    def test_1b_payment_variant_and_trust_variant(self):
        c = run(conv(("u", "quiero empezar"), ("a", PAYCTA), ("u", "mmm luego lo veo")))["contradiction"]
        self.assertEqual(c["type"], "PAYMENT_INTENT_MISMATCH")
        c = run(conv(("u", "quiero empezar"), ("a", CTA), ("u", "sí pero ¿es seguro? tengo miedo")))["contradiction"]
        self.assertEqual(c["type"], "TRUST_INTENT_MISMATCH")

    def test_2_consistent(self):
        c = run(conv(("u", "quiero empezar"), ("a", CTA), ("u", "listo, ¿cómo pago?")))["contradiction"]
        self.assertFalse(c["detected"])
        self.assertEqual(c["type"], "NONE")
        self.assertEqual(c["behavioral_commitment"], "HIGH")

    def test_no_intent_no_contradiction(self):
        c = run(conv(("u", "hola")))["contradiction"]
        self.assertFalse(c["detected"])
        self.assertEqual(c["intent_signal"], "NONE")


class TestMomentum(unittest.TestCase):
    def test_3_rising(self):
        m = run(conv(("u", "necesito dinero extra, ¿cómo funciona esto?"), ("a", CTA),
                     ("u", "ok, ¿cuánto es el mínimo para depositar?")))["momentum"]
        self.assertEqual(m["direction"], "RISING")
        self.assertGreater(m["score"], 0.2)
        self.assertIn("asked_payment_question", m["positive_signals"])
        self.assertIn("engaged_after_cta", m["positive_signals"])
        self.assertEqual(m["confidence_type"], "rule_based_coverage")

    def test_4_falling(self):
        m = run(conv(("u", "hola, me llamaron para un trabajo y quiero saber más detalles de todo"),
                     ("a", CTA), ("u", "mmm")))["momentum"]
        self.assertEqual(m["direction"], "FALLING")
        self.assertIn("ignored_cta", m["negative_signals"])
        self.assertIn("one_word_reply", m["negative_signals"])
        self.assertLessEqual(m["score"], -0.2)

    def test_5_no_evidence(self):
        m = run(conv(("u", "hola")))["momentum"]
        self.assertEqual(m["direction"], "STABLE")
        self.assertEqual(m["score"], 0.0)
        self.assertEqual(m["positive_signals"] + m["negative_signals"], [])
        self.assertLessEqual(m["confidence"], 0.25)

    def test_returned_after_pause_and_bounds(self):
        ts = conv(("u", "hola"), ("a", "hola, ¿qué tal?"), gap_min=1)
        ts.append(Turn("user", "ya vi lo que me mandaste, ¿cómo sigue?", id=3, ts=T0 + timedelta(hours=9)))
        m = run(ts)["momentum"]
        self.assertIn("returned_after_pause", m["positive_signals"])
        self.assertTrue(-1.0 <= m["score"] <= 1.0)


class TestNextQuestion(unittest.TestCase):
    def test_6_payment(self):
        n = run(conv(("u", "quiero empezar"), ("a", PAYCTA), ("u", "¿cómo pago? no tengo tarjeta")))["next_best_question"]
        self.assertTrue(n["recommended"])
        self.assertEqual(n["question_intent"], "ASK_PAYMENT_BARRIER")
        self.assertEqual(n["target_unknown"], "PAYMENT")
        self.assertTrue(n["reason"].startswith("rule:"))

    def test_7_trust(self):
        n = run(conv(("u", "¿es seguro? tengo miedo de que sea estafa")))["next_best_question"]
        self.assertEqual(n["question_intent"], "ASK_TRUST_CONCERN")
        self.assertEqual(n["target_unknown"], "TRUST")

    def test_8_insufficient(self):
        n = run(conv(("u", "hola")), stage="new")["next_best_question"]
        self.assertFalse(n["recommended"])
        self.assertEqual((n["question_intent"], n["target_unknown"]), ("NONE", "NONE"))

    def test_need_and_readiness(self):
        n = run(conv(("u", "¿cómo funciona esto?")))["next_best_question"]
        self.assertEqual(n["question_intent"], "ASK_NEED_CLARIFICATION")
        n = run(conv(("a", CTA), ("u", "luego te aviso")))["next_best_question"]
        self.assertEqual(n["question_intent"], "ASK_READINESS")

    def test_no_text_generated(self):
        n = run(conv(("u", "¿es seguro? tengo miedo")))["next_best_question"]
        self.assertNotIn("question_text", n)


class TestDepositGraph(unittest.TestCase):
    def test_9_interest_to_need(self):
        d = run(conv(("u", "¿cómo funciona esto?"), ("a", "te explico"), ("u", "necesito dinero extra")))["deposit_path"]
        self.assertEqual((d["current_node"], d["previous_node"]), ("NEED", "INTEREST"))
        self.assertIsNone(d["blocked_transition"])
        self.assertEqual(d["observed_transition"], "INTEREST→NEED")

    def test_10_payment_to_deposit_no_blocker(self):
        d = run(conv(("u", "¿cómo pago?"), ("a", "con tarjeta"), ("u", "listo, ya hice el pago")))["deposit_path"]
        self.assertEqual((d["current_node"], d["previous_node"]), ("DEPOSIT", "PAYMENT"))
        self.assertIn("SUCCESS", d["possible_next_nodes"])
        self.assertIsNone(d["blocked_transition"])
        d = run([], stage="offer")["deposit_path"]
        self.assertEqual(d["current_node"], "PAYMENT")
        self.assertEqual(d["possible_next_nodes"][0], "DEPOSIT")
        self.assertIsNone(d["blocked_transition"])

    def test_11_payment_to_trust_with_blocker(self):
        d = run(conv(("u", "¿cómo pago?"), ("a", "con tarjeta"), ("u", "¿es seguro? tengo miedo")))["deposit_path"]
        self.assertEqual((d["current_node"], d["previous_node"]), ("TRUST", "PAYMENT"))
        self.assertTrue(d["regression"])
        self.assertEqual(d["blocker"], "TRUST_CONCERN")
        self.assertEqual(d["blocked_transition"], "PAYMENT→DEPOSIT")
        self.assertGreater(d["blocker_confidence"], 0)
        self.assertTrue(d["evidence"])
        self.assertIn("PAYMENT", d["possible_next_nodes"])

    def test_12_no_fabricated_blocker(self):
        for turns, stage in ((conv(("u", "hola")), "new"), ([], None), ([], "offer")):
            d = run(turns, stage=stage)["deposit_path"]
            self.assertIsNone(d["blocked_transition"])
            self.assertIsNone(d["blocker"])
            self.assertIsNone(d["blocker_confidence"])

    def test_terminal_stage_from_production(self):
        self.assertEqual(run(conv(("u", "gracias")), stage="deposited")["deposit_path"]["current_node"], "SUCCESS")
        self.assertEqual(run(conv(("u", "gracias")), stage="lost")["deposit_path"]["current_node"], "DROPPED")


class TestAnalysisObject(unittest.TestCase):
    def test_json_and_no_raw_text(self):
        secret = "mi nombre es Juan Perez y vivo en la calle Falsa 123, necesito dinero extra"
        r = run(conv(("u", secret), ("a", CTA), ("u", "quiero empezar pero ¿es seguro?")))
        blob = json.dumps(r)
        for frag in ("Juan", "Falsa", "calle", "quiero empezar", "es seguro"):
            self.assertNotIn(frag, blob)
        self.assertEqual(set(r), {"contradiction", "momentum", "next_best_question", "deposit_path", "meta"})

    def test_module_isolation(self):
        with mock.patch.object(analysis.momentum, "compute", side_effect=RuntimeError("x")):
            r = run(conv(("u", "¿es seguro? tengo miedo")))
        self.assertEqual(r["momentum"], {"error": "RuntimeError"})
        self.assertEqual(r["meta"]["errors"], {"momentum": "RuntimeError"})
        self.assertEqual(r["next_best_question"]["target_unknown"], "TRUST")
        self.assertEqual(r["deposit_path"]["current_node"], "TRUST")


class TestIntegration(Base):
    async def seed(self, rows):
        async with self.sf() as s:
            await s.execute(text("CREATE TABLE dialogs (id INTEGER PRIMARY KEY, user_id BIGINT, "
                                 "role TEXT, content TEXT, created_at TEXT)"))
            for i, (r, c) in enumerate(rows, 1):
                await s.execute(text("INSERT INTO dialogs VALUES (:i,111,:r,:c,:t)"),
                                {"i": i, "r": r, "c": c, "t": (T0 + timedelta(minutes=i)).isoformat()})
            await s.commit()

    ROWS = [("user", "quiero empezar"), ("assistant", PAYCTA), ("user", "¿cómo pago? no tengo tarjeta"),
            ("assistant", "RESPUESTA YA ENVIADA")]

    def hist(self):
        return [{"role": r, "content": c} for r, c in self.ROWS[:3]]

    async def fetch(self):
        async with self.sf() as s:
            return (await s.execute(select(store.shadow_decisions))).mappings().one()

    async def test_13_all_four_in_record(self):
        await self.seed(self.ROWS)
        self.assertTrue(self.sched(msg=7, history=self.hist()))
        await self.drain()
        row = await self.fetch()
        for col in ("contradiction", "momentum", "next_best_question", "deposit_path"):
            self.assertIsInstance(row[col], dict, col)
            self.assertNotIn("error", row[col])
        self.assertEqual(row["next_best_question"]["question_intent"], "ASK_PAYMENT_BARRIER")
        self.assertEqual(row["deposit_path"]["current_node"], "PAYMENT")
        meta = row["state_snapshot"]["analysis_meta"]
        self.assertTrue(meta["ids_available"])
        self.assertEqual(meta["turns_used"], 3)          # the already-sent reply is excluded
        blob = json.dumps([row[c] for c in ("contradiction", "momentum", "next_best_question", "deposit_path")])
        self.assertNotIn("no tengo tarjeta", blob)
        self.assertNotIn("RESPUESTA YA ENVIADA", blob)
        self.assertEqual(row["chosen_action"], "CONTINUE_DIALOGUE")   # selection rule unchanged
        self.assertEqual(row["decision_status"], "pre_uplift")

    async def test_13b_history_fallback_without_dialogs_table(self):
        self.assertTrue(self.sched(msg=8, history=self.hist()))
        await self.drain()
        row = await self.fetch()
        self.assertFalse(row["state_snapshot"]["analysis_meta"]["ids_available"])
        self.assertEqual(row["deposit_path"]["current_node"], "PAYMENT")

    async def test_14_15_production_unchanged(self):
        await self.seed(self.ROWS)
        user, cs, hist = make_user(), make_cs(), self.hist()
        reply = "respuesta de produccion"
        before = copy.deepcopy((vars(user), cs, hist, reply))
        self.sched(msg=9, user=user, client_state=cs, history=hist, reply_text=reply)
        await self.drain()
        self.assertEqual(before, (vars(user), cs, hist, reply))
        async with self.sf() as s:   # production table untouched
            n = (await s.execute(text("SELECT count(*) FROM dialogs"))).scalar()
        self.assertEqual(n, len(self.ROWS))

    def test_16_no_telegram_possible(self):
        stdlib_or_local = {"re", "unicodedata", "dataclasses", "datetime", "logging", "__future__"}
        for name in ("signals", "contradiction", "momentum", "next_question", "deposit_graph", "analysis"):
            tree = ast.parse((PKG / f"{name}.py").read_text())
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        self.assertIn(a.name.split(".")[0], stdlib_or_local, name)
                if isinstance(n, ast.ImportFrom):
                    self.assertIn((n.module or "").split(".")[0], stdlib_or_local | {"copilot_shadow"}, name)
                if isinstance(n, ast.Attribute):
                    self.assertNotIn(n.attr, {"send_message", "reply", "send_file", "execute", "commit"})

    async def test_17_exceptions_do_not_break(self):
        await self.seed(self.ROWS)
        with mock.patch.object(hook, "analyze", side_effect=RuntimeError("boom")):
            self.assertTrue(self.sched(msg=10, history=self.hist()))
            await self.drain()
        row = await self.fetch()          # decision record still persisted
        self.assertIsNone(row["momentum"])
        self.assertEqual(row["chosen_action"], "CONTINUE_DIALOGUE")
        # one module failing keeps the other three
        with mock.patch.object(analysis.contradiction, "detect", side_effect=ValueError("x")):
            self.assertTrue(self.sched(msg=11, history=self.hist()))
            await self.drain()
        async with self.sf() as s:
            r2 = (await s.execute(select(store.shadow_decisions).where(store.shadow_decisions.c.message_id == 11))).mappings().one()
        self.assertEqual(r2["contradiction"], {"error": "ValueError"})
        self.assertIn("direction", r2["momentum"])
        self.assertEqual(r2["deposit_path"]["current_node"], "PAYMENT")

    async def test_18_flag_disables_everything(self):
        await self.seed(self.ROWS)
        with mock.patch.dict(os.environ, {"COPILOT_SHADOW_ENABLED": "false"}), \
             mock.patch.object(hook, "analyze", side_effect=AssertionError("must not run")), \
             mock.patch.object(hook, "_load_turns", side_effect=AssertionError("must not run")):
            self.assertFalse(self.sched(msg=12, history=self.hist()))
            await self.drain()
        self.assertEqual(await self.count(), 0)
        self.assertFalse(store._ready)

    async def test_migration_adds_columns_to_old_table(self):
        async with self.sf() as s:
            await s.execute(text(
                "CREATE TABLE shadow_decisions (id INTEGER PRIMARY KEY, decision_id TEXT UNIQUE, chat_id BIGINT, "
                "message_id BIGINT, created_at TEXT, state_snapshot JSON, candidate_actions JSON, chosen_action TEXT, "
                "chosen_rule TEXT, wait_baseline JSON, decision_status TEXT, shadow_mode BOOLEAN, model_name TEXT, "
                "model_version TEXT, analyzer_version TEXT, code_version TEXT, production_regime TEXT, "
                "production_action_proxy TEXT, reply_chars INTEGER, UNIQUE(chat_id, message_id))"))
            await s.commit()
        await store.ensure_table(self.sf)
        async with self.sf() as s:
            cols = {r[1] for r in (await s.execute(text("PRAGMA table_info(shadow_decisions)"))).all()}
        self.assertTrue({"contradiction", "momentum", "next_best_question", "deposit_path"} <= cols)


if __name__ == "__main__":
    unittest.main()
