import ast
import dataclasses
import random
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from copilot_evidence.contracts import ContractError
from copilot_outcomes import joiner
from copilot_outcomes.contracts import (ATTR_EXPLICIT, ATTR_FALLBACK, DEFAULT_OUTCOME_WINDOW_SECONDS, Outcome)
from copilot_outcomes.joiner import DecisionRef, join, join_all

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)


def dec(did="pd_1", chat=5, at=T0):
    return DecisionRef(did, chat, at)


def out(oid="o1", at=T0 + 10 * S, typ="DEPOSIT", did=None, chat=5, conf=None, value=None):
    return Outcome(outcome_id=oid, decision_id=did, chat_id=chat, observed_at=at, outcome_type=typ,
                   value=value, confidence=conf, source="POSTBACK", metadata={})


class JoinerTests(unittest.TestCase):
    def test_explicit_decision_id(self):
        r = join(dec(), [out(did="pd_1")])
        (a,) = r.outcomes
        self.assertEqual(a.attribution, ATTR_EXPLICIT)
        self.assertEqual(r.attribution, (ATTR_EXPLICIT,))
        self.assertEqual(r.decision_id, "pd_1")

    def test_explicit_has_priority_over_nearer_decision(self):
        d1, d2 = dec("pd_1", at=T0), dec("pd_2", at=T0 + 60 * S)
        o = out(at=T0 + 120 * S, did="pd_1")
        s1, s2 = join_all([d1, d2], [o])
        self.assertEqual([a.outcome for a in s1.outcomes], [o])
        self.assertEqual(s1.outcomes[0].attribution, ATTR_EXPLICIT)
        self.assertEqual(s2.outcomes, ())               # not stolen by the nearer decision

    def test_explicit_never_falls_back(self):
        d1 = dec("pd_1")
        self.assertEqual(join(d1, [out(did="pd_other")]).outcomes, ())          # unknown id
        d_other = dec("pd_9", chat=6)
        self.assertEqual(join_all([d1, dec("pd_2", at=T0 + S)], [out(did="pd_zzz")])[1].outcomes, ())
        self.assertEqual(join(d_other, [out(did="pd_9", chat=5)]).outcomes, ())  # chat mismatch: rejected
        self.assertEqual(join(d1, [out(did="pd_1", at=T0 - S)]).outcomes, ())    # time rule still applies

    def test_before_and_exactly_at_decision_rejected(self):
        for at in (T0 - 10 * S, T0 - timedelta(microseconds=1), T0):
            self.assertEqual(join(dec(), [out(at=at)]).outcomes, (), at)
            self.assertEqual(join(dec(), [out(at=at, did="pd_1")]).outcomes, (), at)
        self.assertEqual(len(join(dec(), [out(at=T0 + timedelta(microseconds=1))]).outcomes), 1)

    def test_window_inside_and_outside(self):
        w = 3600
        self.assertEqual(len(join(dec(), [out(at=T0 + 3599 * S)], outcome_window_seconds=w).outcomes), 1)
        self.assertEqual(len(join(dec(), [out(at=T0 + 3600 * S)], outcome_window_seconds=w).outcomes), 1)  # inclusive end
        self.assertEqual(join(dec(), [out(at=T0 + 3601 * S)], outcome_window_seconds=w).outcomes, ())
        self.assertEqual(join(dec(), [out(at=T0 + 3601 * S, did="pd_1")], outcome_window_seconds=w).outcomes, ())
        r = join(dec(), [], outcome_window_seconds=w)
        self.assertEqual((r.window.seconds, r.window.start, r.window.end), (w, T0, T0 + w * S))

    def test_default_window_comes_from_contract(self):
        r = join(dec(), [])
        self.assertEqual(r.window.seconds, DEFAULT_OUTCOME_WINDOW_SECONDS)
        edge = T0 + DEFAULT_OUTCOME_WINDOW_SECONDS * S
        self.assertEqual(len(join(dec(), [out(at=edge)]).outcomes), 1)
        self.assertEqual(join(dec(), [out(at=edge + S)]).outcomes, ())
        src = (ROOT / "copilot_outcomes" / "joiner.py").read_text(encoding="utf-8")
        self.assertNotIn("86400", src)
        self.assertNotIn("3600", src)

    def test_invalid_window(self):
        for bad in (0, -1, None, "3600", True, float("inf"), float("nan")):
            with self.assertRaises(ContractError, msg=repr(bad)):
                join(dec(), [], outcome_window_seconds=bad)

    def test_fallback_without_decision_id_and_marker(self):
        o = out(conf=0.4, value={"x": 1})
        r = join(dec(), [o])
        (a,) = r.outcomes
        self.assertEqual(a.attribution, "temporal_chat_fallback")
        self.assertEqual(r.attribution, (ATTR_FALLBACK,))
        self.assertEqual(a.outcome, o)                                    # confidence/value untouched
        self.assertEqual(a.outcome.confidence, 0.4)
        self.assertIsNone(join(dec(), [out()]).outcomes[0].outcome.confidence)   # not invented

    def test_fallback_other_chat_ignored(self):
        self.assertEqual(join(dec(), [out(chat=6)]).outcomes, ())

    def test_multiple_decisions_nearest_preceding_only(self):
        d1, d2 = dec("pd_1", at=T0), dec("pd_2", at=T0 + 100 * S)
        o = out(at=T0 + 200 * S)
        s1, s2 = join_all([d1, d2], [o])
        self.assertEqual(s1.outcomes, ())                                 # never attached to both
        self.assertEqual([(a.outcome, a.attribution) for a in s2.outcomes], [(o, ATTR_FALLBACK)])
        # outcome between D1 and D2 belongs to D1 only
        o2 = out("o2", at=T0 + 50 * S)
        s1, s2 = join_all([d1, d2], [o2])
        self.assertEqual(len(s1.outcomes), 1)
        self.assertEqual(s2.outcomes, ())
        # via join(): context decisions decide the nearest one
        self.assertEqual(join(d1, [o], decisions=[d2]).outcomes, ())
        self.assertEqual(len(join(d2, [o], decisions=[d1]).outcomes), 1)
        total = sum(len(s.outcomes) for s in join_all([d1, d2], [o, o2]))
        self.assertEqual(total, 2)

    def test_nearest_preceding_respects_window(self):
        d1, d2 = dec("pd_1", at=T0), dec("pd_2", at=T0 + 1000 * S)
        o = out(at=T0 + 1500 * S)
        s1, s2 = join_all([d1, d2], [o], outcome_window_seconds=600)
        self.assertEqual((s1.outcomes, len(s2.outcomes)), ((), 1))
        s1, s2 = join_all([d1, d2], [out(at=T0 + 1700 * S)], outcome_window_seconds=600)
        self.assertEqual((s1.outcomes, s2.outcomes), ((), ()))            # nearest out of window, older also out

    def test_mixed_explicit_and_fallback(self):
        o_exp, o_fb = out("a", at=T0 + 5 * S, did="pd_1"), out("b", at=T0 + 6 * S, typ="REGISTRATION")
        r = join(dec(), [o_fb, o_exp])
        self.assertEqual(r.attribution, (ATTR_EXPLICIT, ATTR_FALLBACK))
        self.assertEqual({a.outcome.outcome_id: a.attribution for a in r.outcomes},
                         {"a": ATTR_EXPLICIT, "b": ATTR_FALLBACK})

    def test_multiple_outcomes_all_preserved(self):
        types = ("PAYMENT_ATTEMPT", "PAYMENT_SUCCESS", "DEPOSIT")
        items = [out(f"o{i}", at=T0 + (i + 1) * S, typ=t) for i, t in enumerate(types)]
        r = join(dec(), items)
        self.assertEqual([a.outcome.outcome_type.value for a in r.outcomes], list(types))
        same_ts = [out("x", typ="DEPOSIT"), out("y", typ="DEPOSIT")]
        self.assertEqual(len(join(dec(), same_ts).outcomes), 2)           # no "main outcome" chosen

    def test_deterministic_ordering(self):
        items = [out("c", at=T0 + 30 * S, typ="DROPPED"), out("a", at=T0 + 10 * S, typ="DEPOSIT"),
                 out("b", at=T0 + 10 * S, typ="CONTINUED"), out("z", at=T0 + 10 * S, typ="DEPOSIT", did="pd_1"),
                 out("d", at=T0 + 20 * S, typ="PAYMENT_FAILED")]
        base = join(dec(), items)
        rnd = random.Random(1)
        for _ in range(20):
            sh = items[:]
            rnd.shuffle(sh)
            self.assertEqual(join(dec(), sh), base)
        order = [(a.outcome.observed_at, a.outcome.outcome_type.value, a.outcome.outcome_id) for a in base.outcomes]
        self.assertEqual(order, sorted(order))

    def test_deterministic_dedup(self):
        o = out("o1", conf=0.5)
        r = join(dec(), [o, out("o1", conf=0.5), o])
        self.assertEqual(len(r.outcomes), 1)
        r = join(dec(), [out("o1", conf=0.5), out("o1", conf=0.6)])       # same id, different content: both kept
        self.assertEqual(len(r.outcomes), 2)
        r = join(dec(), [out("o1"), out("o2")])                           # different ids: never merged
        self.assertEqual(len(r.outcomes), 2)
        self.assertEqual(join(dec(), [o, o, o]), join(dec(), [o]))

    def test_invalid_inputs(self):
        with self.assertRaises(ContractError):
            join(dec(), [out(), {"outcome_id": "x"}])
        with self.assertRaises(ContractError):
            join(dec("pd_1"), [], decisions=[dec("pd_1", at=T0 + S)])     # conflicting duplicate decision
        with self.assertRaises(ContractError):
            DecisionRef("pd_1", 5, datetime(2026, 1, 1))                  # naive time
        with self.assertRaises(ContractError):
            DecisionRef("", 5, T0)
        self.assertEqual(join(dec(), iter([])).outcomes, ())

    def test_decision_forms(self):
        class PD:
            decision_id, chat_id, created_at = "pd_1", 5, T0
        for d in (PD(), {"decision_id": "pd_1", "chat_id": 5, "created_at": T0}, dec()):
            self.assertEqual(len(join(d, [out()]).outcomes), 1)

    def test_immutable_result_and_no_input_mutation(self):
        items = [out("a"), out("b")]
        before = [o.to_dict() for o in items]
        r = join(dec(), items)
        self.assertEqual([o.to_dict() for o in items], before)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            r.decision_id = "x"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            r.outcomes[0].attribution = "x"
        self.assertIsInstance(r.outcomes, tuple)

    def test_no_causal_fields_or_vocabulary(self):
        banned = ("caus", "uplift", "increment", "effect", "lift")
        names = []
        for cls in (joiner.OutcomeSet, joiner.AttributedOutcome, joiner.OutcomeWindow, joiner.DecisionRef):
            names += [f.name for f in dataclasses.fields(cls)]
        src = [ROOT / "copilot_outcomes" / n for n in ("joiner.py", "contracts.py")]
        tree_names = []
        for f in src:
            for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if isinstance(n, ast.Name):
                    tree_names.append(n.id)
                elif isinstance(n, ast.Attribute):
                    tree_names.append(n.attr)
                elif isinstance(n, ast.FunctionDef):
                    tree_names.append(n.name)
        for nm in names + tree_names:
            for b in banned:
                self.assertNotIn(b, nm.lower(), nm)
        self.assertEqual(set(names), {"decision_id", "outcomes", "attribution", "window", "outcome",
                                      "seconds", "start", "end", "chat_id", "created_at"})


