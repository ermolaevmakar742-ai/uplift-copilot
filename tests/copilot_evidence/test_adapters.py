import ast
import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from copilot_contracts.stages import Stage
from copilot_decisions.contract import ProductionDecision
from copilot_evidence.adapters import (adapt_client_state, adapt_production_decision,
                                    adapt_shadow_row)
from copilot_evidence.contracts import Evidence, EvidenceSource, EvidenceType as T
from copilot_shadow.analysis import analyze
from copilot_shadow.signals import Turn

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def types(res):
    return [e.evidence_type for e in res.evidence]


def reasons(res):
    return {s.what: s.reason for s in res.skipped}


def shadow_row(user_msgs, stage="new", **over):
    turns = []
    for m in user_msgs:
        turns += [Turn("assistant", "hola"), Turn("user", m)]
    row = analyze(turns, stage).to_dict()
    row.pop("meta")
    row.update(decision_id="pd_abc", chat_id=5, message_id=9, created_at=NOW)
    row.update(over)
    return row


class ShadowTests(unittest.TestCase):
    def test_real_analysis_row(self):
        row = shadow_row(["quiero empezar", "como pago? no tengo tarjeta"])
        res = adapt_shadow_row(row)
        self.assertTrue(res.evidence)
        for e in res.evidence:
            self.assertIs(e.source, EvidenceSource.SHADOW)
            self.assertEqual(e.provenance.source_id, "pd_abc")
            self.assertEqual(e.provenance.source.value, "shadow")
            self.assertIsNone(e.reliability)
            Evidence.from_dict(json.loads(json.dumps(e.to_dict())))   # round-trip safe
        by = {e.evidence_type: e for e in res.evidence}
        self.assertEqual(by[T.MOMENTUM].confidence, row["momentum"]["confidence"])  # untouched
        self.assertEqual(by[T.MOMENTUM].value["direction"], row["momentum"]["direction"])
        self.assertEqual(by[T.MOMENTUM].metadata["confidence_type"], "rule_based_coverage")

    def test_stage_and_blocker_payment(self):
        dp = {"version": "v", "current_node": "PAYMENT", "previous_node": "NEED", "furthest_node": "PAYMENT",
              "regression": False, "blocked_transition": "PAYMENT→DEPOSIT",
              "blocker": "PAYMENT_METHOD_UNCERTAINTY", "blocker_confidence": 0.7,
              "transition_confidence": 0.8, "confidence_type": "rule_based_coverage",
              "node_source": "current_turn_text"}
        res = adapt_shadow_row({"decision_id": "pd_1", "chat_id": 1, "message_id": 2, "created_at": NOW,
                                "deposit_path": dp})
        by = {e.evidence_type: e for e in res.evidence}
        self.assertEqual(set(by), {T.STAGE, T.BLOCKER, T.PAYMENT_SIGNAL})
        self.assertIs(by[T.STAGE].stage, Stage.PAYMENT)
        self.assertEqual(by[T.STAGE].confidence, 0.8)
        self.assertEqual(by[T.BLOCKER].confidence, 0.7)
        self.assertEqual(by[T.PAYMENT_SIGNAL].metadata["derived_from"], "deposit_path.blocker")

    def test_trust_blocker(self):
        dp = {"current_node": "TRUST", "blocker": "TRUST_CONCERN", "blocker_confidence": 0.6,
              "transition_confidence": 0.5, "node_source": "earlier_turn_text"}
        res = adapt_shadow_row({"decision_id": "pd_1", "chat_id": 1, "created_at": NOW, "deposit_path": dp})
        self.assertIn(T.TRUST_SIGNAL, types(res))
        self.assertNotIn(T.PAYMENT_SIGNAL, types(res))

    def test_contradiction(self):
        ctr = {"detected": True, "type": "PAYMENT_INTENT_MISMATCH", "confidence": 0.5,
               "intent_signal": "HIGH", "behavioral_commitment": "LOW", "confidence_type": "x"}
        res = adapt_shadow_row({"decision_id": "pd_1", "chat_id": 1, "created_at": NOW, "contradiction": ctr})
        e = res.evidence[0]
        self.assertIs(e.evidence_type, T.CONTRADICTION)
        self.assertEqual(e.value["type"], "PAYMENT_INTENT_MISMATCH")
        self.assertEqual(e.confidence, 0.5)

    def test_absence_is_not_evidence(self):
        row = {"decision_id": "pd_1", "chat_id": 1, "created_at": NOW,
               "contradiction": {"detected": False, "type": "NONE", "confidence": 0.0},
               "momentum": {"score": 0.0, "direction": "STABLE", "confidence": 0.0, "user_turns_seen": 0},
               "deposit_path": {"current_node": "START", "node_source": "no_evidence"},
               "next_best_question": {"recommended": True}}
        res = adapt_shadow_row(row)
        self.assertEqual(res.evidence, ())
        r = reasons(res)
        self.assertEqual(r["contradiction"], "not_detected")
        self.assertEqual(r["momentum"], "no_user_turns_seen")
        self.assertEqual(r["next_best_question"], "recommendation_not_observation")

    def test_production_stage_node_not_guessed(self):
        dp = {"current_node": "SUCCESS", "node_source": "production_stage", "transition_confidence": 0.9}
        res = adapt_shadow_row({"decision_id": "pd_1", "chat_id": 1, "created_at": NOW, "deposit_path": dp})
        self.assertEqual(res.evidence, ())
        self.assertIn("deposit_path.stage", reasons(res))

    def test_missing_and_bad_fields(self):
        self.assertEqual(adapt_shadow_row({}).evidence, ())
        self.assertEqual(adapt_shadow_row(None).evidence, ())
        self.assertEqual(adapt_shadow_row({"chat_id": 1}).evidence, ())            # no created_at
        res = adapt_shadow_row({"chat_id": 1, "created_at": NOW,
                                "momentum": {"error": "KeyError"}, "contradiction": "junk"})
        self.assertEqual(res.evidence, ())
        self.assertEqual(reasons(res)["momentum"], "module_error:KeyError")
        # no decision_id -> source_id None, never invented
        res = adapt_shadow_row({"chat_id": 1, "message_id": 3, "created_at": NOW,
                                "momentum": {"score": 0.2, "direction": "STABLE", "confidence": 0.5,
                                             "user_turns_seen": 2}})
        self.assertIsNone(res.evidence[0].provenance.source_id)

    def test_one_bad_evidence_does_not_break_batch(self):
        row = {"decision_id": "pd_1", "chat_id": 1, "created_at": NOW,
               "momentum": {"score": 0.2, "direction": "STABLE", "confidence": 7.0, "user_turns_seen": 2},
               "contradiction": {"detected": True, "type": "X", "confidence": 0.4}}
        res = adapt_shadow_row(row)
        self.assertEqual(types(res), [T.CONTRADICTION])
        self.assertTrue(reasons(res)["MOMENTUM"].startswith("invalid_evidence"))

    def test_no_mutation_and_deterministic(self):
        row = shadow_row(["quiero empezar", "es seguro? tengo miedo, parece estafa"])
        before = copy.deepcopy(row)
        a, b = adapt_shadow_row(row), adapt_shadow_row(row)
        self.assertEqual(row, before)
        self.assertEqual(a, b)


