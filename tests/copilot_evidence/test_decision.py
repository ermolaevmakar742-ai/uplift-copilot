import ast
import copy
import dataclasses
import json
import random
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from copilot_contracts.stages import Stage
from copilot_evidence.adapters import adapt_client_state, adapt_production_decision, adapt_shadow_row
from copilot_evidence.contracts import ContractError, Evidence, EvidenceSource as S, EvidenceType as T, Provenance
from copilot_evidence.decision import (DECISION_EVIDENCE_CONTRACT_VERSION, DecisionEvidence,
                                    build_decision_evidence)
from copilot_evidence.fusion import fuse

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
DID = "pd_abc123"


def ev(eid, src, typ, value=None, stage=Stage.UNKNOWN, chat=5, msg=9, sid=DID, meta=None):
    return Evidence(evidence_id=eid, chat_id=chat, message_id=msg, created_at=NOW, source=src,
                    evidence_type=typ, value={"k": [1, {"z": 2}]} if value is None else value,
                    confidence=0.5, reliability=None, observed_at=NOW, stage=stage,
                    provenance=Provenance(src, sid), metadata=meta or {"m": {"n": [1]}})


def items():
    return [ev("cs", S.CLIENT_STATE, T.STAGE, {"production_stage": "deposit_attempt"}, Stage.PAYMENT, sid=None),
            ev("sh", S.SHADOW, T.STAGE, stage=Stage.TRUST),
            ev("blk", S.SHADOW, T.BLOCKER, {"blocker": "PAYMENT_METHOD_UNCERTAINTY"}),
            ev("pay", S.SHADOW, T.PAYMENT_SIGNAL, {"blocker": "PAYMENT_METHOD_UNCERTAINTY"},
               meta={"derived_from": "deposit_path.blocker"}),
            ev("os", S.EXTERNAL, T.EXTERNAL_SIGNAL, {"category": "platform_presence"}, sid="partner_feed", msg=None),
            ev("act", S.PRODUCTION, T.ACTION, {"regime": "sales"})]


def make(its=None, **kw):
    args = dict(chat_id=5, message_id=9, created_at=NOW)
    args.update(kw)
    return build_decision_evidence(DID, fuse(items() if its is None else its), **args)


