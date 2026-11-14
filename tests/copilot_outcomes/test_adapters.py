import ast
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from copilot_outcomes.adapters import adapt, adapt_postback, adapt_production_event, adapt_telegram_event
from copilot_outcomes.contracts import Outcome, OutcomeSource as S, OutcomeType as T
from copilot_outcomes.joiner import DecisionRef, join

ROOT = Path(__file__).resolve().parents[2]
TS = 1790942400                                   # 2026-10-02T12:00:00Z
DT = datetime.fromtimestamp(TS, tz=timezone.utc)


def pb(**kw):
    d = {"user_id": 777, "event": "deposit_success", "event_id": "evt-1", "payload": None, "amount": 25.5,
         "timestamp": TS}
    d.update(kw)
    return d


def tg(**kw):
    d = {"id": 42, "user_id": 777, "role": "user", "content": "quiero depositar ya", "created_at": DT}
    d.update(kw)
    return d


def fe(**kw):
    d = {"id": 9, "user_id": 777, "old_stage": "conversation", "new_stage": "lost", "source": "admin",
         "event": "manual_stage_change", "created_at": DT}
    d.update(kw)
    return d


def one(res):
    assert len(res.outcomes) == 1, res
    return res.outcomes[0]


class PostbackTests(unittest.TestCase):
    def test_event_mapping(self):
        for ev, want in (("deposit_attempt", T.PAYMENT_ATTEMPT), ("deposit_success", T.DEPOSIT),
                         ("register_complete", T.REGISTRATION)):
            o = one(adapt_postback(pb(event=ev)))
            self.assertIs(o.outcome_type, want, ev)
            self.assertIs(o.source, S.POSTBACK)
            self.assertEqual(o.chat_id, 777)
        self.assertIsNot(T.PAYMENT_ATTEMPT, T.PAYMENT_SUCCESS)

    def test_deposit_not_payment_success_and_attempt_not_success(self):
        self.assertEqual(len(adapt_postback(pb()).outcomes), 1)               # one event -> one outcome
        self.assertIs(one(adapt_postback(pb())).outcome_type, T.DEPOSIT)      # not also PAYMENT_SUCCESS
        self.assertIs(one(adapt_postback(pb(event="deposit_attempt"))).outcome_type, T.PAYMENT_ATTEMPT)

    def test_no_payment_success_or_failed_inferred(self):
        produced = set()
        for ev in ("visit", "register_start", "register_complete", "card_added", "deposit_attempt", "deposit_success",
                   "withdraw_request", "withdraw_success", "reject", "payment_success", "payment_failed", "x"):
            produced |= {o.outcome_type for o in adapt_postback(pb(event=ev)).outcomes}
        self.assertEqual(produced, {T.PAYMENT_ATTEMPT, T.DEPOSIT, T.REGISTRATION})

    def test_unsupported_events_skipped_with_reason(self):
        for ev, reason in (("reject", "ambiguous_rejection"), ("register_start", "registration_started_is_not_registration"),
                           ("visit", "visit_is_not_an_outcome_type"), ("card_added", "no_outcome_type_for_card_added"),
                           ("withdraw_success", "no_outcome_type_for_withdrawals"), ("bogus", "unknown_event")):
            r = adapt_postback(pb(event=ev))
            self.assertEqual((r.outcomes, r.skipped[0].reason), ((), reason), ev)
        for bad in (None, {}, "x", pb(event=""), pb(event=None)):
            self.assertEqual(adapt_postback(bad).outcomes, ())

    def test_decision_id(self):
        o = one(adapt_postback(pb()))
        self.assertIsNone(o.decision_id)                       # format has none: nothing generated
        self.assertEqual(one(adapt_postback(pb(decision_id="pd_abc"))).decision_id, "pd_abc")
        self.assertEqual(adapt_postback(pb(decision_id="fake")).outcomes, ())    # malformed link is skipped, not repaired

    def test_timestamp_preserved_and_required(self):
        self.assertEqual(one(adapt_postback(pb())).observed_at, DT)
        self.assertEqual(one(adapt_postback(pb(timestamp=None, created_at=DT))).observed_at, DT)  # DB-row form
        for bad in (dict(timestamp=None), dict(timestamp=0), dict(timestamp=-5), dict(timestamp="1790942400"),
                    dict(timestamp=True), dict(timestamp=10 ** 18), dict(timestamp=None, created_at=datetime(2026, 1, 1))):
            r = adapt_postback(pb(**bad))
            self.assertEqual(r.outcomes, (), bad)
            self.assertEqual(r.skipped[0].reason, "missing_or_unreliable_timestamp")
        d = pb()
        del d["timestamp"]
        self.assertEqual(adapt_postback(d).outcomes, ())
        src = (ROOT / "copilot_outcomes" / "adapters" / "postback.py").read_text(encoding="utf-8")
        self.assertNotIn("now(", src)

    def test_deterministic_outcome_id(self):
        a, b = one(adapt_postback(pb())), one(adapt_postback(pb()))
        self.assertEqual(a, b)
        self.assertEqual(a.outcome_id, "postback:777:deposit_success:evt-1")
        self.assertNotEqual(one(adapt_postback(pb(event_id="evt-2"))).outcome_id, a.outcome_id)
        self.assertNotEqual(one(adapt_postback(pb(event="deposit_attempt"))).outcome_id, a.outcome_id)
        self.assertEqual(adapt_postback(pb(event_id=None)).skipped[0].reason, "missing_stable_event_id")
        self.assertEqual(adapt_postback(pb(event_id="has space")).outcomes, ())
        self.assertEqual(adapt_postback(pb(user_id=None)).skipped[0].reason, "missing_chat_id")

    def test_confidence(self):
        self.assertIsNone(one(adapt_postback(pb())).confidence)               # never invented
        self.assertEqual(one(adapt_postback(pb(confidence=0.3))).confidence, 0.3)
        self.assertEqual(adapt_postback(pb(confidence=1.5)).outcomes, ())      # invalid -> skipped

    def test_value_and_pii_minimization(self):
        d = pb(payload="mail me at juan@example.com or +52 55 1234 5678 card 4111111111111111",
               email="juan@example.com", phone="+525512345678", card="4111111111111111", token="sk-secret")
        o = one(adapt_postback(d))
        blob = json.dumps(o.to_dict())
        for secret in ("juan@example.com", "5512345678", "4111111111111111", "sk-secret", "mail me"):
            self.assertNotIn(secret, blob)
        self.assertEqual(o.value, {"amount": 25.5})
        self.assertEqual(dict(o.metadata), {"event": "deposit_success", "event_id": "evt-1", "payload_dropped": True})
        self.assertIsNone(one(adapt_postback(pb(amount=None))).value)
        self.assertIsNone(one(adapt_postback(pb(amount=-1))).value)

    def test_object_input_and_no_mutation(self):
        obj = SimpleNamespace(**pb())
        self.assertEqual(one(adapt_postback(obj)), one(adapt_postback(pb())))
        d = pb(payload="p")
        before = copy.deepcopy(d)
        adapt_postback(d)
        self.assertEqual(d, before)


