import ast
import copy
import dataclasses
import random
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from copilot_contracts.stages import Stage
from copilot_evidence import fusion
from copilot_evidence.adapters import adapt_client_state, adapt_production_decision, adapt_shadow_row
from copilot_evidence.contracts import Evidence, EvidenceSource as S, EvidenceType as T, Provenance
from copilot_evidence.fusion import FusionResult, fuse

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def ev(eid="e1", src=S.SHADOW, typ=T.MOMENTUM, value=None, stage=Stage.UNKNOWN, chat=5, msg=9,
       sid="pd_1", created=NOW, meta=None, conf=None):
    return Evidence(evidence_id=eid, chat_id=chat, message_id=msg, created_at=created, source=src,
                    evidence_type=typ, value={"x": 1} if value is None else value, confidence=conf,
                    reliability=None, observed_at=NOW, stage=stage, provenance=Provenance(src, sid),
                    metadata=meta or {})


class FusionTests(unittest.TestCase):
    def test_empty_and_missing(self):
        r = fuse([])
        self.assertEqual(r, FusionResult())
        self.assertEqual((r.evidence, r.links, r.conflicts, r.duplicates, r.rejected), ((),) * 5)
        self.assertEqual(r.fusion_version, "fusion.v1")
        self.assertEqual(fuse(iter(())).evidence, ())      # no evidence != negative evidence

    def test_single(self):
        e = ev()
        r = fuse([e])
        self.assertEqual(r.evidence, (e,))
        self.assertEqual((r.links, r.conflicts, r.duplicates), ((), (), ()))

    def test_multiple_sources_all_kept(self):
        a = ev("m", S.SHADOW, T.MOMENTUM)
        b = ev("o", S.EXTERNAL, T.EXTERNAL_SIGNAL, sid="partner_feed", msg=None)
        c = ev("i", S.CLIENT_STATE, T.INTENT, value="question", sid=None)
        r = fuse([c, b, a])
        self.assertEqual({e.evidence_id for e in r.evidence}, {"m", "o", "i"})
        self.assertEqual(r.conflicts, ())

    def test_exact_duplicate(self):
        a = ev()
        b = ev(created=NOW + timedelta(minutes=5))      # same observation, other created_at
        r = fuse([b, a, copy.copy(a)])
        self.assertEqual(len(r.evidence), 1)
        self.assertEqual(r.evidence[0].created_at, NOW)
        self.assertEqual(r.duplicates[0].dropped, 2)

    def test_same_type_is_not_duplicate(self):
        r = fuse([ev("a", value={"x": 1}), ev("b", value={"x": 2}), ev("c", src=S.EXTERNAL, sid="h")])
        self.assertEqual(len(r.evidence), 3)
        self.assertEqual(r.duplicates, ())

    def test_derived_linked_not_removed(self):
        row = {"decision_id": "pd_1", "chat_id": 5, "message_id": 9, "created_at": NOW,
               "deposit_path": {"current_node": "PAYMENT", "node_source": "current_turn_text",
                                "transition_confidence": 0.8, "blocker": "PAYMENT_METHOD_UNCERTAINTY",
                                "blocker_confidence": 0.7, "blocked_transition": "PAYMENT→DEPOSIT"}}
        res = adapt_shadow_row(row)
        r = fuse(res.evidence)
        self.assertEqual({e.evidence_type for e in r.evidence}, {T.STAGE, T.BLOCKER, T.PAYMENT_SIGNAL})
        (link,) = r.links
        self.assertEqual(link.resolution, "resolved")
        self.assertFalse(link.independent)
        by = {e.evidence_type: e.evidence_id for e in r.evidence}
        self.assertEqual((link.derived_id, link.origin_id), (by[T.PAYMENT_SIGNAL], by[T.BLOCKER]))
        self.assertEqual(r.conflicts, ())

    def test_derived_unresolved(self):
        lone = ev("p", typ=T.PAYMENT_SIGNAL, meta={"derived_from": "deposit_path.blocker"})
        self.assertEqual(fuse([lone]).links[0].resolution, "origin_missing")
        other = ev("g", S.CLIENT_STATE, T.CLIENT_FACT, value={"field": "region", "value": "MX"},
                   meta={"derived_from": "declared_profile"})
        l = fuse([other]).links[0]
        self.assertEqual((l.resolution, l.origin_id), ("origin_not_evidence", None))

    def test_conflicting_stage(self):
        cs = ev("cs", S.CLIENT_STATE, T.STAGE, value={"production_stage": "deposit_attempt"},
                stage=Stage.PAYMENT, sid=None)
        sh = ev("sh", S.SHADOW, T.STAGE, stage=Stage.TRUST)
        r = fuse([sh, cs])
        self.assertEqual(len(r.evidence), 2)
        (c,) = r.conflicts
        self.assertEqual(c.field, "stage")
        self.assertEqual(set(c.evidence_ids), {"cs", "sh"})
        self.assertEqual(dict(zip(c.evidence_ids, c.values)), {"cs": "PAYMENT", "sh": "TRUST"})
        self.assertEqual(dict(zip(c.evidence_ids, c.sources)), {"cs": "client_state", "sh": "shadow"})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            c.field = "x"

    def test_no_stage_conflict_cases(self):
        same = fuse([ev("a", typ=T.STAGE, stage=Stage.TRUST), ev("b", S.CLIENT_STATE, T.STAGE, stage=Stage.TRUST)])
        self.assertEqual(same.conflicts, ())
        unk = fuse([ev("a", typ=T.STAGE, stage=Stage.TRUST), ev("b", S.CLIENT_STATE, T.STAGE, stage=Stage.UNKNOWN)])
        self.assertEqual(unk.conflicts, ())
        other_msg = fuse([ev("a", typ=T.STAGE, stage=Stage.TRUST, msg=1), ev("b", typ=T.STAGE, stage=Stage.PAYMENT, msg=2)])
        self.assertEqual(other_msg.conflicts, ())
        other_chat = fuse([ev("a", typ=T.STAGE, stage=Stage.TRUST), ev("b", typ=T.STAGE, stage=Stage.PAYMENT, chat=6)])
        self.assertEqual(other_chat.conflicts, ())

    def test_conflicting_intent_and_sentiment(self):
        r = fuse([ev("a", S.CLIENT_STATE, T.INTENT, value="ready"), ev("b", S.SHADOW, T.INTENT, value="objection"),
                  ev("s1", S.CLIENT_STATE, T.SENTIMENT, value=0.7), ev("s2", S.SHADOW, T.SENTIMENT, value=0.3)])
        self.assertEqual(sorted(c.field for c in r.conflicts), ["intent", "sentiment"])
        self.assertEqual(len(r.evidence), 4)

    def test_client_fact_conflict_region_vs_geo(self):
        cs = ev("f1", S.CLIENT_STATE, T.CLIENT_FACT, value={"field": "region", "value": "MX"}, sid=None)
        os_ = ev("f2", S.EXTERNAL, T.CLIENT_FACT, value={"category": "geo", "value": "ES"}, sid="phone_prefix", msg=None)
        ok = ev("f3", S.EXTERNAL, T.CLIENT_FACT, value={"category": "geo", "value": "mx"}, sid="x", msg=None)
        self.assertEqual(len(fuse([cs, os_]).conflicts), 1)
        self.assertEqual(fuse([cs, ok]).conflicts, ())

    def test_action_never_conflicts(self):
        act = ev("act", S.EXTERNAL, T.ACTION, value={"regime": "sales"}, sid="pd_1")
        client = [ev("i", S.CLIENT_STATE, T.INTENT, value="objection"),
                  ev("st", S.CLIENT_STATE, T.STAGE, stage=Stage.OBJECTION),
                  ev("se", S.CLIENT_STATE, T.SENTIMENT, value=0.3),
                  ev("f", S.CLIENT_STATE, T.CLIENT_FACT, value={"field": "region", "value": "MX"})]
        act2 = ev("act2", S.EXTERNAL, T.ACTION, value={"regime": "casual"}, sid="pd_2")
        r = fuse(client + [act, act2])
        self.assertEqual(r.conflicts, ())
        self.assertEqual(len(r.evidence), 6)

    def test_id_collision_reported(self):
        r = fuse([ev("same", value={"x": 1}), ev("same", value={"x": 2})])
        self.assertEqual(len(r.evidence), 2)
        self.assertEqual(r.conflicts[0].field, "evidence_id")

    def test_invalid_input(self):
        good = ev()
        r = fuse([None, good, {"evidence_id": "x"}, "str", 5, good.to_dict()])
        self.assertEqual(r.evidence, (good,))
        self.assertEqual(len(r.rejected), 5)
        for junk in (None, 5, "abc", b"x"):
            rr = fuse(junk)
            self.assertEqual(rr.evidence, ())
            self.assertEqual(rr.rejected[0].reason, "not_an_iterable")
        gen = fuse(x for x in [good, None])
        self.assertEqual(len(gen.evidence), 1)

    def test_deterministic_order_independent(self):
        items = [ev("m", S.SHADOW, T.MOMENTUM), ev("o", S.EXTERNAL, T.EXTERNAL_SIGNAL, sid="h", msg=None),
                 ev("i", S.CLIENT_STATE, T.INTENT, value="ready"), ev("i2", S.SHADOW, T.INTENT, value="question"),
                 ev("a", S.EXTERNAL, T.ACTION, value={"regime": "r"}),
                 ev("m", S.SHADOW, T.MOMENTUM, created=NOW + timedelta(hours=1)),
                 ev("p", typ=T.PAYMENT_SIGNAL, meta={"derived_from": "deposit_path.blocker"}), None]
        base = fuse(items)
        rnd = random.Random(7)
        for _ in range(20):
            shuffled = items[:]
            rnd.shuffle(shuffled)
            self.assertEqual(fuse(shuffled), base)
        order = [(e.source.value, e.evidence_type.value, e.evidence_id) for e in base.evidence]
        self.assertEqual(order, sorted(order))
        self.assertEqual(fuse(items), base)

    def test_provenance_preserved(self):
        items = [ev("m", sid="pd_9"), ev("c", S.CLIENT_STATE, T.INTENT, value="x", sid=None),
                 ev("o", S.EXTERNAL, T.EXTERNAL_SIGNAL, sid="partner_feed", msg=None)]
        r = fuse(items)
        self.assertEqual(set(r.provenance()), {("shadow", "pd_9", "m", "evidence.v1"),
                                               ("client_state", None, "c", "evidence.v1"),
                                               ("external", "partner_feed", "o", "evidence.v1")})
        for e in r.evidence:
            self.assertIn(e, items)        # the very same objects, nothing rewritten or invented

    def test_no_mutation_and_immutable_result(self):
        items = [ev("a", typ=T.STAGE, stage=Stage.TRUST), ev("b", S.CLIENT_STATE, T.STAGE, stage=Stage.PAYMENT)]
        before = [e.to_dict() for e in items]
        r = fuse(items)
        self.assertEqual([e.to_dict() for e in items], before)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            r.evidence = ()
        self.assertIsInstance(r.evidence, tuple)
        self.assertIsInstance(r.conflicts, tuple)

    def test_end_to_end_adapters(self):
        cs = adapt_client_state({"user_id": 5, "stage": "deposit_attempt",
                                 "conversation": {"intent": "ready", "sentiment": 0.7}},
                                created_at=NOW, message_id=9)
        sh = adapt_shadow_row({"decision_id": "pd_1", "chat_id": 5, "message_id": 9, "created_at": NOW,
                               "deposit_path": {"current_node": "TRUST", "node_source": "current_turn_text",
                                                "transition_confidence": 0.8}})
        act = adapt_production_decision({"decision_id": "pd_1", "chat_id": 5, "message_id": 9,
                                         "created_at": NOW, "regime": "sales"})
        r = fuse(list(cs.evidence) + list(sh.evidence) + list(act.evidence))
        self.assertEqual([c.field for c in r.conflicts], ["stage"])
        self.assertIn(T.ACTION, {e.evidence_type for e in r.evidence})


class FusionSafetyTests(unittest.TestCase):
    SRC = ROOT / "copilot_evidence" / "fusion.py"
    ALLOWED = {"hashlib", "json", "dataclasses", "typing", "copilot_evidence"}
    BANNED = ("main", "telethon", "fastapi", "sqlalchemy", "asyncio", "aiohttp", "httpx", "requests",
              "socket", "urllib", "pickle", "os", "subprocess", "openai", "anthropic", "random",
              "copilot_decisions", "copilot_shadow", "client_state")

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
        for w in ("create table", "insert into", "datetime.now", "time.time"):
            self.assertNotIn(w, txt)

    def test_no_forbidden_modules_and_no_production_import(self):
        for p in ("decision_evidence.py", "outcome", "delta_state", "uplift", "policy", "scoring.py"):
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
        code = ("import sys, copilot_evidence.fusion;"
                "bad=[m for m in ('main','telethon','sqlalchemy','fastapi','asyncio','socket','requests',"
                "'httpx','subprocess','copilot_decisions','copilot_shadow','copilot_osint','scout') if m in sys.modules];"
                "print(','.join(bad)); sys.exit(1 if bad else 0)")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
