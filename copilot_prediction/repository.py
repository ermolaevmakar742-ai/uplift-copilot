"""Persistence for prediction comparisons: INSERT/SELECT only (save / get / list_by_decision / exists). Strict reads through
PredictionComparison.from_dict; the indexed columns must agree with the payload (else PredictionCorrupt). Same id + identical
content -> False; same id + different content -> PredictionConflict. No DDL, no clock, no joins."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from copilot_evidence.contracts import ContractError
from copilot_prediction.contract import PredictionComparison
from copilot_prediction.store import prediction_comparisons as T


class PredictionStoreError(Exception):
    pass


class PredictionConflict(PredictionStoreError):
    pass


class PredictionCorrupt(PredictionStoreError):
    pass


def _utc(dt):
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc) if isinstance(dt, datetime) else dt


def _restore(row) -> PredictionComparison:
    try:
        c = PredictionComparison.from_dict(row["payload"])
    except (ContractError, TypeError, ValueError, KeyError) as e:
        raise PredictionCorrupt(f"comparison {row['comparison_id']!r} unreadable: {e}") from None
    for name, a, b in (("comparison_id", c.comparison_id, row["comparison_id"]), ("prediction_id", c.prediction_id, row["prediction_id"]),
                       ("decision_id", c.decision_id, row["decision_id"]), ("chat_id", c.chat_id, row["chat_id"]),
                       ("status", c.status.value, row["status"]), ("evaluated_at", _utc(c.evaluated_at), _utc(row["evaluated_at"])),
                       ("contract_version", c.contract_version, row["contract_version"])):
        if a != b:
            raise PredictionCorrupt(f"row/payload mismatch on {name}")
    return c


class PredictionRepository:
    def __init__(self, session_factory):
        self._sf = session_factory

    @staticmethod
    async def _rows(s, where, order=False):
        q = select(T).where(where)
        if order:
            q = q.order_by(T.c.evaluated_at, T.c.comparison_id)
        try:
            return (await s.execute(q)).mappings().all()
        except ValueError as e:
            raise PredictionCorrupt(f"unreadable row: {e}") from None

    async def save(self, c: PredictionComparison) -> bool:
        if not isinstance(c, PredictionComparison):
            raise TypeError("c must be a PredictionComparison")
        row = {"comparison_id": c.comparison_id, "prediction_id": c.prediction_id, "decision_id": c.decision_id,
               "chat_id": c.chat_id, "status": c.status.value, "evaluated_at": c.evaluated_at.astimezone(timezone.utc),
               "contract_version": c.contract_version, "payload": c.to_dict()}
        async with self._sf() as s:
            dialect = s.bind.dialect.name if s.bind is not None else "postgresql"
            res = await s.execute((sqlite_insert if dialect == "sqlite" else pg_insert)(T).values(**row)
                                  .on_conflict_do_nothing(index_elements=["comparison_id"]))
            if res.rowcount:
                await s.commit()
                return True
            rows = await self._rows(s, T.c.comparison_id == c.comparison_id)
        if not rows:
            raise PredictionStoreError("insert ignored but no row")
        if _restore(rows[0]) != c:
            raise PredictionConflict(f"a different comparison is stored under {c.comparison_id!r}")
        return False

    async def get(self, comparison_id: str) -> PredictionComparison | None:
        async with self._sf() as s:
            rows = await self._rows(s, T.c.comparison_id == comparison_id)
        return _restore(rows[0]) if rows else None

    async def list_by_decision(self, decision_id: str) -> tuple[PredictionComparison, ...]:
        async with self._sf() as s:
            rows = await self._rows(s, T.c.decision_id == decision_id, order=True)
        return tuple(_restore(r) for r in rows)

    async def exists(self, comparison_id: str) -> bool:
        async with self._sf() as s:
            return (await s.execute(select(T.c.comparison_id).where(T.c.comparison_id == comparison_id).limit(1))).first() is not None
