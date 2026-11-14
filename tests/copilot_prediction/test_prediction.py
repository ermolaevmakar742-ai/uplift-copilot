import ast
import copy
import dataclasses
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from copilot_attribution.contract import AttributionPolicy, OutcomePolicy, PolicyKind
from copilot_attribution.rules_v2 import BoundaryRef, attribute_all_v2
from copilot_attribution.contract import DecisionRef
from copilot_evidence.contracts import ContractError
from copilot_outcomes.contracts import Outcome, OutcomeType as OT
from copilot_prediction import migrate, store
from copilot_prediction.contract import (EXPECTATIONS, ObservationStatus as S, Prediction, PredictionComparison, Relation as R,
                                      make_prediction_id)
from copilot_prediction.evaluator import Observation, compare, observe_decision, predictions_from_shadow
from copilot_prediction.repository import PredictionConflict, PredictionCorrupt, PredictionRepository, PredictionStoreError

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
SEC, MIN, H = timedelta(seconds=1), timedelta(minutes=1), timedelta(hours=1)
POLICY = AttributionPolicy({OT.CONTINUED: OutcomePolicy(PolicyKind.WINDOW, 3600, "t"),
                            OT.DEPOSIT: OutcomePolicy(PolicyKind.UNKNOWN, None, "t")})


def out(oid="o1", at=T0 + 5 * MIN, typ="CONTINUED", chat=5):
    return Outcome(outcome_id=oid, decision_id=None, chat_id=chat, observed_at=at, outcome_type=typ, value=None,
                   confidence=None, source="TELEGRAM_EVENT", metadata={})


def results_for(outcomes, decisions=(DecisionRef("pd_1", 5, T0),), boundaries=None):
    b = boundaries if boundaries is not None else [BoundaryRef("rb_1", "pd_1", 5, T0 + 14 * SEC, "SENT", True, 0, 1)]
    return attribute_all_v2(outcomes, list(decisions), b, policy=POLICY)


def pred(direction="RISING", did="pd_1", recorded=T0 + 20 * SEC):
    row = {"decision_id": did, "chat_id": 5, "created_at": recorded, "decision_created_at": T0,
           "momentum": {"direction": direction, "user_turns_seen": 3}}
    return predictions_from_shadow([row])[0]


def obs(outcomes, *, evaluated=T0 + 5 * H, boundary=T0 + 14 * SEC, capture_since=T0 - H, targets=("CONTINUED",), results=None):
    return observe_decision("pd_1", 5, boundary, results_for(outcomes) if results is None else results, evaluated_at=evaluated,
                            target_types=targets, policy=POLICY, capture_since=capture_since)


class ObservationTests(unittest.TestCase):
    def test_observed(self):
        o = obs([out()])
        self.assertEqual((o.status, o.reason, o.outcome_ids, o.outcome_types), (S.OBSERVED, "attributed_outcome_observed", ("o1",), ("CONTINUED",)))

    def test_no_outcome_is_neutral_only_after_the_window_with_capture_evidence(self):
        o = obs([])
        self.assertEqual((o.status, o.reason), (S.NO_OUTCOME, "window_elapsed_no_attributed_outcome"))
        self.assertEqual(o.outcome_ids, ())
        c = compare(pred(), o, evaluated_at=T0 + 5 * H)
        self.assertEqual((c.status, c.relation), (S.NO_OUTCOME, R.NOT_COMPARABLE))             # never DIFFERENT / negative

    def test_unknown_cases(self):
        self.assertEqual(obs([], boundary=None, results=[]).reason, "no_sent_response_boundary")
        self.assertEqual(obs([], evaluated=T0 + 30 * MIN).reason, "window_open")                  # window not over yet
        self.assertEqual(obs([], evaluated=T0 + 14 * SEC + H).reason, "window_open")              # exactly at the end: still open
        self.assertEqual(obs([], capture_since=None).reason, "no_outcome_capture_evidence")
        self.assertEqual(obs([], capture_since=T0 + H).reason, "outcome_capture_started_later")
        self.assertEqual(obs([], targets=("DEPOSIT",)).reason, "window_unknown_for_target")
        self.assertEqual(obs([], targets=()).reason, "window_unknown_for_target")
        for o in (obs([], boundary=None, results=[]), obs([], capture_since=None)):
            self.assertEqual(o.status, S.UNKNOWN)

    def test_ambiguous_and_unresolved_attribution_is_unknown_not_no_outcome(self):
        ds = [DecisionRef("pd_1", 5, T0), DecisionRef("pd_2", 5, T0 + 10 * MIN)]
        bs = [BoundaryRef("rb_1", "pd_1", 5, T0 + 14 * SEC, "SENT", True), BoundaryRef("rb_2", "pd_2", 5, T0 + 10 * MIN + 14 * SEC, "SENT", True)]
        res = results_for([out(at=T0 + 20 * MIN)], ds, bs)
        o = obs([], results=res)
        self.assertEqual((o.status, o.reason), (S.UNKNOWN, "ambiguous_attribution"))
        res = results_for([out(at=T0 + 5 * MIN)], ds, [])                                          # no boundaries: unverifiable
        self.assertEqual(obs([], results=res).reason, "unresolved_attribution")

    def test_attributed_to_another_decision_does_not_count(self):
        ds = [DecisionRef("pd_1", 5, T0), DecisionRef("pd_2", 5, T0 + 2 * H)]
        bs = [BoundaryRef("rb_1", "pd_1", 5, T0 + 14 * SEC, "SENT", True), BoundaryRef("rb_2", "pd_2", 5, T0 + 2 * H + 14 * SEC, "SENT", True)]
        res = results_for([out(at=T0 + 2 * H + 5 * MIN)], ds, bs)                                  # belongs to pd_2
        self.assertEqual(obs([], results=res, evaluated=T0 + 9 * H).status, S.NO_OUTCOME)

    def test_non_target_outcome_types_ignored(self):
        res = results_for([out(typ="DEPOSIT")])
        self.assertEqual(obs([], results=res).status, S.NO_OUTCOME)


