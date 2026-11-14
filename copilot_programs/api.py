"""Partner Programs HTTP API (admin panel only). `build_router(require_admin, session_factory, audit_log)` returns an
APIRouter; main.py includes it. This module never imports main.py: authentication and audit are injected.

Every route depends on the panel's own `require_admin` (X-Admin-Token session). No Telegram exposure, no secrets
(promo code / URLs are public data), no raw SQL (service -> parameterised repository). Every write is stamped
updated_at/updated_by (the panel has a single admin identity -> "admin") and recorded in the existing audit log.
Errors: 400 validation (field -> message), 404 unknown id, 409 duplicate id, 503 storage unavailable (e.g. table not migrated)."""
import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from copilot_programs.service import ProgramExists, ProgramNotFound, ProgramService, ProgramValidationError

logger = logging.getLogger("copilot_programs")
UPDATED_BY = "admin"
WRITABLE_META = {"program_id", "active"}


def _flag(v: str | None) -> bool | None:
    if v is None or v == "":
        return None
    if v.lower() in ("1", "true", "yes"):
        return True
    if v.lower() in ("0", "false", "no"):
        return False
    raise HTTPException(400, detail={"ok": False, "errors": {"active": "must be true or false"}})


def build_router(require_admin: Callable, session_factory, audit_log: Callable | None = None, clock=None) -> APIRouter:
    router = APIRouter(prefix="/api/programs", tags=["programs"], dependencies=[Depends(require_admin)])
    svc = ProgramService(session_factory) if clock is None else ProgramService(session_factory, clock)

    async def audit(action: str, request: Request, details: str) -> None:
        if audit_log is not None:
            try:
                await audit_log(action, ip=request.client.host if request.client else None, details=details)
            except Exception:
                logger.warning("programs audit failed action=%s", action)

    async def guarded(coro):
        try:
            return await coro
        except ProgramValidationError as e:
            raise HTTPException(400, detail={"ok": False, "errors": e.errors})
        except ProgramNotFound:
            raise HTTPException(404, detail={"ok": False, "error": "program_not_found"})
        except ProgramExists:
            raise HTTPException(409, detail={"ok": False, "error": "program_id_exists"})
        except HTTPException:
            raise
        except Exception:
            logger.exception("partner programs storage failure")
            raise HTTPException(503, detail={"ok": False, "error": "programs_unavailable"})

    async def body_of(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, detail={"ok": False, "errors": {"_body": "invalid JSON"}})
        if not isinstance(data, dict):
            raise HTTPException(400, detail={"ok": False, "errors": {"_body": "JSON object expected"}})
        return data

    @router.get("")
    async def list_programs(active: str | None = None, country: str | None = None, brand: str | None = None):
        items = await guarded(svc.list_programs(active=_flag(active), country=country, brand=brand))
        return {"ok": True, "programs": [p.to_dict() for p in items]}

    @router.get("/context")
    async def program_for_context(country: str | None = None, brand: str | None = None, currency: str | None = None):
        """Read-only lookup for future bot use. Not wired to replies. null unless exactly one active program matches."""
        found = await guarded(svc.find_active_programs_for_context(country=country, brand=brand, currency=currency))
        return {"ok": True, "program": found[0].to_dict() if len(found) == 1 else None, "matches": len(found)}

    @router.get("/{program_id}")
    async def get_program(program_id: str):
        p = await guarded(svc.get_program(program_id))
        if p is None:
            raise HTTPException(404, detail={"ok": False, "error": "program_not_found"})
        return {"ok": True, "program": p.to_dict()}

    @router.post("")
    async def create_program(request: Request):
        data = await body_of(request)
        pid = data.pop("program_id", None)
        active = data.pop("active", False)
        p = await guarded(svc.create_program(pid, data, updated_by=UPDATED_BY, active=active))
        await audit("partner_program_created", request, f"program_id={p.program_id} active={p.active}")
        return {"ok": True, "program": p.to_dict()}

    @router.patch("/{program_id}")
    async def update_program(program_id: str, request: Request):
        data = await body_of(request)
        if WRITABLE_META & set(data):
            raise HTTPException(400, detail={"ok": False, "errors": {
                "_body": "program_id is immutable; use /activate or /deactivate to change status"}})
        p = await guarded(svc.update_program(program_id, data, updated_by=UPDATED_BY))
        await audit("partner_program_updated", request, f"program_id={p.program_id} fields={sorted(data)}"[:480])
        return {"ok": True, "program": p.to_dict()}

    @router.post("/{program_id}/activate")
    async def activate(program_id: str, request: Request):
        p = await guarded(svc.activate_program(program_id, updated_by=UPDATED_BY))
        await audit("partner_program_activated", request, f"program_id={p.program_id}")
        return {"ok": True, "program": p.to_dict()}

    @router.post("/{program_id}/deactivate")
    async def deactivate(program_id: str, request: Request):
        p = await guarded(svc.deactivate_program(program_id, updated_by=UPDATED_BY))
        await audit("partner_program_deactivated", request, f"program_id={p.program_id}")
        return {"ok": True, "program": p.to_dict()}

    return router