def cs_state(**over):
    st = {"user_id": 5, "stage": "new", "regime": "casual",
          "identity": {"username": "u", "name": "N", "region": "MX"},
          "conversation": {"intent": "question", "sentiment": 0.7, "complexity": 0.2, "messages_count": 4,
                           "last_message": "SECRET TEXT"},
          "memory": {}, "risk": {}, "updated_at": "2026-10-02T11:59:00"}
    st.update(over)
    return st


class ClientStateTests(unittest.TestCase):
    def test_full_state(self):
        st = cs_state()
        res = adapt_client_state(st, created_at=NOW, message_id=9)
        by = {e.evidence_type: e for e in res.evidence}
        self.assertEqual(set(by), {T.STAGE, T.INTENT, T.SENTIMENT, T.CLIENT_FACT})
        self.assertIs(by[T.STAGE].stage, Stage.START)
        self.assertEqual(by[T.STAGE].value["production_stage"], "new")
        self.assertEqual(by[T.INTENT].value, "question")
        self.assertEqual(by[T.SENTIMENT].value, 0.7)
        self.assertEqual(by[T.CLIENT_FACT].value, {"field": "region", "value": "MX"})
        self.assertEqual(by[T.CLIENT_FACT].metadata["derived_from"], "declared_profile")
        for e in res.evidence:
            self.assertIs(e.source, EvidenceSource.CLIENT_STATE)
            self.assertEqual(e.chat_id, 5)
            self.assertIsNone(e.provenance.source_id)      # no identifier exists -> None
            self.assertIsNone(e.confidence)
            self.assertEqual(e.observed_at, datetime(2026, 10, 2, 11, 59, tzinfo=timezone.utc))
        self.assertNotIn("SECRET", json.dumps([e.to_dict() for e in res.evidence]))
        self.assertEqual(reasons(res)["complexity"], "no_canonical_evidence_type")

    def test_stage_not_guessed(self):
        for raw, want in (("deposited", Stage.DEPOSIT), ("qualified", Stage.UNKNOWN), ("offer", Stage.UNKNOWN),
                          ("deposit_attempt", Stage.PAYMENT)):
            e = adapt_client_state(cs_state(stage=raw), created_at=NOW).evidence[0]
            self.assertIs(e.stage, want, raw)
            self.assertEqual(e.value["production_stage"], raw)

    def test_missing_and_default_values(self):
        res = adapt_client_state({"user_id": 5}, created_at=NOW)
        self.assertEqual(res.evidence, ())
        st = cs_state(identity={"region": "?"}, stage=None)
        st["conversation"].update(intent="unknown", sentiment=0.5)
        res = adapt_client_state(st, created_at=NOW)
        self.assertEqual(res.evidence, ())
        self.assertEqual(reasons(res)["sentiment"], "default_value_without_text")
        self.assertEqual(adapt_client_state(cs_state(), created_at=None).evidence, ())
        self.assertEqual(adapt_client_state("x", created_at=NOW).evidence, ())
        self.assertEqual(adapt_client_state({"stage": "new"}, created_at=NOW).evidence, ())  # no chat id

    def test_ledger_snapshot_shape_and_source_id(self):
        snap = {"stage": "objection", "intent": "ready", "sentiment": 0.3, "region": "MX",
                "snapshot_version": "cs-snapshot-1"}
        res = adapt_client_state(snap, chat_id=5, created_at=NOW, source_id="pd_abc")
        self.assertEqual(len(res.evidence), 4)
        self.assertTrue(all(e.provenance.source_id == "pd_abc" for e in res.evidence))
        self.assertIs(res.evidence[0].stage, Stage.OBJECTION)

    def test_no_mutation(self):
        st = cs_state()
        before = copy.deepcopy(st)
        adapt_client_state(st, created_at=NOW)
        self.assertEqual(st, before)


