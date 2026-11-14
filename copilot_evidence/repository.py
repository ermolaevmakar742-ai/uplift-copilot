"""Persistence for DecisionEvidence. API is deliberately minimal and INSERT-only:
save / get_by_decision_id / exists. No update, upsert, replace or delete exists.

save(): first save inserts; saving the same decision_id again with an IDENTICAL snapshot is an
idempotent no-op (returns False); a DIFFERENT snapshot raises DecisionEvidenceConflict. Never overwrites.
get_by_decision_id(): rebuilds the object only through DecisionEvidence.from_dict (the contract is the
single serializer) and refuses to return a snapshot that is corrupt, of an incompatible contract
version, or inconsistent with its row columns (DecisionEvidenceCorrupt). Nothing is repaired.

No DDL here (see migrate.py) and no production imports. Not wired into production."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from copilot_evidence.contracts import ContractError
from copilot_evidence.decision import DecisionEvidence
from copilot_evidence.store import decision_evidence


class DecisionEvidenceStoreError(Exception):
    """Base class for repository errors."""


class DecisionEvidenceConflict(DecisionEvidenceStoreError):
    """A different snapshot already exists for this decision_id."""


class DecisionEvidenceCorrupt(DecisionEvidenceStoreError):
    """Stored snapshot is unreadable, incompatible or inconsistent with its row."""


def _utc(dt):
    if not isinstance(dt, datetime):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)   # sqlite drops tz


def _restore(row) -> DecisionEvidence:
    try:
        snap = DecisionEvidence.from_dict(row["snapshot"])
    except (ContractError, TypeError, ValueError, KeyError) as e:
        raise DecisionEvidenceCorrupt(f"snapshot unreadable for {row['decision_id']!r}: {e}") from None
    checks = (("decision_evidence_id", snap.decision_evidence_id, row["decision_evidence_id"]),
              ("decision_id", snap.decision_id, row["decision_id"]),
              ("chat_id", snap.chat_id, row["chat_id"]),
              ("message_id", snap.message_id, row["message_id"]),
              ("created_at", _utc(snap.created_at), _utc(row["created_at"])),
              ("contract_version", snap.contract_version, row["contract_version"]))
    for name, a, b in checks:
        if a != b:
            raise DecisionEvidenceCorrupt(f"row/snapshot mismatch on {name} for {row['decision_id']!r}")
    return snap


class DecisionEvidenceRepository:
    def __init__(self, session_factory):
        self._sf = session_factory

    async def save(self, snapshot: DecisionEvidence) -> bool:
        """True = inserted, False = identical snapshot already stored. Conflict -> exception."""
        if not isinstance(snapshot, DecisionEvidence):
            raise TypeError("snapshot must be a DecisionEvidence")
        data = snapshot.to_dict()
        row = {"decision_evidence_id": snapshot.decision_evidence_id, "decision_id": snapshot.decision_id,
               "chat_id": snapshot.chat_id, "message_id": snapshot.message_id,
               "created_at": snapshot.created_at, "contract_version": snapshot.contract_version,
               "snapshot": data}
        async with self._sf() as s:
            dialect = s.bind.dialect.name if s.bind is not None else "postgresql"
            ins = (sqlite_insert if dialect == "sqlite" else pg_insert)(decision_evidence).values(**row)
            res = await s.execute(ins.on_conflict_do_nothing())
            if res.rowcount:
                await s.commit()
                return True
            existing = (await s.execute(select(decision_evidence).where(
                decision_evidence.c.decision_id == snapshot.decision_id))).mappings().first()
        if existing is None:
            raise DecisionEvidenceStoreError("insert ignored but no row for this decision_id (key collision)")
        if _restore(existing) != snapshot:
            raise DecisionEvidenceConflict(
                f"a different snapshot is already stored for decision_id {snapshot.decision_id!r}")
        return False

    async def get_by_decision_id(self, decision_id: str) -> DecisionEvidence | None:
        async with self._sf() as s:
            row = (await s.execute(select(decision_evidence).where(
                decision_evidence.c.decision_id == decision_id))).mappings().first()
        return None if row is None else _restore(row)

    async def exists(self, decision_id: str) -> bool:
        async with self._sf() as s:
            return (await s.execute(select(decision_evidence.c.decision_evidence_id).where(
                decision_evidence.c.decision_id == decision_id).limit(1))).first() is not None
