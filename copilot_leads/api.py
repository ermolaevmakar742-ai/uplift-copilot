"""Lead Journey admin API (panel only). `build_router(require_admin, session_factory, audit_log)`; main.py includes it. Never imports main.py.
EVERY route depends on the panel's `require_admin` (X-Admin-Token session). Minimal surface:
  GET  /api/leads/journey?limit=&offset=   bounded page of real leads on the canonical axis
  GET  /api/leads/{chat_id}                one lead: signals, recent decision / boundary / outcome, state series, override history
  POST /api/leads/{chat_id}/stage          body {"from_stage","to_stage"}: appends ONE human override event (never edits users.stage)
Errors never reach Telegram: this router is not used by the bot's reply path."""
import logging
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from copilot_leads.contract import LeadError
from copilot_leads.service import LeadService

logger = logging.getLogger("copilot_leads")
ACTOR = "admin"
_STATUS = {"invalid_chat_id": 400, "invalid_stage": 400, "invalid_actor": 400, "same_stage": 400, "lead_not_found": 404, "stale_stage": 409,
           "override_storage_unavailable": 503}


def build_router(require_admin: Callable, session_factory, audit_log: Callable | None = None, clock=None, service: LeadService | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/leads", tags=["leads"], dependencies=[Depends(require_admin)])
    svc = service or LeadService(session_factory, clock=clock)

    async def guarded(coro):
        try:
            return await coro
        except LeadError as e:
            raise HTTPException(_STATUS.get(e.code, 400), detail={"ok": False, "error": e.code})
        except HTTPException:
            raise
        except Exception:
            logger.exception("lead journey failure")
            raise HTTPException(503, detail={"ok": False, "error": "leads_unavailable"})

    @router.get("/journey")
    async def journey(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0, le=10_000_000)):
        return {"ok": True, **(await guarded(svc.journey(limit, offset)))}

    @router.get("/{chat_id}")
    async def detail(chat_id: str):
        return {"ok": True, **(await guarded(svc.detail(chat_id)))}

    @router.post("/{chat_id}/stage")
    async def move(chat_id: str, request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, detail={"ok": False, "error": "invalid_json"})
        if not isinstance(body, dict) or set(body) != {"from_stage", "to_stage"}:
            raise HTTPException(400, detail={"ok": False, "error": "unexpected_fields"})
        res = await guarded(svc.move(chat_id, body["from_stage"], body["to_stage"], ACTOR))
        if audit_log is not None:
            try:
                await audit_log("lead_stage_override", ip=request.client.host if request.client else None,
                                details=f"{res['override_id']} {res['previous_stage']}->{res['new_stage']}"[:480])
            except Exception:
                logger.warning("lead override audit_log failed")
        return {"ok": True, "override": res}

    return router
