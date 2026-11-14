import ast
import dataclasses
import json
import random
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from copilot_attribution.contract import Interruption
from copilot_attribution.rules_v2 import BoundaryRef
from copilot_decisions import migrate as dec_migrate
from copilot_evidence.contracts import ContractError
from copilot_outcomes import migrate as out_migrate
from copilot_outcomes.contracts import Outcome
from copilot_response_boundary import migrate as rb_migrate
from copilot_uplift.contract import DataQuality as Q, UpliftEvaluation
from copilot_uplift.evaluator import UpliftData, evaluate_uplift
from copilot_uplift.policy import POLICY_V1
from copilot_uplift.provider import SqlUpliftProvider, read_only_sql

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
SEC, MIN, H = timedelta(seconds=1), timedelta(minutes=1), timedelta(hours=1)
NOW = T0 + 48 * H


def dec(i, at, chat=5, regime="hard", state=None):
    return {"decision_id": f"pd_{i}", "chat_id": chat, "created_at": at, "regime": regime,
            "state": state if state is not None else {"stage": "new", "intent": "question", "sentiment": 0.7, "region": "MX", "messages_count": i}}


def bnd(i, at, chat=5, status="SENT", idx=0, total=1):
    return BoundaryRef(f"rb_{i}_{idx}_{status}", f"pd_{i}", chat, at, status, status == "SENT", idx, total)


def out(oid, at, chat=5, typ="CONTINUED", did=None):
    return Outcome(outcome_id=oid, decision_id=did, chat_id=chat, observed_at=at, outcome_type=typ, value=None, confidence=None,
                   source="TELEGRAM_EVENT", metadata={})


def ev(decisions=(), boundaries=(), outcomes=(), ints=(), now=NOW, **kw):
    return evaluate_uplift(UpliftData(tuple(decisions), tuple(boundaries), tuple(outcomes), tuple(ints), **kw), POLICY_V1, evaluated_at=now)


