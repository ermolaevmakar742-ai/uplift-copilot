"""Human stage override rules (pure): effective stage = latest human override if any, else the canonical mapping of users.stage.
The override is an observation about a lead; it never writes users.stage and nothing in production reads it."""
from datetime import datetime

from copilot_contracts.stages import Stage
from copilot_leads.contract import LeadError, TARGETS, build_override, parse_stage, production_stage


def effective_stage(user_stage_raw, last_override: dict | None) -> tuple[Stage, str]:
    """-> (stage, origin) with origin in {'human', 'production'}."""
    if last_override:
        return Stage(last_override["new_stage"]), "human"
    return production_stage(user_stage_raw), "production"


def plan_move(user: dict, last_override: dict | None, from_stage, to_stage, actor: str, now: datetime):
    new = parse_stage(to_stage, allow_unknown=False)
    claimed = parse_stage(from_stage, allow_unknown=True)
    if new not in TARGETS:
        raise LeadError("invalid_stage")
    current, _ = effective_stage(user.get("stage"), last_override)
    if claimed is not current:
        raise LeadError("stale_stage")            # the board the admin saw is out of date: nothing is written
    if new is current:
        raise LeadError("same_stage")
    return build_override(user["id"], current, new, actor, now, last_override["override_id"] if last_override else None)
