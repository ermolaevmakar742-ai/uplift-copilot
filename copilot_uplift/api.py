"""Uplift admin API (panel only). `build_router(require_admin, session_factory, audit_log)`; main.py includes it. Never imports main.py:
authentication and audit are injected. EVERY route depends on the panel's own `require_admin` (X-Admin-Token session): no
unauthenticated access, no Telegram exposure, no raw SQL, no secrets.

  GET  /api/uplift/status    live read-only evaluation + gates + activation state (writes nothing)
  POST /api/uplift/evaluate  run the evaluation and store ONE append-only snapshot (if the table exists)
  POST /api/uplift/enable    body {"confirm": "ENABLE UPLIFT"}; re-checks every gate at this moment; BLOCKED + 409 if any fails
  POST /api/uplift/disable   ACTIVE -> OFF (BLOCKED on an integrity failure); Production stops using Uplift immediately
The kill switch is read-only here: this API cannot turn it on."""
import logging
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from copilot_uplift.activation import ActivationError
from copilot_uplift.service import UpliftService

logger = logging.getLogger("copilot_uplift")
CHANGED_BY = "admin"
_STATUS = {"confirmation_required": 400, "already_active": 409, "not_active": 409, "activation_storage_unavailable": 503,
           "activation_state_corrupt": 503}


def build_router(require_admin: Callable, session_factory, audit_log: Callable | None = None, clock=None, service: UpliftService | None = None,
                 ) -> APIRouter:
    router = APIRouter(prefix="/api/uplift", tags=["uplift"], dependencies=[Depends(require_admin)])
    svc = service or UpliftService(session_factory, clock=clock)

    async def audit(action: str, request: Request, details: str) -> None:
        if audit_log is not None:
            try:
                await audit_log(action, ip=request.client.host if request.client else None, details=details[:480])
            except Exception:
                logger.warning("uplift audit failed action=%s", action)

    async def guarded(coro):
        try:
            return await coro
        except ActivationError as e:
            raise HTTPException(_STATUS.get(e.code, 400), detail={"ok": False, "error": e.code})
        except HTTPException:
            raise
        except Exception:
            logger.exception("uplift storage/evaluation failure")
            raise HTTPException(503, detail={"ok": False, "error": "uplift_unavailable"})

    async def body_of(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, detail={"ok": False, "error": "invalid_json"})
        if not isinstance(data, dict):
            raise HTTPException(400, detail={"ok": False, "error": "json_object_expected"})
        return data

    @router.get("/status")
    async def status():
        return {"ok": True, **(await guarded(svc.status()))}

    @router.post("/evaluate")
    async def evaluate(request: Request):
        ev, stored = await guarded(svc.evaluate_and_store())
        await audit("uplift_evaluated", request, f"evaluation_id={ev.evaluation_id} quality={ev.data_quality.value} stored={stored}")
        return {"ok": True, "stored": stored, "evaluation_id": ev.evaluation_id, "data_quality": ev.data_quality.value,
                "evaluated_at": ev.evaluation_timestamp.isoformat()}

    @router.post("/enable")
    async def enable(request: Request):
        data = await body_of(request)
        confirm = data.get("confirm")
        if not isinstance(confirm, str):
            raise HTTPException(400, detail={"ok": False, "error": "confirmation_required"})
        res = await guarded(svc.enable(CHANGED_BY, confirm))
        await audit("uplift_enable_" + res["state"].lower(), request, f"missing={res['missing_gates']}")
        if res["state"] != "ACTIVE":
            raise HTTPException(409, detail={"ok": False, "error": "activation_blocked", "missing_gates": res["missing_gates"]})
        return {"ok": True, **res}

    @router.post("/disable")
    async def disable(request: Request):
        res = await guarded(svc.disable(CHANGED_BY, "disabled by admin"))
        await audit("uplift_disabled", request, f"state={res['state']}")
        return {"ok": True, **res}

    return router
