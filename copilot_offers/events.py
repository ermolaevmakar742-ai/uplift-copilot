"""Structured events + the append-only write. Events carry identifiers and enums only."""
import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select

from .store import offer_decisions

logger = logging.getLogger("copilot_offers")
_TRUE = {"1", "true", "yes", "on"}


def _flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in _TRUE


def engine_enabled() -> bool:
    return _flag("COPILOT_OFFER_ENGINE_ENABLED")


def shadow_enabled() -> bool:
    """Persist offer_decisions rows (requires the engine flag as well)."""
    return _flag("COPILOT_OFFER_SHADOW_ENABLED")


def context_enabled() -> bool:
    """Put the assigned, validated program into the LLM prompt as quoted DATA. Independent of ENGINE / SHADOW / REAL."""
    return _flag("COPILOT_OFFER_CONTEXT_ENABLED")


def real_enabled() -> bool:
    """Reserved. Nothing in V1 reads it to change behaviour: replies are never modified."""
    return _flag("COPILOT_OFFER_REAL_ENABLED")


def emit(name: str, payload: dict) -> None:
    payload = {"ts": datetime.now(timezone.utc).isoformat(), **payload}
    logger.info("%s %s", name, json.dumps(payload, ensure_ascii=False, default=str))


async def persist(session_factory, record: dict) -> bool:
    """Append-only insert, once per production decision: returns False (writes nothing) if this correlation_id already has a row."""
    async with session_factory() as s:
        dup = (await s.execute(select(offer_decisions.c.id).where(
            offer_decisions.c.correlation_id == record["correlation_id"]).limit(1))).first()
        if dup:
            return False
        await s.execute(offer_decisions.insert().values(**record))
        await s.commit()
        return True