def decision(**kw):
    base = dict(decision_id="pd_123", chat_id=5, message_id=9, created_at=NOW, regime="sales",
                model="m1", flow_version="v37", trigger="price", experiment_variant="B",
                used_fallback=False, reply_parts=2,
                client_state_snapshot={"stage": "offer", "intent": "ready"},
                metadata={"contract_version": "production-decision-1"})
    base.update(kw)
    return ProductionDecision(**base)


class ProductionTests(unittest.TestCase):
    def test_decision_id_in_provenance(self):
        res = adapt_production_decision(decision())
        self.assertEqual(len(res.evidence), 1)
        e = res.evidence[0]
        self.assertEqual(e.provenance.source_id, "pd_123")
        self.assertIs(e.source, EvidenceSource.PRODUCTION)
        self.assertEqual(e.value["regime"], "sales")
        self.assertEqual(e.value["flow_version"], "v37")
        self.assertEqual(e.metadata["experiment_variant"], "B")
        self.assertEqual(e.metadata["trigger"], "price")
        self.assertTrue(e.metadata["not_a_client_fact"])
        self.assertEqual(e.metadata["subject"], "system_action")

    def test_not_a_client_fact(self):
        res = adapt_production_decision(decision())
        self.assertEqual(types(res), [T.ACTION])
        for banned in (T.INTENT, T.STAGE, T.CLIENT_FACT, T.PAYMENT_SIGNAL, T.TRUST_SIGNAL, T.UNKNOWN):
            self.assertNotIn(banned, types(res))
        self.assertIs(res.evidence[0].stage, Stage.UNKNOWN)
        blob = json.dumps(res.evidence[0].to_dict())
        self.assertNotIn("priority", blob)         # legacy signal fields not copied
        self.assertNotIn("offer", blob)            # snapshot not copied
        self.assertEqual(adapt_production_decision(decision(regime="objection")).evidence[0].stage, Stage.UNKNOWN)
        r = reasons(res)
        self.assertIn("client_state_snapshot", r)

    def test_missing_optional_fields_safe(self):
        d = decision(regime=None, model=None, flow_version=None, trigger=None, experiment_variant=None,
                     used_fallback=None, reply_parts=None, client_state_snapshot=None,
                     message_id=None, metadata={})
        res = adapt_production_decision(d)        # only decision_path left
        self.assertEqual(res.evidence[0].value, {"decision_path": "llm_reply"})
        self.assertIsNone(res.evidence[0].message_id)

    def test_dict_form_and_garbage(self):
        d = decision().to_dict()
        self.assertEqual(adapt_production_decision(d), adapt_production_decision(decision()))
        for junk in (None, 5, {}, {"decision_id": "pd_1"}, {"decision_id": "pd_1", "chat_id": 1}):
            self.assertEqual(adapt_production_decision(junk).evidence, ())