class TelegramTests(unittest.TestCase):
    def test_continued(self):
        o = one(adapt_telegram_event(tg()))
        self.assertIs(o.outcome_type, T.CONTINUED)
        self.assertIs(o.source, S.TELEGRAM_EVENT)
        self.assertEqual((o.chat_id, o.observed_at, o.outcome_id), (777, DT, "telegram:777:42"))
        self.assertIsNone(o.decision_id)
        self.assertIsNone(o.confidence)

    def test_content_never_interpreted_or_copied(self):
        outs = set()
        for text in ("quiero depositar ya", "ya pagué", "me registré", "deposit done", "ok", "", "estafa"):
            r = adapt_telegram_event(tg(content=text))
            outs |= {o.outcome_type for o in r.outcomes}
            self.assertNotIn(text or "\0", json.dumps([o.to_dict() for o in r.outcomes]))
        self.assertEqual(outs, {T.CONTINUED})
        self.assertNotIn("content", json.dumps(one(adapt_telegram_event(tg())).to_dict()).replace("content_dropped", ""))

    def test_ambiguous_or_non_user_skipped(self):
        for kw in (dict(role="assistant"), dict(role=None), dict(role="bot"), dict(role="")):
            self.assertEqual(adapt_telegram_event(tg(**kw)).outcomes, (), kw)
        for bad in (None, {}, "x"):
            self.assertEqual(adapt_telegram_event(bad).outcomes, ())

    def test_timestamp_required_and_preserved(self):
        for bad in (dict(created_at=None), dict(created_at=datetime(2026, 1, 1)), dict(created_at="junk")):
            r = adapt_telegram_event(tg(**bad))
            self.assertEqual(r.outcomes, (), bad)
            self.assertEqual(r.skipped[0].reason, "missing_or_unreliable_timestamp")
        self.assertEqual(one(adapt_telegram_event(tg(created_at=DT.isoformat()))).observed_at, DT)
        d = tg()
        d.pop("created_at")
        d["date"] = DT                                         # Telethon-style field
        self.assertEqual(one(adapt_telegram_event(d)).observed_at, DT)
        d = tg()
        del d["created_at"]
        self.assertEqual(adapt_telegram_event(d).outcomes, ())

    def test_ids_deterministic_and_required(self):
        self.assertEqual(one(adapt_telegram_event(tg())), one(adapt_telegram_event(tg())))
        self.assertEqual(one(adapt_telegram_event(tg(id=None, message_id=99))).outcome_id, "telegram:777:99")
        r = adapt_telegram_event(tg(id=None))
        self.assertEqual(r.skipped[0].reason, "missing_stable_message_id")
        self.assertEqual(adapt_telegram_event(tg(user_id=None)).outcomes, ())
        self.assertEqual(one(adapt_telegram_event(tg(user_id=None, chat_id=5))).chat_id, 5)

    def test_confidence_and_no_mutation(self):
        self.assertEqual(one(adapt_telegram_event(tg(confidence=0.9))).confidence, 0.9)
        self.assertEqual(adapt_telegram_event(tg(confidence=2)).outcomes, ())
        d = tg()
        before = copy.deepcopy(d)
        adapt_telegram_event(d)
        self.assertEqual(d, before)


