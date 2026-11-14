"""Admin API for LLM key management. `build_router(require_admin, session_factory, audit_log, env=None)`; main.py includes it. Never imports main.py.
EVERY route depends on the panel's `require_admin` (anonymous = 401). Responses carry metadata only (masked key, status, version, timestamps), never a key,
and are sent with Cache-Control: no-store. The request body is parsed manually so that a submitted key is never echoed in a validation error.

  GET    /api/settings/llm/keys                 slots + master-key state + read-only router flags
  POST   /api/settings/llm/keys/{slot}          body {"api_key","provider"?}: create or rotate
  DELETE /api/settings/llm/keys/{slot}          delete + crypto-erase
  POST   /api/settings/llm/keys/{slot}/test     dry-run connection test (no network)
  POST   /api/settings/llm/keys/{slot}/disable | /activate"""
import logging
import os
from typing import Callable, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from copilot_secrets.contracts import SecretError, Slot
from copilot_secrets.crypto import SecretBox
from copilot_secrets.service import SecretService, parse_slot

logger = logging.getLogger("copilot_secrets")
ACTOR = "admin"
MAX_BODY = 2048
_STATUS = {"invalid_slot": 404, "invalid_key_format": 400, "invalid_provider": 400, "master_key_unavailable": 503, "storage_not_migrated": 503,
           "not_configured": 404, "not_disabled": 409, "already_disabled_or_missing": 409, "concurrent_update": 409, "unexpected_fields": 400,
           "invalid_json": 400, "body_too_large": 413}


def _router_view(env: Mapping) -> dict:
    """Read-only view of the (inactive) LLM router configuration: provider / model per slot and the three flags. No key, no key variable value."""
    try:
        from copilot_llm_router.config import load_config
        c = load_config(env)
        return {"enabled": c.router_enabled, "failover_enabled": c.failover_enabled, "dry_run": c.dry_run,
                "slots": {s.slot.value: {"provider": s.provider, "model": s.model} for s in c.slots}}
    except Exception:
        return {"enabled": False, "failover_enabled": False, "dry_run": True, "slots": {}}


def build_router(require_admin: Callable, session_factory, audit_log: Callable | None = None, env: Mapping | None = None, service: SecretService | None = None,
                 ) -> APIRouter:
    router = APIRouter(prefix="/api/settings/llm/keys", tags=["llm-keys"], dependencies=[Depends(require_admin)])
    envmap = env if env is not None else os.environ
    svc = service or SecretService(session_factory, SecretBox(envmap), audit_log=audit_log)

    def fail(code: str, status: int | None = None):
        raise HTTPException(status or _STATUS.get(code, 400), detail={"ok": False, "error": code})

    async def guarded(coro):
        try:
            return await coro
        except SecretError as e:
            fail(e.code)
        except HTTPException:
            raise
        except Exception:
            logger.error("llm key operation failed")          # no exc_info: a traceback must never be able to carry request data
            fail("secret_service_unavailable", 503)

    def slot_of(raw: str) -> Slot:
        try:
            return parse_slot(raw.upper())
        except SecretError as e:
            fail(e.code)

    @router.get("")
    async def overview(response: Response):
        response.headers["Cache-Control"] = "no-store"
        storage, metas = "ready", {}
        try:
            metas = await svc.list_metadata()
        except SecretError:
            storage = "not_migrated"
        rv = _router_view(envmap)
        slots = []
        for sl in Slot:
            m = metas.get(sl.value)
            cfgs = rv["slots"].get(sl.value, {})
            slots.append({"slot": sl.value, "provider": (m.provider if m else None) or cfgs.get("provider"), "model": cfgs.get("model"),
                          "configured": bool(m), "status": m.status if m else "NOT_CONFIGURED", "masked": m.masked if m else None,
                          "version": m.version if m else None, "last_tested_at": m.last_tested_at.isoformat() if m and m.last_tested_at else None,
                          "last_test_result": m.last_test_result if m else None})
        return {"ok": True, "master_key": svc.box.state, "writes_enabled": svc.writes_enabled, "storage": storage, "router": {k: rv[k] for k in ("enabled", "failover_enabled", "dry_run")},
                "slots": slots}

    @router.post("/{slot}")
    async def set_key(slot: str, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        sl = slot_of(slot)
        raw = await request.body()
        if len(raw) > MAX_BODY:
            fail("body_too_large")
        try:
            import json
            body = json.loads(raw)
        except Exception:
            fail("invalid_json")
        if not isinstance(body, dict) or not set(body) <= {"api_key", "provider"} or "api_key" not in body:
            fail("unexpected_fields")
        meta = await guarded(svc.set_secret(sl, body.get("api_key"), body.get("provider") or (_router_view(envmap)["slots"].get(sl.value, {}).get("provider")) or "openrouter", ACTOR))
        return {"ok": True, "key": meta.to_dict()}

    @router.delete("/{slot}")
    async def delete_key(slot: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        await guarded(svc.delete_secret(slot_of(slot), ACTOR))
        return {"ok": True}

    @router.post("/{slot}/test")
    async def test_key(slot: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"ok": True, **(await guarded(svc.test_secret(slot_of(slot), ACTOR)))}

    @router.post("/{slot}/disable")
    async def disable_key(slot: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"ok": True, "key": (await guarded(svc.disable_secret(slot_of(slot), ACTOR))).to_dict()}

    @router.post("/{slot}/activate")
    async def activate_key(slot: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"ok": True, "key": (await guarded(svc.activate_secret(slot_of(slot), ACTOR))).to_dict()}

    return router
