"""COPILOT canonical analytical contracts (Phase 2A). Pure, read-only, dependency-light.
Not wired into production. No I/O, no network, no DB, no LLM."""
from copilot_contracts.stages import (
    STAGE_CONTRACT_VERSION, Stage, is_valid_stage, map_production_stage, normalize_stage,
)

__all__ = ["STAGE_CONTRACT_VERSION", "Stage", "is_valid_stage", "map_production_stage", "normalize_stage"]
