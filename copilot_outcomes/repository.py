"""Persistence for Outcome. INSERT-only API: save / get / exists. No update, upsert, replace or delete.

save(): first save inserts; the same outcome_id with IDENTICAL content is an idempotent no-op (False); the same
outcome_id with DIFFERENT content raises OutcomeConflict. Never overwrites.
get(): rebuilds the object only through Outcome.from_dict (the contract is the single serializer); a row that
is corrupt, of another contract version or fails validation raises OutcomeCorrupt. Nothing is repaired.
The columns are the only copy of the data (no redundant snapshot), so a row that is still schema-valid but was
edited out-of-band cannot be told apart from the original: immutability rests on the PG UPDATE trigger.

The repository stores the Outcome as observed: no join, no attribution, no decision lookup, no clock (observed_at is
the event time), no DDL (see migrate.py), no production imports. Not wired into production."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from copilot_evidence.contracts import ContractError
from copilot_outcomes.contracts import Outcome
from copilot_outcomes.store import outcomes


class OutcomeStoreError(Exception):
    """Base class for repository errors."""


class OutcomeConflict(OutcomeStoreError):
    """A different Outcome is already stored under this outcome_id."""


class OutcomeCorrupt(OutcomeStoreError):
    """Stored row is unreadable, incompatible or invalid."""


def _iso_utc(dt):
    if not isinstance(dt, datetime):
        return dt
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()  # sqlite drops tz


def _restore(row) -> Outcome:
    try:
        return Outcome.from_dict({
            "contract_version": row["contract_version"], "outcome_id": row["outcome_id"],
            "decision_id": row["decision_id"], "chat_id": row["chat_id"], "observed_at": _iso_utc(row["observed_at"]),
            "outcome_type": row["outcome_type"], "value": row["value"], "confidence": row["confidence"],
            "source": row["source"], "metadata": row["metadata"]})
    except (ContractError, TypeError, ValueError, KeyError) as e:
        raise OutcomeCorrupt(f"outcome {row['outcome_id']!r} unreadable: {e}") from None


class OutcomeRepository:
    def __init__(self, session_factory):
        self._sf = session_factory

    @staticmethod
    async def _fetch(session, outcome_id):
        try:
            return (await session.execute(select(outcomes).where(outcomes.c.outcome_id == outcome_id))).mappings().first()
        except ValueError as e:        # e.g. undecodable JSON in a stored column
            raise OutcomeCorrupt(f"outcome {outcome_id!r} unreadable: {e}") from None

    async def save(self, outcome: Outcome) -> bool:
        """True = inserted, False = identical outcome already stored. Different content -> OutcomeConflict."""
        if not isinstance(outcome, Outcome):
            raise TypeError("outcome must be an Outcome")
        d = outcome.to_dict()
        row = {"outcome_id": d["outcome_id"], "decision_id": d["decision_id"], "chat_id": d["chat_id"],
               "observed_at": outcome.observed_at.astimezone(timezone.utc), "outcome_type": d["outcome_type"], "value": d["value"],
               "confidence": d["confidence"], "source": d["source"], "contract_version": d["contract_version"],
               "metadata": d["metadata"]}
        async with self._sf() as s:
            dialect = s.bind.dialect.name if s.bind is not None else "postgresql"
            ins = (sqlite_insert if dialect == "sqlite" else pg_insert)(outcomes).values(**row)
            res = await s.execute(ins.on_conflict_do_nothing(index_elements=["outcome_id"]))
            if res.rowcount:
                await s.commit()
                return True
            existing = await self._fetch(s, outcome.outcome_id)
        if existing is None:
            raise OutcomeStoreError("insert ignored but no row for this outcome_id")
        if _restore(existing) != outcome:
            raise OutcomeConflict(f"a different outcome is already stored under {outcome.outcome_id!r}")
        return False

    async def get(self, outcome_id: str) -> Outcome | None:
        async with self._sf() as s:
            row = await self._fetch(s, outcome_id)
        return None if row is None else _restore(row)

    async def exists(self, outcome_id: str) -> bool:
        async with self._sf() as s:
            return (await s.execute(select(outcomes.c.outcome_id).where(
                outcomes.c.outcome_id == outcome_id).limit(1))).first() is not None