class SafetyTests(unittest.TestCase):
    FILES = sorted((ROOT / "copilot_evidence" / "adapters").glob("*.py"))
    ALLOWED = {"copy", "dataclasses", "datetime", "typing", "copilot_contracts", "copilot_evidence"}
    ALLOWED_FROM_OUTSIDE = set()
    BANNED = ("main", "telethon", "fastapi", "starlette", "sqlalchemy", "asyncio", "aiohttp", "httpx",
              "requests", "socket", "urllib", "pickle", "os", "subprocess", "openai", "anthropic",
              "copilot_decisions", "copilot_shadow", "scout", "client_state", "language_layer")

    def test_import_guard(self):
        self.assertEqual({f.name for f in self.FILES},
                         {"__init__.py", "base.py", "client_state.py", "production.py", "shadow.py"})
        for f in self.FILES:
            for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                mods = ([a.name for a in n.names] if isinstance(n, ast.Import)
                        else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
                for m in mods:
                    self.assertNotIn(m.split(".")[0], self.BANNED, f"{f.name}: {m}")
                    self.assertTrue(m.split(".")[0] in self.ALLOWED or m in self.ALLOWED_FROM_OUTSIDE,
                                    f"{f.name}: {m}")
                if isinstance(n, ast.ImportFrom):
                    self.assertEqual(n.level, 0)
                self.assertNotIsInstance(n, (ast.AsyncFunctionDef, ast.Await))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                    self.assertNotIn(n.func.id, {"open", "eval", "exec", "compile", "__import__"})

    def test_no_external_imports(self):
        for f in self.FILES:
            txt = f.read_text(encoding="utf-8")
            self.assertNotIn("copilot_external", txt, f.name)
            self.assertNotIn("sociopath", txt, f.name)
            self.assertNotIn("osint", txt, f.name)

    def test_no_forbidden_modules_created(self):
        for p in ("decision_evidence.py", "outcome", "delta_state", "uplift", "policy"):
            self.assertFalse((ROOT / "copilot_evidence" / p).exists(), p)

    def test_production_does_not_reference_adapters(self):
        prod = [ROOT / n for n in ("client_state.py", "classifier.py", "embeddings.py")]
        for d in ("copilot_decisions", "copilot_shadow"):
            prod += [p for p in (ROOT / d).glob("*.py") if p.name != "recorder.py"]   # only copilot_decisions/recorder.py may hook capture
        for f in prod:
            if f.exists():
                txt = f.read_text(encoding="utf-8")
                self.assertNotIn("copilot_evidence", txt, f.name)

    def test_imports_pull_no_network_or_runtime(self):
        import subprocess
        import sys
        code = ("import sys, copilot_evidence.adapters;"
                "bad=[m for m in ('main','telethon','sqlalchemy','fastapi','asyncio','socket','requests',"
                "'httpx','subprocess','copilot_decisions','copilot_shadow','scout') if m in sys.modules];"
                "print(','.join(bad)); sys.exit(1 if bad else 0)")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