class ComparisonTests(unittest.TestCase):
    def test_prediction_only_from_a_real_stated_direction(self):
        self.assertEqual(EXPECTATIONS, {"RISING": ("CONTINUED",), "FALLING": (), "STABLE": ()})
        p = pred("RISING")
        self.assertEqual((p.expected_outcome_types, p.expectation_version, p.source_record_id), (("CONTINUED",), "expectation.v1", "pd_1"))
        for m in (None, {}, {"error": "KeyError"}, {"direction": "RISING", "user_turns_seen": 0}, {"direction": "SIDEWAYS", "user_turns_seen": 3},
                  {"user_turns_seen": 3}, "RISING"):
            rows = [{"decision_id": "pd_1", "chat_id": 5, "created_at": T0, "decision_created_at": T0, "momentum": m}]
            self.assertEqual(predictions_from_shadow(rows), (), m)                                   # no fake prediction
        self.assertEqual(predictions_from_shadow([{"decision_id": "pd_1", "chat_id": 5, "momentum": {"direction": "RISING", "user_turns_seen": 1}}]), ())

    def test_observed_consistent_and_different(self):
        o = obs([out()])
        c = compare(pred("RISING"), o, evaluated_at=T0 + 5 * H)
        self.assertEqual((c.status, c.relation, c.outcome_ids, c.reason), (S.OBSERVED, R.CONSISTENT, ("o1",), "attributed_outcome_observed"))
        d = compare(pred("RISING"), Observation(S.OBSERVED, "attributed_outcome_observed", ("o9",), ("REGISTRATION",)), evaluated_at=T0 + 5 * H)
        self.assertEqual(d.relation, R.DIFFERENT)
        for direction in ("FALLING", "STABLE"):                                                      # no stated expectation
            n = compare(pred(direction), o, evaluated_at=T0 + 5 * H)
            self.assertEqual((n.status, n.relation, n.reason), (S.OBSERVED, R.NOT_COMPARABLE, "no_expectation_stated"))

    def test_no_leakage_prediction_recorded_after_the_outcome(self):
        late = pred("RISING", recorded=T0 + 10 * MIN)                                              # written after the outcome at +5m
        c = compare(late, obs([out()]), evaluated_at=T0 + 5 * H, first_outcome_at=T0 + 5 * MIN)
        self.assertEqual((c.status, c.relation, c.reason, c.outcome_ids), (S.UNKNOWN, R.NOT_COMPARABLE, "prediction_recorded_after_outcome", ()))
        ok = compare(pred("RISING"), obs([out()]), evaluated_at=T0 + 5 * H, first_outcome_at=T0 + 5 * MIN)
        self.assertEqual(ok.relation, R.CONSISTENT)

    def test_unknown_comparison_carries_no_outcomes_or_relation(self):
        c = compare(pred(), Observation(S.UNKNOWN, "window_open"), evaluated_at=T0 + MIN)
        self.assertEqual((c.status, c.relation, c.outcome_ids, c.observed_outcome_types), (S.UNKNOWN, R.NOT_COMPARABLE, (), ()))

    def test_contract_strict_deterministic_immutable(self):
        p, c = pred(), compare(pred(), obs([out()]), evaluated_at=T0 + 5 * H)
        self.assertEqual(pred().prediction_id, p.prediction_id)
        self.assertEqual(compare(pred(), obs([out()]), evaluated_at=T0 + 5 * H).comparison_id, c.comparison_id)
        self.assertNotEqual(compare(pred(), obs([out()]), evaluated_at=T0 + 6 * H).comparison_id, c.comparison_id)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            c.status = S.UNKNOWN
        with self.assertRaises(TypeError):
            c.metadata["x"] = 1
        for obj, cls in ((p, Prediction), (c, PredictionComparison)):
            d = json.loads(json.dumps(obj.to_dict()))
            self.assertEqual(cls.from_dict(d), obj)
            for k in list(d):
                bad = copy.deepcopy(d)
                del bad[k]
                with self.assertRaises(ContractError, msg=k):
                    cls.from_dict(bad)
            with self.assertRaises(ContractError):
                cls.from_dict({**d, "extra": 1})
        bad = c.to_dict(); bad["comparison_id"] = "pc_forged"
        with self.assertRaises(ContractError):
            PredictionComparison.from_dict(bad)
        bad = c.to_dict(); bad["outcome_ids"] = []
        with self.assertRaises(ContractError):
            PredictionComparison.from_dict(bad)
        bad = c.to_dict(); bad["status"] = "NO_OUTCOME"
        with self.assertRaises(ContractError):
            PredictionComparison.from_dict(bad)
        bad = p.to_dict(); bad["expected_outcome_types"] = []
        with self.assertRaises(ContractError):
            Prediction.from_dict(bad)
        bad = p.to_dict(); bad["source_record_id"] = ""
        with self.assertRaises(ContractError):
            Prediction.from_dict(bad)
        with self.assertRaises(ContractError):
            Prediction(make_prediction_id("pd_1", "MOMENTUM_DIRECTION", "RISING", "x"), "pd_1", 5, "MOMENTUM_DIRECTION", "RISING",
                       ("CONTINUED",), "expectation.v1", "x", datetime(2026, 1, 1), T0)           # naive time

    def test_deterministic_prediction_set_order(self):
        rows = [{"decision_id": f"pd_{i}", "chat_id": 5, "created_at": T0, "decision_created_at": T0,
                 "momentum": {"direction": "RISING", "user_turns_seen": 2}} for i in (3, 1, 2)]
        self.assertEqual(predictions_from_shadow(rows), predictions_from_shadow(list(reversed(rows))))


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        await migrate.ensure_schema(self.engine)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        self.repo = PredictionRepository(self.sf)
        self.c = compare(pred(), obs([out()]), evaluated_at=T0 + 5 * H)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def raw(self, sql):
        async with self.sf() as s:
            await s.execute(text(sql))
            await s.commit()

    async def test_migration_sql(self):
        pg = postgresql.dialect()
        sql = " ".join(migrate.plan(pg))
        self.assertIn("CREATE TABLE IF NOT EXISTS prediction_comparisons", sql)
        self.assertIn("BEFORE UPDATE ON prediction_comparisons", sql)
        self.assertEqual(set(store.metadata.tables), {"prediction_comparisons"})
        self.assertEqual(migrate.rollback_plan(pg)[-1], "DROP TABLE IF EXISTS prediction_comparisons")
        await migrate.ensure_schema(self.engine)
        await migrate.rollback_schema(self.engine)

    async def test_save_get_list_exists_idempotent_conflict(self):
        self.assertFalse(await self.repo.exists(self.c.comparison_id))
        self.assertTrue(await self.repo.save(self.c))
        self.assertFalse(await self.repo.save(self.c))
        self.assertEqual(await self.repo.get(self.c.comparison_id), self.c)
        later = compare(pred(), obs([]), evaluated_at=T0 + 9 * H)                                    # re-evaluation = new row
        await self.repo.save(later)
        self.assertEqual([x.status for x in await self.repo.list_by_decision("pd_1")], [S.OBSERVED, S.NO_OUTCOME])
        self.assertEqual(await self.repo.list_by_decision("pd_none"), ())
        other = dataclasses.replace(self.c, metadata={"different": 1})
        with self.assertRaises(PredictionConflict):
            await self.repo.save(other)
        for bad in (None, self.c.to_dict()):
            with self.assertRaises(TypeError):
                await self.repo.save(bad)

    async def test_corruption_rejected(self):
        await self.repo.save(self.c)
        for setter in ("status='NO_OUTCOME'", "chat_id=6", "decision_id='pd_x'", "contract_version='v9'", "payload='not json'", "payload='[1]'"):
            await self.raw("DELETE FROM prediction_comparisons")
            await self.repo.save(self.c)
            try:
                await self.raw(f"UPDATE prediction_comparisons SET {setter}")
            except Exception:
                continue
            with self.assertRaises(PredictionCorrupt, msg=setter):
                await self.repo.get(self.c.comparison_id)
        self.assertEqual({n for n in dir(PredictionRepository) if not n.startswith("_")}, {"save", "get", "list_by_decision", "exists"})
        self.assertTrue(issubclass(PredictionConflict, PredictionStoreError))


if __name__ == "__main__":
    unittest.main()
