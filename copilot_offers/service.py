"""Offer content + account assignments over the offer_* tables. Parameterised SQLAlchemy core only. Never touches partner_programs
(program existence is checked through ProgramService, read-only)."""
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from copilot_programs.service import ProgramNotFound, ProgramService

from . import validate
from .store import offer_account_assignments as A, offer_details as D, offer_images as I

__all__ = ["OfferService", "ProgramNotFound"]


class AccountRefInvalid(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc)


class OfferService:
    def __init__(self, session_factory, clock=_now):
        self._sf = session_factory
        self._clock = clock
        self._programs = ProgramService(session_factory)

    # ---- content (description + images)
    async def get_content(self, program_id: str) -> dict:
        async with self._sf() as s:
            d = (await s.execute(select(D.c.description).where(D.c.program_id == program_id))).first()
            rows = (await s.execute(select(I.c.image_id, I.c.url, I.c.sort_order, I.c.active)
                                    .where(I.c.program_id == program_id).order_by(I.c.sort_order, I.c.image_id))).all()
        return {"description": d[0] if d else None,
                "images": [{"image_id": r[0], "url": r[1], "sort_order": r[2], "active": bool(r[3])} for r in rows]}

    async def set_content(self, program_id: str, description, images) -> dict:
        """Replace description + the whole image list (order = list order). References only: nothing is downloaded."""
        if await self._programs.get_program(program_id) is None:
            raise ProgramNotFound(program_id)
        desc, imgs = validate.clean_content(description, images)
        now = self._clock()
        async with self._sf() as s:
            r = await s.execute(update(D).where(D.c.program_id == program_id).values(description=desc, updated_at=now))
            if not r.rowcount:
                await s.execute(D.insert().values(program_id=program_id, description=desc, updated_at=now))
            await s.execute(delete(I).where(I.c.program_id == program_id))
            for n, im in enumerate(imgs):
                await s.execute(I.insert().values(image_id=validate.image_id_for(program_id, im["url"]), program_id=program_id,
                                                  url=im["url"], sort_order=n, active=im["active"], created_at=now))
            await s.commit()
        return await self.get_content(program_id)

    # ---- account -> offer (max one per account: account_ref is the primary key)
    async def get_assignment(self, account_ref: str):
        async with self._sf() as s:
            r = (await s.execute(select(A.c.program_id).where(A.c.account_ref == account_ref))).first()
        return r[0] if r else None

    async def list_assignments(self) -> dict:
        async with self._sf() as s:
            rows = (await s.execute(select(A.c.account_ref, A.c.program_id).order_by(A.c.account_ref))).all()
        return {r[0]: r[1] for r in rows}

    async def assign(self, account_ref: str, program_id: str, *, assigned_by: str = "admin") -> str:
        """Create or REPLACE the account's offer. The program must exist (it may be inactive: that is reported at runtime)."""
        if not isinstance(account_ref, str) or not validate.ACCOUNT_REF.fullmatch(account_ref):
            raise AccountRefInvalid(str(account_ref)[:40])
        if await self._programs.get_program(program_id) is None:
            raise ProgramNotFound(program_id)
        now = self._clock()
        for _ in range(2):
            async with self._sf() as s:
                try:
                    r = await s.execute(update(A).where(A.c.account_ref == account_ref)
                                        .values(program_id=program_id, assigned_by=assigned_by, updated_at=now))
                    if not r.rowcount:
                        await s.execute(A.insert().values(account_ref=account_ref, program_id=program_id,
                                                          assigned_by=assigned_by, created_at=now, updated_at=now))
                    await s.commit()
                    return program_id
                except IntegrityError:          # concurrent first insert: retry as an update
                    await s.rollback()
        raise RuntimeError("assignment conflict")

    async def unassign(self, account_ref: str) -> bool:
        async with self._sf() as s:
            r = await s.execute(delete(A).where(A.c.account_ref == account_ref))
            await s.commit()
        return bool(r.rowcount)