class SafetyTests(unittest.TestCase):
    # persistence modules (phase 4C) are guarded separately by test_repository
    FILES = [ROOT / "copilot_outcomes" / n for n in ("__init__.py", "contracts.py", "joiner.py")]
    ALLOWED = {"json", "dataclasses", "datetime", "enum", "typing", "copilot_evidence", "copilot_outcomes"}
    BANNED = ("main", "telethon", "fastapi", "sqlalchemy", "asyncio", "aiohttp", "httpx", "requests", "socket",
              "urllib", "pickle", "os", "subprocess", "openai", "anthropic", "copilot_decisions", "copilot_shadow",
              "copilot_external", "scout", "client_state", "random")

    def test_imports_and_calls(self):
        for f in self.FILES:
            for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                mods = ([a.name for a in n.names] if isinstance(n, ast.Import)
                        else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
                for m in mods:
                    self.assertNotIn(m.split(".")[0], self.BANNED, f"{f.name}: {m}")
                    self.assertIn(m.split(".")[0], self.ALLOWED, f"{f.name}: {m}")
                self.assertNotIsInstance(n, (ast.AsyncFunctionDef, ast.Await))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                    self.assertNotIn(n.func.id, {"open", "eval", "exec", "compile", "__import__"})
            txt = f.read_text(encoding="utf-8").lower()
            for w in ("create table", "insert into", "datetime.now", "utcnow"):
                self.assertNotIn(w, txt, f.name)

    def test_nothing_in_production_references_outcomes(self):
        prod = [ROOT / n for n in ("client_state.py", "scout.py", "run.py")]   # main.py: exactly one capture hook, see test_capture
        for d in ("copilot_decisions", "copilot_shadow", "copilot_external", "copilot_evidence"):
            prod += list((ROOT / d).glob("*.py")) + list((ROOT / d).glob("adapters/*.py"))
        for f in prod:
            if f.exists():
                self.assertNotIn("copilot_outcomes", f.read_text(encoding="utf-8"), f.name)

    def test_no_db_or_forbidden_modules(self):
        for p in ("attribution.py", "reports.py"):          # capture.py/persistence are legit since 4C/4D
            self.assertFalse((ROOT / "copilot_outcomes" / p).exists(), p)
        for p in ("policy", "uplift", "delta_state", "bandit", "decision_engine.py"):
            self.assertFalse((ROOT / p).exists(), p)

    def test_import_pulls_no_runtime(self):
        import subprocess
        import sys
        code = ("import sys, copilot_outcomes;"
                "bad=[m for m in ('main','telethon','sqlalchemy','fastapi','asyncio','socket','requests','httpx',"
                "'subprocess','copilot_decisions','copilot_shadow','copilot_external','scout') if m in sys.modules];"
                "print(','.join(bad)); sys.exit(1 if bad else 0)")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
