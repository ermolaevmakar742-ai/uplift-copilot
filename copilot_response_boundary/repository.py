"""Persistence for response boundaries. INSERT/SELECT only: save / get_by_id / get_by_decision_id / exists.
No update, upsert, replace or delete. The contract is the single serializer (ResponseBoundary.from_dict).

save(): first save inserts; the same id with IDENTICAL content is a no-op (False); the same id with DIFFERENT content
raises BoundaryConflict. Reads rebuild strictly; a corrupt / incompatible row raises BoundaryCorrupt (never repaired,
never returned). get_by_decision_id returns ALL parts of a reply, ordered by (sent_at, id), as a tuple (empty if none).
No DDL (migrate.py), no clock, no joins, no attribution, no decision lookup."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from copilot_evidence.contracts import ContractError
from copilot_response_boundary.contract import ResponseBoundary
from copilot_response_boundary.store import response_boundaries as T


class BoundaryStoreError(Exception):
    pass


class BoundaryConflict(BoundaryStoreError):
    pass


class BoundaryCorrupt(BoundaryStoreError):
    pass


def _iso_utc(dt):
    if not isinstance(dt, datetime):
        return dt
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()   # sqlite drops tz


def _restore(row) -> ResponseBoundary:
    try:
        return ResponseBoundary.from_dict({
            "contract_version": row["contract_version"], "response_boundary_id": row["response_boundary_id"],
            "decision_id": row["decision_id"], "chat_id": row["chat_id"],
            "telegram_message_id": row["telegram_message_id"], "sent_at": _iso_utc(row["sent_at"]),
            "status": row["status"], "metadata": row["metadata"]})
    except (ContractError, TypeError, ValueError, KeyError) as e:
        raise BoundaryCorrupt(f"boundary {row['response_boundary_id']!r} unreadable: {e}") from None


class ResponseBoundaryRepository:
    def __init__(self, session_factory):
        self._sf = session_factory

    @staticmethod
    async def _rows(session, where, order=False):
        q = select(T).where(where)
        if order:
            q = q.order_by(T.c.sent_at, T.c.response_boundary_id)
        try:
            return (await session.execute(q)).mappings().all()
        except ValueError as e:                         # e.g. undecodable JSON in a stored column
            raise BoundaryCorrupt(f"unreadable row: {e}") from None

    async def save(self, b: ResponseBoundary) -> bool:
        if not isinstance(b, ResponseBoundary):
            raise TypeError("b must be a ResponseBoundary")
        d = b.to_dict()
        row = {"response_boundary_id": d["response_boundary_id"], "decision_id": d["decision_id"], "chat_id": d["chat_id"],
               "telegram_message_id": d["telegram_message_id"], "sent_at": b.sent_at.astimezone(timezone.utc),
               "status": d["status"], "contract_version": d["contract_version"], "metadata": d["metadata"]}
        async with self._sf() as s:
            dialect = s.bind.dialect.name if s.bind is not None else "postgresql"
            ins = (sqlite_insert if dialect == "sqlite" else pg_insert)(T).values(**row)
            res = await s.execute(ins.on_conflict_do_nothing(index_elements=["response_boundary_id"]))
            if res.rowcount:
                await s.commit()
                return True
            rows = await self._rows(s, T.c.response_boundary_id == b.response_boundary_id)
        if not rows:
            raise BoundaryStoreError("insert ignored but no row for this id")
        if _restore(rows[0]) != b:
            raise BoundaryConflict(f"a different record is already stored under {b.response_boundary_id!r}")
        return False

    async def get_by_id(self, response_boundary_id: str) -> ResponseBoundary | None:
        async with self._sf() as s:
            rows = await self._rows(s, T.c.response_boundary_id == response_boundary_id)
        return _restore(rows[0]) if rows else None

    async def get_by_decision_id(self, decision_id: str) -> tuple[ResponseBoundary, ...]:
        async with self._sf() as s:
            rows = await self._rows(s, T.c.decision_id == decision_id, order=True)
        return tuple(_restore(r) for r in rows)

    async def exists(self, response_boundary_id: str) -> bool:
        async with self._sf() as s:
            return (await s.execute(select(T.c.response_boundary_id).where(
                T.c.response_boundary_id == response_boundary_id).limit(1))).first() is not None
