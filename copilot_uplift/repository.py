"""Append-only repositories for the uplift tables. INSERT/SELECT only; strict contract decoding; indexed columns must agree with
the payload (else UpliftCorrupt). Same id + identical content -> False; same id + different content -> UpliftConflict.
No update / upsert / delete API, no DDL, no clock."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from copilot_evidence.contracts import ContractError
from copilot_uplift.contract import ActivationEvent, ShadowRecord, UpliftEvaluation
from copilot_uplift.store import uplift_activation_events as TA, uplift_assignments as TG, uplift_evaluations as TE, uplift_shadow as TS


class UpliftStoreError(Exception):
    pass


class UpliftConflict(UpliftStoreError):
    pass


class UpliftCorrupt(UpliftStoreError):
    pass


def _utc(dt):
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc) if isinstance(dt, datetime) else dt


class _Repo:
    table = None
    id_col = ""
    codec = None

    def __init__(self, session_factory):
        self._sf = session_factory

    def _check(self, obj, row):                      # (name, from object, from row) triples
        raise NotImplementedError

    def _row(self, obj) -> dict:
        raise NotImplementedError

    def _restore(self, row):
        try:
            obj = self.codec.from_dict(row["payload"])
        except (ContractError, TypeError, ValueError, KeyError) as e:
            raise UpliftCorrupt(f"{self.table.name}: unreadable row: {e}") from None
        for name, a, b in self._check(obj, row):
            if _utc(a) != _utc(b):
                raise UpliftCorrupt(f"{self.table.name}: row/payload mismatch on {name}")
        return obj

    async def _select(self, s, where=None, order=(), limit=None):
        q = select(self.table)
        if where is not None:
            q = q.where(where)
        if order:
            q = q.order_by(*order)
        if limit:
            q = q.limit(limit)
        try:
            return (await s.execute(q)).mappings().all()
        except ValueError as e:
            raise UpliftCorrupt(f"{self.table.name}: unreadable row: {e}") from None

    async def _insert(self, obj, conflict_where) -> bool:
        async with self._sf() as s:
            dialect = s.bind.dialect.name if s.bind is not None else "postgresql"
            res = await s.execute((sqlite_insert if dialect == "sqlite" else pg_insert)(self.table).values(**self._row(obj)).on_conflict_do_nothing())
            if res.rowcount:
                await s.commit()
                return True
            rows = await self._select(s, conflict_where)
        if not rows:
            raise UpliftStoreError("insert ignored but no conflicting row")
        if self._restore(rows[0]) != obj:
            raise UpliftConflict(f"a different record already exists in {self.table.name}")
        return False


class ShadowRepository(_Repo):
    table, codec = TS, ShadowRecord

    def _row(self, r):
        return {"shadow_id": r.shadow_id, "decision_id": r.decision_id, "chat_id": r.chat_id, "computed_at": r.computed_at.astimezone(timezone.utc),
                "policy_version": r.policy_version, "data_quality": r.data_quality.value, "contract_version": r.contract_version, "payload": r.to_dict()}

    def _check(self, r, row):
        return (("shadow_id", r.shadow_id, row["shadow_id"]), ("decision_id", r.decision_id, row["decision_id"]), ("chat_id", r.chat_id, row["chat_id"]),
                ("computed_at", r.computed_at, row["computed_at"]), ("policy_version", r.policy_version, row["policy_version"]),
                ("data_quality", r.data_quality.value, row["data_quality"]), ("contract_version", r.contract_version, row["contract_version"]))

    async def save(self, r: ShadowRecord) -> bool:
        if not isinstance(r, ShadowRecord):
            raise TypeError("r must be a ShadowRecord")
        return await self._insert(r, TS.c.decision_id == r.decision_id)

    async def get_by_decision_id(self, decision_id: str):
        async with self._sf() as s:
            rows = await self._select(s, TS.c.decision_id == decision_id)
        return self._restore(rows[0]) if rows else None

    async def count(self) -> int:
        from sqlalchemy import func
        async with self._sf() as s:
            return (await s.execute(select(func.count()).select_from(TS))).scalar() or 0


class EvaluationRepository(_Repo):
    table, codec = TE, UpliftEvaluation

    def _row(self, e):
        return {"evaluation_id": e.evaluation_id, "evaluation_timestamp": e.evaluation_timestamp.astimezone(timezone.utc),
                "policy_version": e.policy_version, "data_quality": e.data_quality.value, "dataset_version": e.dataset_version,
                "contract_version": e.contract_version, "payload": e.to_dict()}

    def _check(self, e, row):
        return (("evaluation_id", e.evaluation_id, row["evaluation_id"]), ("evaluation_timestamp", e.evaluation_timestamp, row["evaluation_timestamp"]),
                ("policy_version", e.policy_version, row["policy_version"]), ("data_quality", e.data_quality.value, row["data_quality"]),
                ("dataset_version", e.dataset_version, row["dataset_version"]), ("contract_version", e.contract_version, row["contract_version"]))

    async def save(self, e: UpliftEvaluation) -> bool:
        if not isinstance(e, UpliftEvaluation):
            raise TypeError("e must be an UpliftEvaluation")
        return await self._insert(e, TE.c.evaluation_id == e.evaluation_id)

    async def get(self, evaluation_id: str):
        async with self._sf() as s:
            rows = await self._select(s, TE.c.evaluation_id == evaluation_id)
        return self._restore(rows[0]) if rows else None

    async def latest(self):
        async with self._sf() as s:
            rows = await self._select(s, None, (TE.c.evaluation_timestamp.desc(), TE.c.evaluation_id.desc()), 1)
        return self._restore(rows[0]) if rows else None


class ActivationRepository(_Repo):
    table, codec = TA, ActivationEvent

    def _row(self, e):
        return {"event_id": e.event_id, "previous_state": e.previous_state.value, "new_state": e.new_state.value, "changed_by": e.changed_by,
                "changed_at": e.changed_at.astimezone(timezone.utc), "policy_version": e.policy_version,
                "contract_version": e.contract_version, "payload": e.to_dict()}

    def _check(self, e, row):
        return (("event_id", e.event_id, row["event_id"]), ("previous_state", e.previous_state.value, row["previous_state"]),
                ("new_state", e.new_state.value, row["new_state"]), ("changed_by", e.changed_by, row["changed_by"]),
                ("changed_at", e.changed_at, row["changed_at"]), ("policy_version", e.policy_version, row["policy_version"]),
                ("contract_version", e.contract_version, row["contract_version"]))

    async def append(self, e: ActivationEvent) -> bool:
        if not isinstance(e, ActivationEvent):
            raise TypeError("e must be an ActivationEvent")
        return await self._insert(e, TA.c.event_id == e.event_id)

    async def list_events(self) -> tuple[ActivationEvent, ...]:
        async with self._sf() as s:
            rows = await self._select(s, None, (TA.c.changed_at, TA.c.event_id))
        return tuple(self._restore(r) for r in rows)


class AssignmentRepository:
    """Append-only assignment rows (one per chat, unique chat_id). Plain validated columns, not a strict codec: the coin is
    deterministic and re-derivable, so the row is an audit trail, not the source of truth. INSERT only; no update/delete."""

    def __init__(self, session_factory):
        self._sf = session_factory

    async def save(self, *, assignment_id: str, chat_id: int, arm: str, assignment_probability: float,
                   decided_at: datetime, context: str, contract_version: str) -> bool:
        if arm not in ("CONTROL", "TREATMENT"):
            raise UpliftStoreError("arm must be CONTROL or TREATMENT")
        if not (isinstance(chat_id, int) and not isinstance(chat_id, bool)):
            raise UpliftStoreError("chat_id must be an int")
        if not (isinstance(assignment_probability, float) and 0.0 < assignment_probability < 1.0):
            raise UpliftStoreError("assignment_probability must be within (0, 1)")
        row = {"assignment_id": assignment_id, "chat_id": chat_id, "arm": arm,
               "assignment_probability": assignment_probability, "decided_at": _utc(decided_at),
               "context": (context or "")[:64], "contract_version": contract_version,
               "payload": {"assignment_id": assignment_id, "chat_id": chat_id, "arm": arm,
                           "assignment_probability": assignment_probability, "context": (context or "")[:64],
                           "contract_version": contract_version}}
        async with self._sf() as s:
            dialect = s.bind.dialect.name if s.bind is not None else "postgresql"
            res = await s.execute((sqlite_insert if dialect == "sqlite" else pg_insert)(TG).values(**row).on_conflict_do_nothing())
            if res.rowcount:
                await s.commit()
                return True
        return False

    async def count(self) -> int:
        from sqlalchemy import func
        async with self._sf() as s:
            return (await s.execute(select(func.count()).select_from(TG))).scalar() or 0

    async def count_by_arm(self) -> dict:
        from sqlalchemy import func
        async with self._sf() as s:
            rows = (await s.execute(select(TG.c.arm, func.count()).select_from(TG).group_by(TG.c.arm))).all()
        return {arm: n for arm, n in rows}