class ProductionTests(unittest.TestCase):
    def test_documented_event(self):
        o = one(adapt_production_event(fe()))
        self.assertIs(o.outcome_type, T.DROPPED)
        self.assertIs(o.source, S.PRODUCTION)
        self.assertEqual((o.chat_id, o.observed_at, o.outcome_id), (777, DT, "production:funnel_event:9"))
        self.assertEqual(o.value, {"old_stage": "conversation", "new_stage": "lost"})
        self.assertEqual(dict(o.metadata), {"source": "admin", "event": "manual_stage_change"})
        self.assertIsNone(o.confidence)
        self.assertEqual(one(adapt_production_event(fe(new_stage=" LOST "))).outcome_type, T.DROPPED)

    def test_unsupported_and_duplicates_skipped(self):
        for stage in ("registered", "deposited", "converted", "offer", "blocked", "reactivated", "new"):
            self.assertEqual(adapt_production_event(fe(new_stage=stage)).outcomes, (), stage)
        r = adapt_production_event(fe(new_stage="deposited", source="postback"))
        self.assertEqual(r.skipped[0].reason, "already_represented_by_postback_outcome")
        self.assertEqual(adapt_production_event(fe(source="postback")).outcomes, ())   # even 'lost' via postback: no double count
        for bad in (None, {}, fe(new_stage=None), fe(id=None), fe(user_id=None), fe(created_at=None)):
            self.assertEqual(adapt_production_event(bad).outcomes, ())

    def test_no_invented_events(self):
        produced = set()
        for stage in ("new", "visited", "conversation", "qualified", "offer", "registered", "deposited", "lost",
                      "converted", "manual_mode", "human_handoff"):
            produced |= {o.outcome_type for o in adapt_production_event(fe(new_stage=stage)).outcomes}
        self.assertEqual(produced, {T.DROPPED})
        self.assertNotIn(T.HUMAN_HANDOFF, produced)

    def test_no_mutation_and_determinism(self):
        d = fe()
        before = copy.deepcopy(d)
        self.assertEqual(adapt_production_event(d), adapt_production_event(d))
        self.assertEqual(d, before)


