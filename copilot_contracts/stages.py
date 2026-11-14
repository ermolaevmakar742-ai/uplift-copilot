"""Canonical Stage Contract (stage.v1).

An ANALYTICAL vocabulary. It does NOT replace or alter production stages
(main.STAGE_MAP / User.stage / FUNNEL_STAGES); it only labels them for analytics.

Contract for unknown input: never guessed, never START. Both helpers return
Stage.UNKNOWN (an explicit canonical member). UNKNOWN != START.

Inventory (SOURCE -> CURRENT VALUE -> CANONICAL), only unambiguous rows are mapped:
  client_state / User.stage default   new            -> START
  FUNNEL_STAGES / shadow              objection      -> OBJECTION
  shadow decide._OBJECTION_STAGES     hesitation     -> OBJECTION   (shadow treats it as an objection stage)
  STAGE_MAP deposit_attempt           deposit_attempt-> PAYMENT
  STAGE_MAP deposit_success           deposited      -> DEPOSIT
  FUNNEL_STAGES                       deposit        -> DEPOSIT     (same name, funnel step)
  FUNNEL_STAGES / shadow              converted      -> SUCCESS
  FUNNEL_STAGES                       lost           -> DROPPED
Everything else is deliberately UNKNOWN, with the reason in AMBIGUOUS_PRODUCTION_STAGES.
OSINT 2.0 has no stage values (evidence only), so it contributes no mapping.
"""
from enum import Enum
from types import MappingProxyType
from typing import Any

STAGE_CONTRACT_VERSION = "stage.v1"


class Stage(str, Enum):
    START = "START"
    INTEREST = "INTEREST"
    NEED = "NEED"
    TRUST = "TRUST"
    OBJECTION = "OBJECTION"
    PAYMENT = "PAYMENT"
    DEPOSIT = "DEPOSIT"
    SUCCESS = "SUCCESS"
    DROPPED = "DROPPED"
    UNKNOWN = "UNKNOWN"


_BY_VALUE = {s.value: s for s in Stage}

# production stage value (lowercase) -> canonical. Unambiguous only.
PRODUCTION_STAGE_MAP = MappingProxyType({
    "new": Stage.START,
    "objection": Stage.OBJECTION,
    "hesitation": Stage.OBJECTION,
    "deposit_attempt": Stage.PAYMENT,
    "deposited": Stage.DEPOSIT,
    "deposit": Stage.DEPOSIT,
    "converted": Stage.SUCCESS,
    "lost": Stage.DROPPED,
})

# Known production values deliberately NOT mapped (-> UNKNOWN), with the reason.
AMBIGUOUS_PRODUCTION_STAGES = MappingProxyType({
    "visited": "visit to partner site; could be START or INTEREST",
    "registration_started": "no canonical registration stage; INTEREST/NEED/TRUST all plausible",
    "registered": "no canonical registration stage",
    "registration": "no canonical registration stage",
    "card_added": "PAYMENT vs TRUST ambiguous",
    "withdraw_request": "post-deposit lifecycle, outside funnel model",
    "withdrawn": "post-deposit lifecycle, outside funnel model",
    "rejected": "platform rejection; DROPPED vs OBJECTION ambiguous",
    "conversation": "INTEREST vs NEED ambiguous",
    "qualified": "NEED vs TRUST ambiguous",
    "offer": "PAYMENT vs OBJECTION context-dependent",
    "action": "PAYMENT vs OBJECTION context-dependent",
    "click": "INTEREST vs PAYMENT ambiguous",
    "blocked": "user blocked bot vs objection ambiguous",
    "reactivated": "re-entry; no canonical equivalent",
})


def _key(value: Any) -> str | None:
    if isinstance(value, Stage):
        return value.value
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v or None


def is_valid_stage(value: Any) -> bool:
    """True only for an exact canonical value (Stage member or its string, UNKNOWN included)."""
    if isinstance(value, Stage):
        return True
    return isinstance(value, str) and value in _BY_VALUE


def normalize_stage(value: Any) -> Stage:
    """Canonical name (case/whitespace-insensitive) -> Stage. Anything else -> UNKNOWN."""
    k = _key(value)
    return _BY_VALUE.get(k.upper(), Stage.UNKNOWN) if k else Stage.UNKNOWN


def map_production_stage(value: Any) -> Stage:
    """Production stage string -> canonical Stage. Unmapped/ambiguous/empty -> UNKNOWN. Pure."""
    k = _key(value)
    if not k or isinstance(value, Stage):
        return Stage.UNKNOWN
    return PRODUCTION_STAGE_MAP.get(k.lower(), Stage.UNKNOWN)
