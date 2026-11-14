import ast
import unittest
from pathlib import Path

from copilot_contracts import stages
from copilot_contracts.stages import (AMBIGUOUS_PRODUCTION_STAGES, PRODUCTION_STAGE_MAP, Stage,
                                   is_valid_stage, map_production_stage, normalize_stage)

ROOT = Path(__file__).resolve().parents[2]


def _production_stage_map() -> dict:
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "STAGE_MAP" for t in n.targets):
            return ast.literal_eval(n.value)
    raise AssertionError("STAGE_MAP not found")


class StageTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(stages.STAGE_CONTRACT_VERSION, "stage.v1")

    def test_canonical_set(self):
        self.assertEqual({s.value for s in Stage}, {
            "START", "INTEREST", "NEED", "TRUST", "OBJECTION", "PAYMENT", "DEPOSIT", "SUCCESS",
            "DROPPED", "UNKNOWN"})

    def test_all_valid(self):
        for s in Stage:
            self.assertTrue(is_valid_stage(s))
            self.assertTrue(is_valid_stage(s.value))
            self.assertIs(normalize_stage(s.value), s)
            self.assertIs(normalize_stage(s.value.lower()), s)

    def test_invalid_not_valid(self):
        for v in ("start", "", None, 5, "visited", "nope"):
            self.assertFalse(is_valid_stage(v))

    def test_unknown_never_start(self):
        for v in (None, "", "  ", "garbage", 0, [], "visited", "qualified"):
            self.assertIs(normalize_stage(v), Stage.UNKNOWN)
            self.assertIs(map_production_stage(v), Stage.UNKNOWN)
        self.assertIsNot(Stage.UNKNOWN, Stage.START)

    def test_mapping(self):
        self.assertIs(map_production_stage("new"), Stage.START)
        self.assertIs(map_production_stage(" Deposited "), Stage.DEPOSIT)
        self.assertIs(map_production_stage("converted"), Stage.SUCCESS)
        self.assertIs(map_production_stage("lost"), Stage.DROPPED)

    def test_mapping_tables_immutable_and_disjoint(self):
        with self.assertRaises(TypeError):
            PRODUCTION_STAGE_MAP["x"] = Stage.START
        with self.assertRaises(TypeError):
            AMBIGUOUS_PRODUCTION_STAGES["x"] = "y"
        self.assertFalse(set(PRODUCTION_STAGE_MAP) & set(AMBIGUOUS_PRODUCTION_STAGES))
        for k, why in AMBIGUOUS_PRODUCTION_STAGES.items():
            self.assertIs(map_production_stage(k), Stage.UNKNOWN)
            self.assertTrue(why)

if __name__ == "__main__":
    unittest.main()