class DecisionEvidenceTests(unittest.TestCase):
    def test_valid(self):
        d = make()
        self.assertEqual(d.decision_id, DID)
        self.assertEqual((d.chat_id, d.message_id, d.created_at), (5, 9, NOW))
        self.assertEqual(d.contract_version, "decision_evidence.v1")
        self.assertEqual(DECISION_EVIDENCE_CONTRACT_VERSION, "decision_evidence.v1")
        self.assertTrue(d.decision_evidence_id.startswith("de_"))
        self.assertEqual(len(d.evidence), 6)
        self.assertEqual(d.created_at, NOW)
        self.assertIsInstance(d.evidence, tuple)

    def test_empty_snapshot_is_valid(self):
        d = make([])
        self.assertEqual((d.evidence, d.conflicts, d.links), ((), (), ()))

    def test_required_decision_id(self):
        for bad in (None, "", "pd_", "xx_1", 5):
            with self.assertRaises(ContractError, msg=repr(bad)):
                build_decision_evidence(bad, fuse([]), chat_id=5, message_id=None, created_at=NOW)

    def test_chat_id_and_message_id(self):
        for bad in (None, "5", True, 1.5):
            with self.assertRaises(ContractError):
                make(chat_id=bad)
        self.assertIsNone(make(message_id=None).message_id)     # nullable, nothing invented
        with self.assertRaises(ContractError):
            make(message_id="9")
        with self.assertRaises(ContractError):
            make(chat_id=6)                                      # evidence of another chat

    def test_invalid_inputs(self):
        for bad in (datetime(2026, 1, 1), "2026", None):
            with self.assertRaises(ContractError):
                make(created_at=bad)
        for bad in (None, [], items(), {"evidence": ()}):
            with self.assertRaises(ContractError):
                build_decision_evidence(DID, bad, chat_id=5, message_id=9, created_at=NOW)
        with self.assertRaises(ContractError):                    # other decision's ACTION
            make([ev("act", S.PRODUCTION, T.ACTION, {"regime": "x"}, sid="pd_other")])
        with self.assertRaises(ContractError):
            DecisionEvidence("de_x", DID, 5, 9, NOW, ("junk",), (), None)

    def test_immutable(self):
        d = make()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            d.decision_id = "pd_z"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            d.provenance.decision_id = "pd_z"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            d.conflicts[0].field = "x"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            d.evidence[0].confidence = 0.9
        with self.assertRaises(TypeError):
            d.evidence[0].metadata["x"] = 1
        with self.assertRaises(TypeError):
            d.evidence[0].value["k"] = 1
        self.assertIsInstance(d.links, tuple)
        self.assertIsInstance(d.provenance.evidence_refs, tuple)

    def test_snapshot_isolation(self):
        src = items()
        fr = fuse(src)
        d = build_decision_evidence(DID, fr, chat_id=5, message_id=9, created_at=NOW)
        before = d.to_dict()
        src.clear()
        src.append(ev("new", S.SHADOW, T.INTENT, "ready"))
        self.assertEqual(d.to_dict(), before)
        # source dicts handed to Evidence cannot reach back into the snapshot
        meta, value = {"a": [1]}, {"b": {"c": 1}}
        e = ev("x", S.EXTERNAL, T.EXTERNAL_SIGNAL, value, meta=meta, sid="h", msg=None)
        d2 = build_decision_evidence(DID, fuse([e]), chat_id=5, message_id=9, created_at=NOW)
        b2 = d2.to_dict()
        meta["a"].append(2)
        value["b"]["c"] = 99
        self.assertEqual(d2.to_dict(), b2)
        # output of to_dict is a private copy
        out = d.to_dict()
        out["evidence"][0]["value"]["injected"] = 1
        out["conflicts"][0]["values"].append("x")
        out["provenance"]["evidence_refs"].clear()
        self.assertEqual(d.to_dict(), before)
        # deep: nested containers are frozen in the snapshot
        found = [e for e in d.evidence if e.evidence_id == "cs"][0]
        with self.assertRaises(TypeError):
            found.value["production_stage"] = "x"
        # replacing the FusionResult/evidence tuple elements is impossible and has no effect
        with self.assertRaises(dataclasses.FrozenInstanceError):
            fr.evidence = ()
        self.assertEqual(d.to_dict(), before)

    def test_no_live_client_state_reference(self):
        state = {"user_id": 5, "stage": "new", "conversation": {"intent": "question", "sentiment": 0.7}}
        res = adapt_client_state(state, created_at=NOW, message_id=9)
        d = build_decision_evidence(DID, fuse(res.evidence), chat_id=5, message_id=9, created_at=NOW)
        before = d.to_dict()
        state["stage"] = "deposited"
        state["conversation"]["intent"] = "objection"
        self.assertEqual(d.to_dict(), before)

    def test_deterministic_order_independent(self):
        base = make()
        rnd = random.Random(3)
        for _ in range(15):
            its = items()
            rnd.shuffle(its)
            d = make(its)
            self.assertEqual(d, base)
            self.assertEqual(d.decision_evidence_id, base.decision_evidence_id)
        order = [(e.source.value, e.evidence_type.value, e.evidence_id) for e in base.evidence]
        self.assertEqual(order, sorted(order))
        later = make(created_at=NOW + timedelta(days=1))
        self.assertEqual(later.decision_evidence_id, base.decision_evidence_id)   # created_at not hashed
        self.assertNotEqual(make(its=items()[:-1]).decision_evidence_id, base.decision_evidence_id)

    def test_conflicts_preserved(self):
        d = make()
        self.assertEqual(len(d.conflicts), 1)
        c = d.conflicts[0]
        self.assertEqual(c.field, "stage")
        self.assertEqual(dict(zip(c.evidence_ids, c.values)), {"cs": "PAYMENT", "sh": "TRUST"})
        ids = {e.evidence_id for e in d.evidence}
        self.assertTrue({"cs", "sh"} <= ids)                     # both conflicting evidences retained
        fr = fuse(items())
        self.assertEqual(d.conflicts, fr.conflicts)
        self.assertEqual(d.links, fr.links)
        self.assertEqual(d.links[0].resolution, "resolved")
        for name in ("final_stage", "final_intent", "winner", "score", "recommendation", "readiness"):
            self.assertFalse(hasattr(d, name))

    def test_conflict_must_reference_snapshot_evidence(self):
        d = make()
        fr = fuse(items())
        bad = dataclasses.replace(fr.conflicts[0], evidence_ids=("cs", "ghost"))
        with self.assertRaises(ContractError):
            DecisionEvidence(d.decision_evidence_id, DID, 5, 9, NOW, d.evidence, (bad,), d.provenance, d.links)

    def test_provenance_preserved(self):
        d = make()
        self.assertEqual(d.provenance.decision_id, DID)
        self.assertEqual(d.provenance.fusion_version, "fusion.v1")
        refs = {(r.source, r.source_id, r.evidence_id, r.contract_version) for r in d.provenance.evidence_refs}
        self.assertEqual(refs, {("client_state", None, "cs", "evidence.v1"), ("shadow", DID, "sh", "evidence.v1"),
                                ("shadow", DID, "blk", "evidence.v1"), ("shadow", DID, "pay", "evidence.v1"),
                                ("external", "partner_feed", "os", "evidence.v1"), ("production", DID, "act", "evidence.v1")})
        for e in d.evidence:                                      # Evidence still carries its own provenance
            self.assertIn((e.source.value, e.provenance.source_id, e.evidence_id, e.contract_version), refs)
        with self.assertRaises(ContractError):                    # fabricated provenance rejected
            DecisionEvidence(d.decision_evidence_id, DID, 5, 9, NOW, d.evidence, d.conflicts,
                             dataclasses.replace(d.provenance, evidence_refs=()), d.links)

    def test_serialization_round_trip(self):
        d = make()
        raw = json.loads(json.dumps(d.to_dict()))
        d2 = DecisionEvidence.from_dict(raw)
        self.assertEqual(d2, d)
        self.assertEqual(d2.to_dict(), d.to_dict())
        e = make([], message_id=None)
        self.assertEqual(DecisionEvidence.from_dict(json.loads(json.dumps(e.to_dict()))), e)

    def test_from_dict_strict(self):
        good = make().to_dict()
        for k in list(good):
            bad = copy.deepcopy(good)
            del bad[k]
            with self.assertRaises(ContractError, msg=k):
                DecisionEvidence.from_dict(bad)
        cases = []
        b = copy.deepcopy(good); b["extra"] = 1; cases.append(b)
        b = copy.deepcopy(good); b["decision_evidence_id"] = "de_tampered"; cases.append(b)
        b = copy.deepcopy(good); b["evidence"][0]["value"] = {"tampered": 1}; cases.append(b)
        b = copy.deepcopy(good); b["contract_version"] = "decision_evidence.v9"; cases.append(b)
        b = copy.deepcopy(good); b["provenance"]["evidence_refs"].pop(); cases.append(b)
        b = copy.deepcopy(good); b["conflicts"][0]["evidence_ids"] = ["cs", "ghost"]; cases.append(b)
        b = copy.deepcopy(good); b["evidence"] = "x"; cases.append(b)
        b = copy.deepcopy(good); b["links"][0]["independent"] = "no"; cases.append(b)
        b = copy.deepcopy(good); b["created_at"] = "2026-01-01T00:00:00"; cases.append(b)
        for bad in cases:
            with self.assertRaises(ContractError):
                DecisionEvidence.from_dict(bad)
        for junk in (None, [], "x"):
            with self.assertRaises(ContractError):
                DecisionEvidence.from_dict(junk)

    def test_end_to_end_with_adapters(self):
        cs = adapt_client_state({"user_id": 5, "stage": "deposit_attempt",
                                 "conversation": {"intent": "ready", "sentiment": 0.7}}, created_at=NOW, message_id=9)
        sh = adapt_shadow_row({"decision_id": DID, "chat_id": 5, "message_id": 9, "created_at": NOW,
                               "deposit_path": {"current_node": "TRUST", "node_source": "current_turn_text",
                                                "transition_confidence": 0.8}})
        act = adapt_production_decision({"decision_id": DID, "chat_id": 5, "message_id": 9, "created_at": NOW,
                                         "regime": "sales"})
        d = build_decision_evidence(DID, fuse(list(cs.evidence) + list(sh.evidence) + list(act.evidence)),
                                    chat_id=5, message_id=9, created_at=NOW)
        self.assertEqual([c.field for c in d.conflicts], ["stage"])
        self.assertEqual(DecisionEvidence.from_dict(json.loads(json.dumps(d.to_dict()))), d)


