import copy
import dataclasses
import json
import unittest
from datetime import datetime, timezone

from copilot_contracts.stages import Stage
from copilot_evidence.contracts import (EVIDENCE_CONTRACT_VERSION, ContractError, Evidence, EvidenceSource,
                                     EvidenceType, Provenance)

T = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def make(**kw):
    base = dict(
        evidence_id="e-1", chat_id=42, message_id=7, created_at=T, source="shadow",
        evidence_type="MOMENTUM", value={"score": 0.4, "tags": ["a", "b"]}, confidence=0.6,
        reliability=0.5, observed_at=T, stage=Stage.NEED,
        provenance=Provenance("shadow", None), metadata={"k": {"n": [1, 2]}})
    base.update(kw)
    return Evidence(**base)


class EvidenceTests(unittest.TestCase):
    def test_valid(self):
        e = make()
        self.assertIs(e.source, EvidenceSource.SHADOW)
        self.assertIs(e.evidence_type, EvidenceType.MOMENTUM)
        self.assertEqual(e.contract_version, "evidence.v1")
        self.assertEqual(EVIDENCE_CONTRACT_VERSION, "evidence.v1")
        self.assertIsNone(e.provenance.source_id)

    def test_vocabularies(self):
        self.assertEqual({s.value for s in EvidenceSource}, {"production", "client_state", "shadow", "external"})
        self.assertEqual({t.value for t in EvidenceType}, {
            "STAGE", "INTENT", "SENTIMENT", "MOMENTUM", "CONTRADICTION", "BLOCKER", "PAYMENT_SIGNAL",
            "TRUST_SIGNAL", "EXTERNAL_SIGNAL", "CLIENT_FACT", "ACTION", "UNKNOWN"})

    def test_bad_confidence(self):
        for v in (-0.01, 1.01, float("nan"), float("inf"), "0.5", True):
            with self.assertRaises(ContractError, msg=repr(v)):
                make(confidence=v)
        make(confidence=0), make(confidence=1.0)

    def test_bad_reliability(self):
        for v in (-1, 2, float("nan"), "x", False):
            with self.assertRaises(ContractError, msg=repr(v)):
                make(reliability=v)

    def test_none_confidence_reliability_allowed_and_round_trip(self):
        e = make(confidence=None, reliability=None)
        self.assertIsNone(e.confidence)
        self.assertEqual(Evidence.from_dict(json.loads(json.dumps(e.to_dict()))), e)

    def test_bad_source(self):
        for v in ("telegram", "", None, "SHADOW"):
            with self.assertRaises(ContractError):
                make(source=v)

    def test_bad_type(self):
        for v in ("stage", "FOO", None, ""):
            with self.assertRaises(ContractError):
                make(evidence_type=v)

    def test_bad_stage_not_coerced(self):
        for v in ("start", "visited", None, ""):
            with self.assertRaises(ContractError):
                make(stage=v)
        self.assertIs(make(stage="UNKNOWN").stage, Stage.UNKNOWN)

    def test_provenance_required_and_consistent(self):
        for v in (None, {"source": "shadow"}, "x"):
            with self.assertRaises(ContractError):
                make(provenance=v)
        with self.assertRaises(ContractError):
            make(provenance=Provenance("external", "x"))
        with self.assertRaises(ContractError):
            Provenance("shadow", "")
        with self.assertRaises(ContractError):
            Provenance("shadow", None, "evidence.v0")

    def test_bad_contract_version(self):
        with self.assertRaises(ContractError):
            make(contract_version="evidence.v2")

    def test_bad_misc(self):
        with self.assertRaises(ContractError):
            make(evidence_id="")
        with self.assertRaises(ContractError):
            make(chat_id="1")
        with self.assertRaises(ContractError):
            make(created_at=datetime(2026, 1, 1))  # naive
        with self.assertRaises(ContractError):
            make(value=object())
        with self.assertRaises(ContractError):
            make(metadata=[1])
        with self.assertRaises(ContractError):
            make(metadata=None)

    def test_immutable(self):
        e = make()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            e.confidence = 0.9
        with self.assertRaises(TypeError):
            e.metadata["x"] = 1
        with self.assertRaises(TypeError):
            e.metadata["k"]["n2"] = 1
        with self.assertRaises(TypeError):
            e.value["score"] = 1
        with self.assertRaises(dataclasses.FrozenInstanceError):
            e.provenance.source_id = "z"

    def test_input_mutation_does_not_leak(self):
        meta = {"a": [1]}
        e = make(metadata=meta)
        meta["a"].append(2)
        meta["b"] = 1
        self.assertEqual(e.to_dict()["metadata"], {"a": [1]})

    def test_round_trip(self):
        e = make(provenance=Provenance("shadow", "row-9"), message_id=None, observed_at=None)
        d = e.to_dict()
        d2 = json.loads(json.dumps(d))
        self.assertEqual(d, d2)
        e2 = Evidence.from_dict(d2)
        self.assertEqual(e, e2)
        self.assertEqual(e2.to_dict(), d)
        self.assertEqual(d["contract_version"], "evidence.v1")
        self.assertEqual(d["provenance"]["contract_version"], "evidence.v1")

    def test_from_dict_strict(self):
        d = make().to_dict()
        for k in list(d):
            bad = copy.deepcopy(d)
            del bad[k]
            with self.assertRaises(ContractError, msg=k):
                Evidence.from_dict(bad)
        bad = copy.deepcopy(d)
        bad["extra"] = 1
        with self.assertRaises(ContractError):
            Evidence.from_dict(bad)
        bad = copy.deepcopy(d)
        del bad["provenance"]["source_id"]
        with self.assertRaises(ContractError):
            Evidence.from_dict(bad)
        with self.assertRaises(ContractError):
            Evidence.from_dict("x")


if __name__ == "__main__":
    unittest.main()
