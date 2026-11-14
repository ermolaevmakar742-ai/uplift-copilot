"""Account Offer Manager HTTP API (admin panel only). `build_router(require_admin, session_factory, audit_log, account_id_getter)`.
Authentication/audit are injected (this module never imports main.py). Offer core fields (name, brand, URL, promo, active) are
still managed by /api/programs; this API adds description + images, and the account -> offer assignment.

    GET    /api/offers                              offers with description + images + which accounts use them
    GET    /api/offers/accounts                     Telegram accounts (current runtime account + any assigned) and their offer
    PUT    /api/offers/accounts/{ref}/assignment    {"program_id": "..."}  assign or REPLACE
    DELETE /api/offers/accounts/{ref}/assignment    remove
    GET    /api/offers/{program_id}                 one offer
    PUT    /api/offers/{program_id}/content         {"description": str|null, "images": [url | {"url","active"}]}
Nothing here sends anything to Telegram, fetches an image, or calls an LLM/partner."""
import logging
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from copilot_programs.service import ProgramService

from .accounts import account_ref_for
from .service import AccountRefInvalid, OfferService, ProgramNotFound
from .validate import ContentValidationError

logger = logging.getLogger("copilot_offers")
LABEL_CURRENT = "COPILOT Telegram account (current)"


def build_router(require_admin: Callable, session_factory, audit_log: Callable | None = None,
                 account_id_getter: Callable | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/offers", tags=["offers"], dependencies=[Depends(require_admin)])
    svc = OfferService(session_factory)
    programs = ProgramService(session_factory)

    async def audit(action: str, request: Request, details: str) -> None:
        if audit_log is not None:
            try:
                await audit_log(action, ip=request.client.host if request.client else None, details=details[:480])
            except Exception:
                logger.warning("offers audit failed action=%s", action)

    async def guarded(coro):
        try:
            return await coro
        except ContentValidationError as e:
            raise HTTPException(400, detail={"ok": False, "errors": e.errors})
        except AccountRefInvalid:
            raise HTTPException(400, detail={"ok": False, "errors": {"account_ref": "invalid account reference"}})
        except ProgramNotFound:
            raise HTTPException(404, detail={"ok": False, "error": "program_not_found"})
        except HTTPException:
            raise
        except Exception:
            logger.exception("offers storage failure")
            raise HTTPException(503, detail={"ok": False, "error": "offers_unavailable"})

    def current_ref():
        try:
            return account_ref_for(account_id_getter()) if account_id_getter else None
        except Exception:
            return None

    async def offer_dict(p, content, users):
        d = p.to_dict()
        for k in ("terms", "notes"):          # admin-only CRM data; not part of this view
            d.pop(k, None)
        d.update(description=content["description"], images=content["images"], used_by=users)
        return d

    async def body_of(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, detail={"ok": False, "errors": {"_body": "invalid JSON"}})
        if not isinstance(data, dict):
            raise HTTPException(400, detail={"ok": False, "errors": {"_body": "JSON object expected"}})
        return data

    @router.get("")
    async def list_offers():
        async def work():
            assigned = await svc.list_assignments()
            out = []
            for p in await programs.list_programs():
                users = sorted(a for a, pid in assigned.items() if pid == p.program_id)
                out.append(await offer_dict(p, await svc.get_content(p.program_id), users))
            return out
        return {"ok": True, "offers": await guarded(work())}

    @router.get("/accounts")
    async def list_accounts():
        assigned = await guarded(svc.list_assignments())
        cur = current_ref()
        refs = ([cur] if cur else []) + [r for r in assigned if r != cur]
        return {"ok": True, "accounts": [{"account_ref": r, "label": LABEL_CURRENT if r == cur else None,
                                          "is_current": r == cur, "program_id": assigned.get(r)} for r in refs]}

    async def require_known_account(ref: str):
        if ref != current_ref() and ref not in await svc.list_assignments():
            raise HTTPException(404, detail={"ok": False, "error": "account_unknown"})

    @router.put("/accounts/{account_ref}/assignment")
    async def assign(account_ref: str, request: Request):
        data = await body_of(request)
        pid = data.get("program_id")
        if not isinstance(pid, str) or not pid:
            raise HTTPException(400, detail={"ok": False, "errors": {"program_id": "required"}})
        await guarded(require_known_account(account_ref))
        await guarded(svc.assign(account_ref, pid))
        await audit("account_offer_assigned", request, f"account={account_ref} program_id={pid}")
        return {"ok": True, "account_ref": account_ref, "program_id": pid}

    @router.delete("/accounts/{account_ref}/assignment")
    async def unassign(account_ref: str, request: Request):
        removed = await guarded(svc.unassign(account_ref))
        if removed:
            await audit("account_offer_removed", request, f"account={account_ref}")
        return {"ok": True, "removed": removed}

    @router.get("/{program_id}")
    async def get_offer(program_id: str):
        async def work():
            p = await programs.get_program(program_id)
            if p is None:
                raise ProgramNotFound(program_id)
            users = sorted(a for a, pid in (await svc.list_assignments()).items() if pid == program_id)
            return await offer_dict(p, await svc.get_content(program_id), users)
        return {"ok": True, "offer": await guarded(work())}

    @router.put("/{program_id}/content")
    async def put_content(program_id: str, request: Request):
        data = await body_of(request)
        extra = set(data) - {"description", "images"}
        if extra:
            raise HTTPException(400, detail={"ok": False, "errors": {"_unknown": f"unknown fields: {sorted(extra)}"}})
        content = await guarded(svc.set_content(program_id, data.get("description"), data.get("images")))
        await audit("offer_content_updated", request, f"program_id={program_id} images={len(content['images'])}")
        return {"ok": True, "content": content}

    return router
