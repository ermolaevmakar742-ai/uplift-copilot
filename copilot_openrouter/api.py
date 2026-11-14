"""Admin API for OpenRouter key status. `build_router(require_admin, session_factory, audit_log, env=None)`; main.py includes it. Never imports main.py.
Every route depends on the panel's `require_admin`. Responses carry normalised metrics only (no key, no raw provider response), Cache-Control: no-store.

  GET  /api/settings/llm/openrouter/status           all slots (cached ~45 s)
  GET  /api/settings/llm/openrouter/status/{slot}    one slot (cached)
  POST /api/settings/llm/openrouter/refresh          bypass cache, real request; body optional {"slot": "PRIMARY"}; audited as OPENROUTER_BALANCE_REFRESHED"""
import json
import logging
import os
from typing import Callable, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from copilot_openrouter.service import OpenRouterBalanceService
from copilot_secrets.contracts import SecretError, Slot
from copilot_secrets.crypto import SecretBox
from copilot_secrets.service import SecretService, parse_slot

logger = logging.getLogger("copilot_openrouter")
ACTOR = "admin"
MAX_BODY = 256


def build_router(require_admin: Callable, session_factory, audit_log: Callable | None = None, env: Mapping | None = None,
                 service: OpenRouterBalanceService | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/settings/llm/openrouter", tags=["llm-openrouter"], dependencies=[Depends(require_admin)])
    envmap = env if env is not None else os.environ
    svc = service or OpenRouterBalanceService(SecretService(session_factory, SecretBox(envmap), audit_log=audit_log))

    def slot_of(raw) -> Slot:
        try:
            return parse_slot(str(raw).upper())
        except SecretError:
            raise HTTPException(404, detail={"ok": False, "error": "invalid_slot"})

    @router.get("/status")
    async def status_all(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"ok": True, "slots": [s.to_dict() for s in await svc.all()]}

    @router.get("/status/{slot}")
    async def status_one(slot: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"ok": True, "slot": (await svc.status(slot_of(slot))).to_dict()}

    @router.post("/refresh")
    async def refresh(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise HTTPException(413, detail={"ok": False, "error": "body_too_large"})
        slots = list(Slot)
        if raw.strip():
            try:
                body = json.loads(raw)
            except Exception:
                raise HTTPException(400, detail={"ok": False, "error": "invalid_json"})
            if not isinstance(body, dict) or not set(body) <= {"slot"}:
                raise HTTPException(400, detail={"ok": False, "error": "unexpected_fields"})
            if "slot" in body:
                slots = [slot_of(body["slot"])]
        results = [await svc.status(s, force=True) for s in slots]
        for r in results:
            if audit_log is not None:
                try:
                    await audit_log("openrouter_balance_refreshed", ip=None, details=f"OPENROUTER_BALANCE_REFRESHED actor={ACTOR} slot={r.slot} status={r.status} code={r.error_code or 'ok'}")
                except Exception:
                    logger.warning("panel audit_log failed")
            logger.info("OPENROUTER_BALANCE_REFRESHED actor=%s slot=%s status=%s", ACTOR, r.slot, r.status)
        return {"ok": True, "slots": [r.to_dict() for r in results]}

    return router
