import ast
import copy
import dataclasses
import itertools
import json
import random
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from copilot_evidence.contracts import ContractError
from copilot_state_delta import (FIELDS, FieldStatus as F, StateDelta, StateSnapshot, compute_state_delta,
                              pair_consecutive_decisions)

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
M = timedelta(minutes=1)


def snap(at=T0, **kw):
    base = dict(stage="new", intent="question", sentiment=0.7, region="MX", messages=3, observed_at=at)
    base.update(kw)
    return StateSnapshot(**base)


def delta(b=None, a=None, **kw):
    return compute_state_delta(b or snap(), a or snap(at=T0 + 10 * M), chat_id=5, anchor_decision_id="pd_1", **kw)


class DeltaTests(unittest.TestCase):
    def test_same_state_is_unchanged_everywhere(self):
        d = delta()
        self.assertEqual([f.status for f in d.fields], [F.UNCHANGED] * 5)
        self.assertEqual([f.field for f in d.fields], list(FIELDS))
        self.assertEqual(d.contract_version, "state_delta.v1")

    def test_changed_fields(self):
        d = delta(a=snap(at=T0 + M, stage="offer", intent="ready", sentiment=0.3, region="CO", messages=7))
        self.assertEqual([f.status for f in d.fields], [F.CHANGED] * 5)
        self.assertEqual((d.field("stage").before, d.field("stage").after), ("new", "offer"))
        mixed = delta(a=snap(at=T0 + M, intent="ready"))
        self.assertEqual({f.field: f.status for f in mixed.fields},
                         {"stage": F.UNCHANGED, "intent": F.CHANGED, "sentiment": F.UNCHANGED, "region": F.UNCHANGED, "messages": F.UNCHANGED})

    def test_missing_before_with_existing_after_is_unknown_not_unchanged(self):
        d = delta(b=StateSnapshot(observed_at=T0), a=snap(at=T0 + M))
        self.assertEqual([f.status for f in d.fields], [F.UNKNOWN] * 5)
        d = delta(b=snap(stage=None), a=snap(at=T0 + M))
        self.assertEqual((d.field("stage").status, d.field("intent").status), (F.UNKNOWN, F.UNCHANGED))
        self.assertIsNone(d.field("stage").before)

    def test_missing_after_and_both_missing_are_unknown(self):
        d = delta(a=snap(at=T0 + M, region=None, messages=None))
        self.assertEqual((d.field("region").status, d.field("messages").status, d.field("stage").status), (F.UNKNOWN, F.UNKNOWN, F.UNCHANGED))
        both = delta(b=StateSnapshot(observed_at=T0), a=StateSnapshot(observed_at=T0 + M))
        self.assertTrue(all(f.status is F.UNKNOWN for f in both.fields))             # never "unchanged" from nothing

    def test_zero_and_falsy_values_are_known(self):
        d = delta(b=snap(messages=0, sentiment=0.0), a=snap(at=T0 + M, messages=0, sentiment=0.0))
        self.assertEqual((d.field("messages").status, d.field("sentiment").status), (F.UNCHANGED, F.UNCHANGED))
        d = delta(b=snap(messages=0), a=snap(at=T0 + M, messages=1))
        self.assertEqual(d.field("messages").status, F.CHANGED)

    def test_from_client_state_normalisation(self):
        full = {"stage": "new", "conversation": {"intent": "question", "sentiment": 0.7, "messages_count": 4},
                "identity": {"region": "MX", "name": "SECRET"}}
        s = StateSnapshot.from_client_state(full, T0)
        self.assertEqual((s.stage, s.intent, s.sentiment, s.region, s.messages), ("new", "question", 0.7, "MX", 4))
        self.assertNotIn("SECRET", json.dumps(s.to_dict()))
        flat = StateSnapshot.from_client_state({"stage": "offer", "intent": "ready", "sentiment": 0.3, "region": "CO", "messages_count": 2}, T0)
        self.assertEqual((flat.stage, flat.messages), ("offer", 2))
        empty_text = StateSnapshot.from_client_state({"stage": "new", "intent": "unknown", "sentiment": 0.5, "region": "?", "messages_count": 0}, T0)
        self.assertEqual((empty_text.intent, empty_text.sentiment, empty_text.region, empty_text.messages), (None, None, None, 0))
        for junk in (None, {}, "x", []):
            s = StateSnapshot.from_client_state(junk, T0)
            self.assertEqual([getattr(s, f) for f in FIELDS], [None] * 5)

    def test_after_must_be_later(self):
        for at in (T0, T0 - M):
            with self.assertRaises(ContractError):
                delta(a=snap(at=at))
        delta(b=StateSnapshot(observed_at=None), a=snap(at=T0))                     # unknown time on one side is allowed

    def test_snapshot_validation(self):
        for kw in (dict(stage=""), dict(sentiment=True), dict(sentiment=float("nan")), dict(messages=-1), dict(messages=True),
                   dict(region=5), dict(observed_at=datetime(2026, 1, 1))):
            with self.assertRaises(ContractError, msg=str(kw)):
                snap(**kw)

    def test_deterministic_ids_and_input_isolation(self):
        self.assertEqual(delta().delta_id, delta().delta_id)
        self.assertTrue(delta().delta_id.startswith("sd_"))
        self.assertNotEqual(delta().delta_id, delta(a=snap(at=T0 + M, stage="x")).delta_id)
        self.assertNotEqual(delta().delta_id, compute_state_delta(snap(), snap(at=T0 + 10 * M), chat_id=6, anchor_decision_id="pd_1").delta_id)
        src = {"stage": "new", "conversation": {"intent": "question", "sentiment": 0.7, "messages_count": 1}}
        b = StateSnapshot.from_client_state(src, T0)
        d = compute_state_delta(b, snap(at=T0 + M), chat_id=5)
        before = d.to_dict()
        src["stage"] = "deposited"
        src["conversation"]["intent"] = "objection"
        self.assertEqual(d.to_dict(), before)                                         # snapshot isolation
        with self.assertRaises(dataclasses.FrozenInstanceError):
            d.chat_id = 9
        with self.assertRaises(dataclasses.FrozenInstanceError):
            d.before.stage = "x"
        with self.assertRaises(TypeError):
            d.metadata["x"] = 1

    def test_serialization_round_trip_and_tamper_evidence(self):
        for d in (delta(), delta(b=StateSnapshot(observed_at=T0), a=snap(at=T0 + M)), delta(metadata={"k": [1, 2]})):
            r = StateDelta.from_dict(json.loads(json.dumps(d.to_dict())))
            self.assertEqual(r, d)
            self.assertEqual(r.to_dict(), d.to_dict())
        good = delta().to_dict()
        for k in list(good):
            bad = copy.deepcopy(good)
            del bad[k]
            with self.assertRaises(ContractError, msg=k):
                StateDelta.from_dict(bad)
        cases = []
        for path, v in ((("fields", 0, "status"), "CHANGED"), (("delta_id",), "sd_forged"), (("after", "stage"), "offer"),
                        (("contract_version",), "state_delta.v2"), (("chat_id",), 6), (("fields",), "x"), (("extra",), 1)):
            b = copy.deepcopy(good)
            cur = b
            for p in path[:-1]:
                cur = cur[p]
            cur[path[-1]] = v
            cases.append(b)
        for b in cases:
            with self.assertRaises(ContractError, msg=str(b)[:60]):
                StateDelta.from_dict(b)

    def test_permutation_of_pairing_inputs(self):
        def D(i, chat, at, stage):
            return {"decision_id": f"pd_{i}", "chat_id": chat, "created_at": T0 + at, "state": {"stage": stage, "intent": "question", "sentiment": 0.5, "messages_count": i}}
        ds = [D(1, 5, 0 * M, "new"), D(2, 5, 30 * M, "offer"), D(3, 5, 60 * M, "offer"), D(4, 9, 5 * M, "new"), D(5, 9, 50 * M, "new")]
        ends = {"pd_1": T0 + 14 * timedelta(seconds=1), "pd_2": T0 + 30 * M + timedelta(seconds=14), "pd_4": T0 + 5 * M + timedelta(seconds=9)}
        base = pair_consecutive_decisions(ds, ends)
        self.assertEqual([d.anchor_decision_id for d in base[0]], ["pd_1", "pd_2", "pd_4"])
        self.assertEqual(base[1], ())
        self.assertEqual(base[0][0].field("stage").status, F.CHANGED)
        self.assertEqual(base[0][1].field("stage").status, F.UNCHANGED)
        rnd = random.Random(4)
        for perm in itertools.islice(itertools.permutations(ds), 40):
            self.assertEqual(pair_consecutive_decisions(list(perm), dict(reversed(list(ends.items())))), base)
        for _ in range(10):
            sh = ds[:]
            rnd.shuffle(sh)
            self.assertEqual(pair_consecutive_decisions(sh, ends), base)

    def test_pairing_skips_when_the_action_is_not_known_to_be_sent(self):
        ds = [{"decision_id": "pd_1", "chat_id": 5, "created_at": T0, "state": {"stage": "new"}},
              {"decision_id": "pd_2", "chat_id": 5, "created_at": T0 + 10 * timedelta(seconds=1), "state": {"stage": "new"}}]
        deltas, skipped = pair_consecutive_decisions(ds, {})
        self.assertEqual((deltas, skipped), ((), (("pd_1", "no_sent_response_boundary"),)))
        deltas, skipped = pair_consecutive_decisions(ds, {"pd_1": T0 + 20 * timedelta(seconds=1)})   # next decision before the reply was sent
        self.assertEqual((deltas, skipped), ((), (("pd_1", "next_decision_before_reply_completed"),)))
        d, _ = pair_consecutive_decisions([ds[0], dict(ds[1], state=None)], {"pd_1": T0 + 5 * timedelta(seconds=1)})
        self.assertTrue(all(f.status is F.UNKNOWN for f in d[0].fields))                              # missing state -> UNKNOWN

    def test_no_clock_network_db_or_llm(self):
        for name in ("contract.py", "compute.py", "__init__.py"):
            src = (ROOT / "copilot_state_delta" / name).read_text(encoding="utf-8")
            for n in ast.walk(ast.parse(src)):
                mods = ([a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
                for m in mods:
                    self.assertNotIn(m.split(".")[0], {"main", "sqlalchemy", "asyncio", "os", "socket", "requests", "httpx", "random", "time",
                                                       "pickle", "subprocess", "openai", "anthropic", "telethon", "fastapi", "copilot_decisions",
                                                       "copilot_shadow", "copilot_external"}, f"{name}: {m}")
                if isinstance(n, ast.Call):
                    f = n.func
                    nm = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
                    self.assertNotIn(nm, {"now", "utcnow", "today", "time", "open", "eval", "exec"}, f"{name}:{nm}")


if __name__ == "__main__":
    unittest.main()
