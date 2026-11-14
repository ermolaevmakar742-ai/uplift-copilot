"""Randomized holdout assignment (contract uplift_assignment.v1).

A deterministic per-chat coin decides the arm BEFORE any proactive action:
  CONTROL   (p = CONTROL_SHARE = 0.10): the follow-up worker skips the proactive follow-up (real silence, the DO_NOTHING arm)
  TREATMENT (p = 1 - CONTROL_SHARE):   follow-ups behave exactly as before
Reactive replies (user-initiated) are NEVER randomized: the experiment touches proactive contact only.

The coin is sha256-based: the same chat_id always yields the same arm, the assignment is replayable without storage, and it
cannot be re-rolled. The append-only `uplift_assignments` row is the audit trail and the estimator join, NOT the source of
the coin. A row is written once per chat (unique chat_id; duplicates are ignored, never overwritten).

Kill switch COPILOT_UPLIFT_ASSIGN_ENABLED (default false): off -> assigned_arm() returns None -> no assignment, no behavior
change. Like COPILOT_UPLIFT_ENABLED it can only ever ADD the experiment when an operator sets it; every consumer must treat
None as "no randomization information" (estimator: control stays unavailable).
"""
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Callable, Mapping

logger = logging.getLogger("copilot_uplift")

CONTRACT_VERSION = "uplift_assignment.v1"
KILL_SWITCH_ENV = "COPILOT_UPLIFT_ASSIGN_ENABLED"
SALT_ENV = "COPILOT_UPLIFT_ASSIGN_SALT"
SALT = "uplift-assign.v1"  # deterministic fallback for pure unit tests only; production never uses it
CONTROL_SHARE = 0.10
CONTROL, TREATMENT = "CONTROL", "TREATMENT"
_ARMS = (CONTROL, TREATMENT)
_ON = {"1", "true", "yes", "on"}


def arm_for(chat_id, *, salt: str = SALT, control_share: float = CONTROL_SHARE) -> str:
    """Deterministic arm for a chat. Pure; no DB, no env, no clock."""
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        raise ValueError("chat_id must be an int")
    h = int.from_bytes(hashlib.sha256(f"{salt}|{chat_id}".encode()).digest()[:8], "big")
    return CONTROL if (h / float(2 ** 64)) < control_share else TREATMENT


def assignment_id(chat_id, arm: str, *, contract_version: str = CONTRACT_VERSION, salt: str = SALT) -> str:
    if arm not in _ARMS:
        raise ValueError(f"arm must be one of {_ARMS}")
    return "ua_" + hashlib.sha256(f"{contract_version}|{salt}|{chat_id}|{arm}".encode()).hexdigest()[:20]


def assigned_arm(chat_id, env: Mapping | None = None) -> str | None:
    """Return an arm only when the kill switch and a stable high-entropy deployment salt are present."""
    source = os.environ if env is None else env
    v = source.get(KILL_SWITCH_ENV, "false")
    salt = source.get(SALT_ENV)
    if not (isinstance(v, str) and v.strip().lower() in _ON):
        return None
    if not isinstance(salt, str) or len(salt) < 32:
        logger.error("%s must contain at least 32 characters; refusing deterministic public-salt assignment", SALT_ENV)
        return None
    return arm_for(chat_id, salt=salt)


def probability(arm: str) -> float:
    return CONTROL_SHARE if arm == CONTROL else round(1.0 - CONTROL_SHARE, 6)


def to_payload(chat_id, arm: str, *, context: str = "followup_holdout_gate", salt: str = SALT) -> dict:
    return {"contract_version": CONTRACT_VERSION, "assignment_id": assignment_id(chat_id, arm, salt=salt), "chat_id": chat_id,
            "arm": arm, "assignment_probability": probability(arm), "context": context}


async def record_assignment(session_factory: Callable, chat_id, arm: str, *, clock: Callable[[], datetime] | None = None,
                            context: str = "followup_holdout_gate", salt: str | None = None) -> bool:
    """Append-only persist. Idempotent: the same chat/arm -> False (row exists), never an error to the caller. Never raises."""
    from copilot_uplift.repository import AssignmentRepository
    try:
        salt = salt if salt is not None else os.environ.get(SALT_ENV)
        if not isinstance(salt, str) or len(salt) < 32:
            logger.error("%s must contain at least 32 characters; refusing to persist assignment", SALT_ENV)
            return False
        now = (clock or datetime.now)().astimezone(timezone.utc) if clock else datetime.now(timezone.utc)
        row = to_payload(chat_id, arm, context=context, salt=salt)
        return await AssignmentRepository(session_factory).save(
            assignment_id=row["assignment_id"], chat_id=row["chat_id"], arm=row["arm"],
            assignment_probability=row["assignment_probability"], decided_at=now,
            context=row["context"], contract_version=row["contract_version"])
    except Exception:
        logger.exception("uplift assignment record failed chat=%s", chat_id)
        return False