class EvaluatorTests(unittest.TestCase):
    def test_empty_dataset_is_insufficient_not_an_error(self):
        e = ev()
        self.assertEqual(e.data_quality, Q.INSUFFICIENT_DATA)
        self.assertEqual(dict(e.metrics), {k: 0 for k in e.metrics})
        self.assertEqual((e.conversion_by_action, e.uplift_estimates, e.control_status), ((), (), "CONTROL_UNAVAILABLE"))
        self.assertEqual(dict(e.coverage), {"response_boundary_coverage": None, "response_boundary_coverage_since_capture": None, "attribution_coverage": None})
        self.assertEqual(dict(e.integrity), {"boundary_violations": 0, "attribution_integrity_errors": 0, "leakage_violations": 0})
        self.assertEqual(UpliftEvaluation.from_dict(json.loads(json.dumps(e.to_dict()))), e)

    def test_reports_every_required_metric_for_a_small_real_looking_set(self):
        decs = [dec(1, T0), dec(2, T0 + 5 * H, regime="casual"), dec(3, T0 + 6 * H, chat=6, regime="hard")]
        bs = [bnd(1, T0 + 14 * SEC), bnd(3, T0 + 6 * H + 14 * SEC, chat=6)]
        outs = [out("o1", T0 + 10 * MIN), out("o2", T0 + 5 * H + MIN)]
        e = ev(decs, bs, outs, now=T0 + 40 * H)
        m = e.metrics
        self.assertEqual((m["unique_leads"], m["decisions"], m["response_boundaries"], m["outcomes"], m["attributed_outcomes"]), (2, 3, 2, 2, 1))
        self.assertEqual((m["eligible_samples"], m["control_samples"], m["treatment_samples"], m["actions"]), (2, 0, 0, 1))
        stats = {s.action_id: s for s in e.conversion_by_action}
        self.assertEqual((stats["ADDRESS_OBJECTION"].eligible, stats["ADDRESS_OBJECTION"].observed, stats["ADDRESS_OBJECTION"].no_outcome), (2, 1, 1))
        self.assertEqual(stats["ADDRESS_OBJECTION"].rate, 0.5)
        self.assertEqual(e.control_status, "CONTROL_UNAVAILABLE")
        self.assertEqual({x.status.value for x in e.uplift_estimates}, {"CONTROL_UNAVAILABLE"})
        self.assertEqual(e.data_quality, Q.INSUFFICIENT_DATA)                  # 2 eligible samples < 30
        self.assertEqual(e.coverage["response_boundary_coverage"], round(2 / 3, 6))
        self.assertEqual(e.coverage["attribution_coverage"], 0.5)
        ms = e.missing_signals
        self.assertEqual((ms["decisions_with_unmapped_action"], ms["decisions_without_sent_boundary"], ms["control_unavailable"],
                          ms["no_randomization_information"]), (1, 1, 1, 1))
        self.assertTrue(any(k.startswith("outcomes_not_attributed:") for k in ms))

    def test_no_ranking_leaderboard_or_best_action(self):
        d = ev([dec(1, T0)], [bnd(1, T0 + 14 * SEC)], [out("o1", T0 + MIN)], now=T0 + 40 * H).to_dict()
        self.assertFalse({"best_action", "ranking", "leaderboard", "winner", "recommended_action"} & set(d))
        blob = json.dumps(d).lower()
        for w in ("best_action", "leaderboard", "winner", "ranking"):
            self.assertNotIn(w, blob)

    def test_unknown_when_window_open_or_no_boundary(self):
        e = ev([dec(1, T0), dec(2, T0 + MIN)], [bnd(1, T0 + 14 * SEC)], [], now=T0 + 2 * H)
        self.assertEqual(e.metrics["eligible_samples"], 0)                      # window still open / no boundary -> UNKNOWN, not "no outcome"
        ms = e.missing_signals
        self.assertEqual(ms["decisions_observation_unknown:no_outcome_capture_evidence"], 1)

    def test_no_outcome_is_neutral_and_needs_capture_evidence(self):
        decs = [dec(1, T0), dec(2, T0 + 20 * H, chat=6)]
        bs = [bnd(1, T0 + 14 * SEC), bnd(2, T0 + 20 * H + 14 * SEC, chat=6)]
        outs = [out("o0", T0 - 5 * H, chat=9)]                                  # earliest outcome = evidence capture was running
        e = ev(decs, bs, outs, now=T0 + 60 * H)
        stats = {s.action_id: s for s in e.conversion_by_action}["ADDRESS_OBJECTION"]
        self.assertEqual((stats.eligible, stats.observed, stats.no_outcome, stats.rate), (2, 0, 2, 0.0))
        # the same decisions but capture evidence starts later than their boundaries -> UNKNOWN
        e2 = ev(decs, bs, [out("o0", T0 + 30 * H, chat=9)], now=T0 + 60 * H)
        self.assertEqual(e2.metrics["eligible_samples"], 0)
        self.assertGreater(e2.missing_signals["decisions_observation_unknown:outcome_capture_started_later"], 0)

    def test_attribution_is_boundary_based_missing_boundary_means_unavailable(self):
        e = ev([dec(1, T0)], [], [out("o1", T0 + 10 * MIN)], now=T0 + 40 * H)
        self.assertEqual(e.metrics["attributed_outcomes"], 0)
        self.assertEqual(e.missing_signals["outcomes_not_attributed:no_sent_response_boundary"], 1)
        failed = ev([dec(1, T0)], [bnd(1, T0 + 14 * SEC, status="FAILED")], [out("o1", T0 + 10 * MIN)], now=T0 + 40 * H)
        self.assertEqual(failed.metrics["attributed_outcomes"], 0)
        self.assertEqual(failed.metrics["response_boundaries"], 0)               # FAILED is never a response boundary

    def test_integrity_counters_are_derived_not_trusted(self):
        bad = ev([dec(1, T0)], [bnd(1, T0 - 5 * SEC)], [], now=T0 + 40 * H)       # SENT before its decision
        self.assertEqual(bad.integrity["boundary_violations"], 1)
        other_chat = ev([dec(1, T0)], [bnd(1, T0 + 14 * SEC, chat=9)], [], now=T0 + 40 * H)
        self.assertEqual(other_chat.integrity["boundary_violations"], 1)
        good = ev([dec(1, T0)], [bnd(1, T0 + 14 * SEC)], [out("o1", T0 + MIN)], now=T0 + 40 * H)
        self.assertEqual(dict(good.integrity), {"boundary_violations": 0, "attribution_integrity_errors": 0, "leakage_violations": 0})

    def test_deterministic_and_order_independent_with_stable_versions(self):
        decs = [dec(i, T0 + i * H, chat=i % 3) for i in range(1, 7)]
        bs = [bnd(i, T0 + i * H + 14 * SEC, chat=i % 3) for i in range(1, 7)]
        outs = [out(f"o{i}", T0 + i * H + 5 * MIN, chat=i % 3) for i in range(1, 5)]
        base = ev(decs, bs, outs, now=T0 + 100 * H)
        rnd = random.Random(3)
        for _ in range(15):
            d2, b2, o2 = decs[:], bs[:], outs[:]
            rnd.shuffle(d2), rnd.shuffle(b2), rnd.shuffle(o2)
            self.assertEqual(ev(d2, b2, o2, now=T0 + 100 * H), base)
        self.assertEqual((base.estimator_version, base.feature_version, base.policy_version), ("estimator.v2", "features.v1", "uplift-policy.v1"))
        self.assertTrue(base.dataset_version.startswith("ds_") and base.evaluation_id.startswith("ue_"))
        self.assertEqual(base.evaluation_timestamp, T0 + 100 * H)               # the timestamp is the argument, never a clock read
        changed = ev(decs, bs, outs[:-1], now=T0 + 100 * H)
        self.assertNotEqual(changed.dataset_version, base.dataset_version)

    def test_truncation_and_totals_come_from_the_source(self):
        e = ev([dec(1, T0)], [], [], unique_leads=777, decisions_total=9000, outcomes_total=4000, boundaries_sent_total=8000, truncated=True)
        self.assertEqual((e.metrics["unique_leads"], e.metrics["decisions"], e.metrics["outcomes"], e.metrics["response_boundaries"], e.truncated),
                         (777, 9000, 4000, 8000, True))

    def test_invalid_input_is_explicit(self):
        with self.assertRaises(ContractError):
            ev([dec(1, T0)], [], ["junk"])
        with self.assertRaises(ContractError):
            evaluate_uplift(UpliftData(), POLICY_V1, evaluated_at=datetime(2026, 1, 1))

    def test_no_llm_network_clock_or_production_imports(self):
        for name in ("evaluator.py", "estimator.py", "features.py", "policy.py", "gates.py", "activation.py", "contract.py", "actions.py"):
            src = (ROOT / "copilot_uplift" / name).read_text(encoding="utf-8")
            for n in ast.walk(ast.parse(src)):
                mods = ([a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
                for m in mods:
                    self.assertNotIn(m.split(".")[0], {"main", "telethon", "fastapi", "requests", "httpx", "aiohttp", "socket", "openai", "anthropic",
                                                       "random", "pickle", "subprocess", "copilot_decisions", "copilot_shadow", "copilot_external",
                                                       "sqlalchemy", "asyncio"}, f"{name}: {m}")
                    self.assertNotIn(m, {"copilot_outcomes.repository", "copilot_response_boundary.repository"}, f"{name}: {m}")
                if isinstance(n, ast.Call):
                    f = n.func
                    nm = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
                    self.assertNotIn(nm, {"now", "utcnow", "today", "time", "open", "eval", "exec"}, f"{name}:{nm}")


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        await dec_migrate.ensure_schema(self.engine)
        await out_migrate.ensure_schema(self.engine)
        await rb_migrate.ensure_schema(self.engine)
        async with self.engine.begin() as c:
            await c.execute(text("CREATE TABLE ai_activity (id integer primary key, user_id bigint, event_type text, created_at timestamp)"))
            for i, (chat, at, regime) in enumerate(((5, T0, "hard"), (5, T0 + H, "casual"), (6, T0 + 2 * H, "hard")), start=1):
                await c.execute(text("INSERT INTO production_decisions (decision_id, chat_id, message_id, created_at, decision_path, regime, client_state_snapshot, metadata) "
                                     "VALUES (:d, :c, :m, :t, 'llm_reply', :r, :s, '{}')"),
                                {"d": f"pd_{i}", "c": chat, "m": i, "t": at.replace(tzinfo=None), "r": regime,
                                 "s": json.dumps({"stage": "new", "intent": "question", "sentiment": 0.7, "region": "MX", "messages_count": i})})
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        from copilot_outcomes.repository import OutcomeRepository
        from copilot_response_boundary.recorder import build_sent
        from copilot_response_boundary.repository import ResponseBoundaryRepository
        await OutcomeRepository(self.sf).save(out("telegram:5:1", T0 + 10 * MIN))
        await ResponseBoundaryRepository(self.sf).save(build_sent(chat_id=5, telegram_message_id=100, sent_at=T0 + 14 * SEC, decision_id="pd_1",
                                                                  part_index=0, parts_total=1, timestamp_source="send_path_clock"))
        self.statements = []

        @event.listens_for(self.engine.sync_engine, "before_cursor_execute")
        def _log(conn, cursor, statement, parameters, context, executemany):
            self.statements.append(statement)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def counts(self):
        async with self.sf() as s:
            return [(await s.execute(text(f"select count(*) from {t}"))).scalar() for t in ("production_decisions", "outcomes", "response_boundaries")]

    async def test_read_only_load_and_evaluate_on_real_schemas(self):
        before = await self.counts()
        self.statements.clear()
        data = await SqlUpliftProvider(self.sf, assume_utc_naive=True).load()
        self.assertEqual(await self.counts(), before)
        for st in self.statements:
            self.assertTrue(st.lstrip().upper().startswith("SELECT"), st[:60])
            self.assertNotRegex(st.upper(), r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b")
        self.assertEqual((data.unique_leads, data.decisions_total, data.outcomes_total, data.boundaries_sent_total, data.truncated), (2, 3, 1, 1, False))
        self.assertEqual((len(data.decisions), len(data.boundaries), len(data.outcomes)), (3, 1, 1))
        e = evaluate_uplift(data, POLICY_V1, evaluated_at=T0 + 40 * H)
        self.assertEqual((e.metrics["unique_leads"], e.metrics["attributed_outcomes"], e.metrics["response_boundaries"]), (2, 1, 1))
        self.assertEqual(e.metrics["eligible_samples"], 1)                       # only pd_1: mapped action + sent boundary + closed window

    async def test_bounds_and_truncation(self):
        data = await SqlUpliftProvider(self.sf, max_decisions=2, assume_utc_naive=True).load()
        self.assertEqual((len(data.decisions), data.decisions_total, data.truncated), (2, 3, True))
        self.assertEqual([d["decision_id"] for d in data.decisions], ["pd_2", "pd_3"])      # most recent, deterministically ordered

    async def test_naive_times_unreadable_unless_assumed_and_corrupt_rows_counted(self):
        data = await SqlUpliftProvider(self.sf, assume_utc_naive=False).load()
        self.assertEqual((len(data.decisions), data.unreadable["decisions"], data.unreadable["boundaries"]), (0, 3, 1))
        async with self.engine.begin() as c:
            await c.execute(text("UPDATE outcomes SET outcome_type='LLM_GUESS'"))
        data = await SqlUpliftProvider(self.sf, assume_utc_naive=True).load()
        self.assertEqual((len(data.outcomes), data.unreadable["outcomes"]), (0, 1))

    async def test_missing_optional_tables_are_reported_not_invented(self):
        async with self.engine.begin() as c:
            await c.execute(text("DROP TABLE response_boundaries"))
            await c.execute(text("DROP TABLE ai_activity"))
        data = await SqlUpliftProvider(self.sf, assume_utc_naive=True).load()
        self.assertEqual((data.tables["response_boundaries"], len(data.boundaries), len(data.interruptions)), (False, 0, 0))
        self.assertTrue(any(n.startswith("response_boundaries_unavailable") for n in data.notes))
        self.assertTrue(any(n.startswith("interruptions_unavailable") for n in data.notes))

    async def test_pg_read_only_statement_and_provider_is_select_only(self):
        self.assertEqual(read_only_sql("postgresql"), "SET TRANSACTION READ ONLY")
        tree = ast.parse((ROOT / "copilot_uplift" / "provider.py").read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                f = n.func
                nm = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
                self.assertNotIn(nm, {"insert", "update", "delete", "commit", "add", "merge", "create_all", "drop_all", "flush"}, nm)


if __name__ == "__main__":
    unittest.main()
