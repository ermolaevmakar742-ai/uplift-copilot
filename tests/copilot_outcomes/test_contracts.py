import copy
import dataclasses
import json
import unittest
from datetime import datetime, timezone

from copilot_evidence.contracts import ContractError
from copilot_outcomes import contracts as C
from copilot_outcomes.contracts import Outcome, OutcomeSource, OutcomeType

T = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def make(**kw):
    base = dict(outcome_id="o1", decision_id="pd_1", chat_id=5, observed_at=T, outcome_type="DEPOSIT",
                value={"amount": 10, "tags": ["a"]}, confidence=0.8, source="POSTBACK", metadata={"m": {"n": [1]}})
    base.update(kw)
    return Outcome(**base)


class OutcomeContractTests(unittest.TestCase):
    def test_valid(self):
        o = make()
        self.assertIs(o.outcome_type, OutcomeType.DEPOSIT)
        self.assertIs(o.source, OutcomeSource.POSTBACK)
        self.assertEqual(o.contract_version, "outcome.v1")
        self.assertEqual(C.OUTCOME_CONTRACT_VERSION, "outcome.v1")
        self.assertIsNone(make(decision_id=None).decision_id)
        self.assertIsNone(make(confidence=None).confidence)

    def test_taxonomy(self):
        self.assertEqual({t.value for t in OutcomeType}, {
            "DEPOSIT", "REGISTRATION", "PAYMENT_ATTEMPT", "PAYMENT_SUCCESS", "PAYMENT_FAILED", "HUMAN_HANDOFF",
            "DROPPED", "CONTINUED", "UNKNOWN"})
        self.assertEqual({s.value for s in OutcomeSource}, {"POSTBACK", "TELEGRAM_EVENT", "PRODUCTION", "SYSTEM", "UNKNOWN"})
        self.assertNotIn("LLM", {s.value for s in OutcomeSource})
        self.assertIsNot(OutcomeType.REGISTRATION, OutcomeType.DEPOSIT)
        self.assertIsNot(OutcomeType.PAYMENT_ATTEMPT, OutcomeType.PAYMENT_SUCCESS)

    def test_default_window_is_contract_constant(self):
        self.assertIsInstance(C.DEFAULT_OUTCOME_WINDOW_SECONDS, int)
        self.assertGreater(C.DEFAULT_OUTCOME_WINDOW_SECONDS, 0)

    def test_invalid_confidence(self):
        for v in (-0.1, 1.1, float("nan"), float("inf"), "0.5", True):
            with self.assertRaises(ContractError, msg=repr(v)):
                make(confidence=v)
        make(confidence=0), make(confidence=1)

    def test_invalid_type_and_source(self):
        for v in ("deposit", "LLM_GUESS", "", None, "REGISTERED"):
            with self.assertRaises(ContractError):
                make(outcome_type=v)
        for v in ("LLM", "postback", "", None, "SHADOW"):
            with self.assertRaises(ContractError):
                make(source=v)

    def test_invalid_misc(self):
        for kw in (dict(outcome_id=""), dict(outcome_id=None), dict(decision_id="x_1"), dict(decision_id="pd_"),
                   dict(decision_id=5), dict(chat_id="5"), dict(chat_id=True), dict(observed_at=datetime(2026, 1, 1)),
                   dict(observed_at="junk"), dict(value=object()), dict(metadata=None), dict(metadata=[1]),
                   dict(contract_version="outcome.v2")):
            with self.assertRaises(ContractError, msg=str(kw)):
                make(**kw)

    def test_immutable(self):
        o = make()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            o.confidence = 0.1
        with self.assertRaises(TypeError):
            o.metadata["x"] = 1
        with self.assertRaises(TypeError):
            o.metadata["m"]["n2"] = 1
        with self.assertRaises(TypeError):
            o.value["amount"] = 1

    def test_input_mutation_does_not_leak(self):
        meta, val = {"a": [1]}, {"b": {"c": 1}}
        o = make(metadata=meta, value=val)
        meta["a"].append(2)
        val["b"]["c"] = 9
        self.assertEqual(o.to_dict()["metadata"], {"a": [1]})
        self.assertEqual(o.to_dict()["value"], {"b": {"c": 1}})

    def test_round_trip(self):
        for o in (make(), make(decision_id=None, confidence=None, outcome_type="UNKNOWN", source="UNKNOWN")):
            d = json.loads(json.dumps(o.to_dict()))
            self.assertEqual(Outcome.from_dict(d), o)
            self.assertEqual(Outcome.from_dict(d).to_dict(), o.to_dict())

    def test_from_dict_strict(self):
        good = make().to_dict()
        for k in list(good):
            bad = copy.deepcopy(good)
            del bad[k]
            with self.assertRaises(ContractError, msg=k):
                Outcome.from_dict(bad)
        bad = dict(good, extra=1)
        with self.assertRaises(ContractError):
            Outcome.from_dict(bad)
        with self.assertRaises(ContractError):
            Outcome.from_dict([])

    def test_no_causal_fields(self):
        names = {f.name for f in dataclasses.fields(Outcome)}
        self.assertEqual(names, {"outcome_id", "decision_id", "chat_id", "observed_at", "outcome_type", "value",
                                 "confidence", "source", "metadata", "contract_version"})


if __name__ == "__main__":
    unittest.main()
