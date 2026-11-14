"""Lead Journey contract. Stage vocabulary is ONLY copilot_contracts.stages (no second dictionary)."""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from copilot_contracts.stages import Stage, is_valid_stage, map_production_stage

CONTRACT_VERSION = "lead-journey.v1"
SOURCE_HUMAN = "human"
# the timeline axis, in order; DROPPED and UNKNOWN are separate states, not axis positions
AXIS = (Stage.START, Stage.INTEREST, Stage.NEED, Stage.TRUST, Stage.OBJECTION, Stage.PAYMENT, Stage.DEPOSIT, Stage.SUCCESS)
SEPARATE = (Stage.DROPPED, Stage.UNKNOWN)
# a human may place a lead on any real stage or mark it DROPPED; "UNKNOWN" is not an observation, so it is never a target
TARGETS = AXIS + (Stage.DROPPED,)
MAX_ACTOR = 64


class LeadError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def parse_stage(value: Any, *, allow_unknown: bool) -> Stage:
    """Exact canonical value only (no case folding, no guessing)."""
    if not isinstance(value, str) or not is_valid_stage(value):
        raise LeadError("invalid_stage")
    s = Stage(value)
    if s is Stage.UNKNOWN and not allow_unknown:
        raise LeadError("invalid_stage")
    return s


def parse_chat_id(value: Any) -> int:
    if isinstance(value, bool):
        raise LeadError("invalid_chat_id")
    if isinstance(value, str) and value.strip().lstrip("-").isdigit() and len(value.strip()) <= 19:
        value = int(value.strip())
    if not isinstance(value, int) or value == 0 or abs(value) >= 2 ** 63:
        raise LeadError("invalid_chat_id")
    return value


def production_stage(raw: Any) -> Stage:
    return map_production_stage(raw)


def mask_chat_id(chat_id: int) -> str:
    s = str(abs(chat_id))
    return "…" + s[-4:] if len(s) > 4 else "…" + s


@dataclass(frozen=True)
class StageOverride:
    override_id: str
    chat_id: int
    previous_stage: str
    new_stage: str
    actor: str
    source: str
    created_at: datetime
    previous_override_id: str | None = None
    contract_version: str = CONTRACT_VERSION

    def to_dict(self) -> dict:
        return {"override_id": self.override_id, "chat_id": self.chat_id, "previous_stage": self.previous_stage, "new_stage": self.new_stage,
                "actor": self.actor, "source": self.source, "created_at": self.created_at.isoformat(),
                "previous_override_id": self.previous_override_id, "contract_version": self.contract_version}


def compute_override_id(chat_id: int, previous_stage: str, new_stage: str, actor: str, created_at: datetime, previous_override_id: str | None) -> str:
    """Content hash that includes the timestamp and the previous override of the lead: a repeated identical move is a NEW event."""
    raw = json.dumps([chat_id, previous_stage, new_stage, actor, created_at.astimezone(timezone.utc).isoformat(), previous_override_id],
                     separators=(",", ":"))
    return "so_" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def build_override(chat_id: int, previous: Stage, new: Stage, actor: str, created_at: datetime, previous_override_id: str | None) -> StageOverride:
    if not isinstance(actor, str) or not actor.strip() or len(actor) > MAX_ACTOR:
        raise LeadError("invalid_actor")
    if created_at.tzinfo is None:
        raise LeadError("naive_timestamp")
    return StageOverride(compute_override_id(chat_id, previous.value, new.value, actor, created_at, previous_override_id), chat_id,
                         previous.value, new.value, actor, SOURCE_HUMAN, created_at, previous_override_id)