class DecisionSafetyTests(unittest.TestCase):
    SRC = ROOT / "copilot_evidence" / "decision.py"
    ALLOWED = {"hashlib", "json", "dataclasses", "datetime", "typing", "copilot_evidence"}
    BANNED = ("main", "telethon", "fastapi", "sqlalchemy", "asyncio", "aiohttp", "httpx", "requests", "socket",
              "urllib", "pickle", "os", "subprocess", "openai", "anthropic", "copilot_decisions", "copilot_shadow",
              "client_state")

    def test_imports(self):
        for n in ast.walk(ast.parse(self.SRC.read_text(encoding="utf-8"))):
            mods = ([a.name for a in n.names] if isinstance(n, ast.Import)
                    else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
            for m in mods:
                self.assertNotIn(m.split(".")[0], self.BANNED, m)
                self.assertIn(m.split(".")[0], self.ALLOWED, m)
            self.assertNotIsInstance(n, (ast.AsyncFunctionDef, ast.Await))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                self.assertNotIn(n.func.id, {"open", "eval", "exec", "compile", "__import__"})
        txt = self.SRC.read_text(encoding="utf-8").lower()
        for w in ("create table", "insert into", "datetime.now", "uuid"):
            self.assertNotIn(w, txt)

    def test_no_decision_engine_artifacts_and_no_production_import(self):
        for p in ("policy", "outcome", "delta_state", "uplift", "bandit", "scoring.py", "decision_engine.py"):
            self.assertFalse((ROOT / "copilot_evidence" / p).exists(), p)
        prod = [ROOT / n for n in ("main.py", "client_state.py", "scout.py", "run.py")]
        for d in ("copilot_decisions", "copilot_shadow"):
            prod += [p for p in (ROOT / d).glob("*.py") if p.name != "recorder.py"]   # only copilot_decisions/recorder.py may hook capture
        for f in prod:
            if f.exists():
                self.assertNotIn("copilot_evidence", f.read_text(encoding="utf-8"), f.name)

    def test_import_pulls_no_runtime(self):
        import subprocess
        import sys
        code = ("import sys, copilot_evidence.decision;"
                "bad=[m for m in ('main','telethon','sqlalchemy','fastapi','asyncio','socket','requests','httpx',"
                "'subprocess','copilot_decisions','copilot_shadow','copilot_osint','scout') if m in sys.modules];"
                "print(','.join(bad)); sys.exit(1 if bad else 0)")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
