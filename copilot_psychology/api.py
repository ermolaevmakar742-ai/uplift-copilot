"""Psychology read-only admin API (panel only): GET /api/psychology/{chat_id}. No write routes, no controls. Admin guard on every route.
Returns the latest stored shadow assessment for the lead plus the mode the engine would be active in. Never imports main.py."""
import logging
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import BigInteger, Column, DateTime, MetaData, String, Table, select
from sqlalchemy.exc import DBAPIError

from copilot_psychology.contracts import ENGINE_VERSION, KNOWLEDGE_VERSION, STRATEGY_VERSION, State
from copilot_psychology.engine import is_active
from copilot_psychology.repository import PsychologyRepository

logger = logging.getLogger("copilot_psychology")
_ro = MetaData()
_dec = Table("production_decisions", _ro, Column("id", BigInteger, primary_key=True), Column("chat_id", BigInteger), Column("created_at", DateTime(timezone=True)),
             Column("regime", String), Column("decision_path", String))
INSUFFICIENT = {State.INSUFFICIENT_DATA, State.UNKNOWN}


def build_router(require_admin: Callable, session_factory, *_ignored) -> APIRouter:
    router = APIRouter(prefix="/api/psychology", tags=["psychology"], dependencies=[Depends(require_admin)])
    repo = PsychologyRepository(session_factory)

    @router.get("/{chat_id}")
    async def latest(chat_id: int):
        if chat_id == 0 or abs(chat_id) >= 2 ** 63:
            raise HTTPException(400, detail={"ok": False, "error": "invalid_chat_id"})
        base = {"ok": True, "knowledge_version": KNOWLEDGE_VERSION, "engine_version": ENGINE_VERSION, "strategy_version": STRATEGY_VERSION,
                "phase": "shadow_only", "mode": "not active", "status": "none", "storage": "ready", "assessment": None}
        try:
            async with session_factory() as s:
                r = (await s.execute(select(_dec.c.regime).where(_dec.c.chat_id == chat_id, _dec.c.decision_path == "llm_reply")
                                     .order_by(_dec.c.created_at.desc()).limit(1))).scalar()
            base["mode"] = "HARD" if is_active(r) else "not active"
        except Exception:
            logger.exception("psychology mode lookup failed")
        try:
            row = await repo.latest_for_chat(chat_id)
        except DBAPIError:
            base["storage"] = "not_migrated"
            return base
        except Exception:
            logger.exception("psychology read failed")
            raise HTTPException(503, detail={"ok": False, "error": "psychology_unavailable"})
        if row:
            a = row["assessment"]
            base.update(status="insufficient" if a.psychological_state in INSUFFICIENT else "available", assessment=a.to_dict(),
                        decision_id=row["decision_id"], created_at=row["created_at"].isoformat())
        return base

    return router
