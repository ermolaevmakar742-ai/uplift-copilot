"""Persistence for partner programs. Parameterised SQLAlchemy Core only (no string-built SQL). No DDL here.
Unlike the append-only COPILOT ledgers, programs are intentionally editable: update() replaces one row by primary key.
Validation lives in the contract/service; this layer only stores and reads validated Program objects."""
from decimal import Decimal

from sqlalchemy import select, update as sa_update
from sqlalchemy.exc import IntegrityError

from copilot_programs.contract import Program
from copilot_programs.store import partner_programs as T


class ProgramExists(Exception):
    pass


class ProgramNotFound(Exception):
    pass


def _row(p: Program) -> dict:
    return {"program_id": p.program_id, "name": p.name, "brand": p.brand, "countries": list(p.countries),
            "currency": p.currency, "registration_url": p.registration_url, "promo_code": p.promo_code,
            "min_deposit": p.min_deposit, "min_withdrawal": p.min_withdrawal, "withdrawal_time": p.withdrawal_time,
            "payment_methods": list(p.payment_methods), "terms": p.terms, "support_contact": p.support_contact,
            "notes": p.notes, "active": p.active, "created_at": p.created_at, "updated_at": p.updated_at,
            "updated_by": p.updated_by}


def _dec(v):
    return None if v is None else Decimal(str(v)).quantize(Decimal("0.01"))


def _program(r) -> Program:
    from datetime import timezone
    aware = lambda d: d if d.tzinfo else d.replace(tzinfo=timezone.utc)      # sqlite drops tz
    return Program(program_id=r["program_id"], name=r["name"], brand=r["brand"], countries=tuple(r["countries"] or ()),
                   currency=r["currency"], registration_url=r["registration_url"], promo_code=r["promo_code"],
                   min_deposit=_dec(r["min_deposit"]), min_withdrawal=_dec(r["min_withdrawal"]),
                   withdrawal_time=r["withdrawal_time"], payment_methods=tuple(r["payment_methods"] or ()),
                   terms=r["terms"], support_contact=r["support_contact"], notes=r["notes"], active=bool(r["active"]),
                   created_at=aware(r["created_at"]), updated_at=aware(r["updated_at"]), updated_by=r["updated_by"])


class ProgramRepository:
    def __init__(self, session_factory):
        self._sf = session_factory

    async def insert(self, p: Program) -> None:
        async with self._sf() as s:
            try:
                await s.execute(T.insert().values(**_row(p)))
                await s.commit()
            except IntegrityError:
                await s.rollback()
                raise ProgramExists(p.program_id) from None

    async def update(self, p: Program) -> None:
        row = _row(p)
        row.pop("program_id")
        row.pop("created_at")                                   # creation time is immutable
        async with self._sf() as s:
            res = await s.execute(sa_update(T).where(T.c.program_id == p.program_id).values(**row))
            if not res.rowcount:
                await s.rollback()
                raise ProgramNotFound(p.program_id)
            await s.commit()

    async def get(self, program_id: str) -> Program | None:
        async with self._sf() as s:
            r = (await s.execute(select(T).where(T.c.program_id == program_id))).mappings().first()
        return None if r is None else _program(r)

    async def list(self, *, active: bool | None = None) -> list[Program]:
        q = select(T).order_by(T.c.program_id)
        if active is not None:
            q = q.where(T.c.active == active)
        async with self._sf() as s:
            rows = (await s.execute(q)).mappings().all()
        return [_program(r) for r in rows]