class GeneralTests(unittest.TestCase):
    def test_dispatch_rejects_invalid_source(self):
        for s in ("llm", "shadow", "", None, 5):
            r = adapt(s, pb())
            self.assertEqual((r.outcomes, r.skipped[0].reason), ((), "unsupported_source"), s)
        self.assertEqual(len(adapt("postback", pb()).outcomes), 1)
        self.assertEqual(len(adapt("telegram", tg()).outcomes), 1)
        self.assertEqual(len(adapt("production", fe()).outcomes), 1)

    def test_outputs_are_contract_outcomes_with_valid_sources(self):
        for r in (adapt_postback(pb()), adapt_telegram_event(tg()), adapt_production_event(fe())):
            for o in r.outcomes:
                self.assertIsInstance(o, Outcome)
                self.assertEqual(Outcome.from_dict(json.loads(json.dumps(o.to_dict()))), o)
                self.assertNotEqual(o.source.value, "LLM")

    def test_joiner_integration(self):
        d = DecisionRef("pd_1", 777, DT - timedelta(minutes=5))
        outs = [one(adapt_postback(pb(event="deposit_attempt", timestamp=TS))),
                one(adapt_postback(pb(event="deposit_success", event_id="evt-2", timestamp=TS + 60))),
                one(adapt_telegram_event(tg())),
                one(adapt_postback(pb(event_id="old", timestamp=TS - 3600)))]       # before the decision
        r = join(d, outs, outcome_window_seconds=3600)
        self.assertEqual(sorted(a.outcome.outcome_type.value for a in r.outcomes),
                         ["CONTINUED", "DEPOSIT", "PAYMENT_ATTEMPT"])
        self.assertEqual({a.attribution for a in r.outcomes}, {"temporal_chat_fallback"})   # none had a decision_id


class SafetyTests(unittest.TestCase):
    FILES = sorted((ROOT / "copilot_outcomes" / "adapters").glob("*.py"))
    ALLOWED = {"math", "dataclasses", "datetime", "typing", "copilot_outcomes"}
    BANNED = ("main", "telethon", "fastapi", "sqlalchemy", "asyncio", "aiohttp", "httpx", "requests", "socket",
              "urllib", "pickle", "os", "subprocess", "openai", "anthropic", "copilot_decisions", "copilot_shadow",
              "copilot_external", "copilot_evidence", "scout", "client_state", "random")

    def test_imports_and_calls(self):
        self.assertEqual({f.name for f in self.FILES}, {"__init__.py", "base.py", "postback.py", "telegram.py", "production.py"})
        for f in self.FILES:
            for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                mods = ([a.name for a in n.names] if isinstance(n, ast.Import)
                        else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
                for m in mods:
                    self.assertNotIn(m.split(".")[0], self.BANNED, f"{f.name}: {m}")
                    self.assertIn(m.split(".")[0], self.ALLOWED, f"{f.name}: {m}")
                self.assertNotIsInstance(n, (ast.AsyncFunctionDef, ast.Await))
                if isinstance(n, ast.Call):
                    fn = n.func
                    name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                    self.assertNotIn(name, {"open", "eval", "exec", "compile", "__import__", "now", "utcnow", "today"})
            self.assertNotIn("insert into", f.read_text(encoding="utf-8").lower())

    def test_production_does_not_reference_outcomes(self):
        prod = [ROOT / n for n in ("client_state.py", "scout.py", "run.py")]   # main.py: exactly one capture hook, see test_capture
        for d in ("copilot_decisions", "copilot_shadow", "copilot_external", "copilot_evidence"):
            prod += list((ROOT / d).rglob("*.py"))
        for f in prod:
            if f.exists():
                self.assertNotIn("copilot_outcomes", f.read_text(encoding="utf-8"), f.name)

    def test_import_pulls_no_runtime(self):
        import subprocess
        import sys
        code = ("import sys, copilot_outcomes.adapters;"
                "bad=[m for m in ('main','telethon','sqlalchemy','fastapi','asyncio','socket','requests','httpx',"
                "'subprocess','copilot_decisions','copilot_shadow','copilot_external','scout') if m in sys.modules];"
                "print(','.join(bad)); sys.exit(1 if bad else 0)")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
