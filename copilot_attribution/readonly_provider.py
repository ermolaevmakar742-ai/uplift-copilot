"""Read-only data provider for the offline evaluator: SELECT statements only (SQLAlchemy Core `select()`), plus
`SET TRANSACTION READ ONLY` on PostgreSQL so the database itself refuses any write from this session.
Local, minimal table definitions (only the columns read) — no import of other packages' stores, no DDL, no
repository, no recorder. Rows that cannot be read as valid contracts are COUNTED (`unreadable`) and reported,
never repaired and never silently dropped. If the interruption source is unavailable that is reported in `notes`.
Loads whole tables: fine for the current volume; add filters before using on large data."""
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any

from sqlalchemy import JSON, BigInteger, Column, DateTime, Float, MetaData, String, Table, select, text
from sqlalchemy.dialects.postgresql import JSONB

from copilot_attribution.contract import DecisionRef, Interruption
from copilot_outcomes.contracts import Outcome

_md = MetaData()                      # private; never used for create_all
_J = JSON().with_variant(JSONB(), "postgresql")
_decisions = Table("production_decisions", _md, Column("decision_id", String), Column("chat_id", BigInteger),
                   Column("created_at", DateTime(timezone=True)))
_outcomes = Table("outcomes", _md, Column("outcome_id", String), Column("decision_id", String),
                  Column("chat_id", BigInteger), Column("observed_at", DateTime(timezone=True)),
                  Column("outcome_type", String), Column("value", _J), Column("confidence", Float(53)),
                  Column("source", String), Column("contract_version", String), Column("metadata", _J))
_activity = Table("ai_activity", _md, Column("user_id", BigInteger), Column("event_type", String),
                  Column("created_at", DateTime(timezone=True)))

# ai_activity.event_type -> Interruption kind (the only timestamped intervening-action events that exist today)
INTERRUPTION_EVENTS = {"followup_sent": "FOLLOWUP", "fixed_trigger": "FIXED_TRIGGER", "insult_block": "INSULT_BLOCK"}


@dataclass(frozen=True)
class LoadedData:
    decisions: tuple
    outcomes: tuple
    interruptions: tuple
    unreadable: dict = field(default_factory=dict)
    notes: tuple = ()


def read_only_sql(dialect_name: str) -> str | None:
    """Statement that makes the session read-only at the database level (PostgreSQL); None where unsupported."""
    return "SET TRANSACTION READ ONLY" if dialect_name == "postgresql" else None


class SqlReadOnlyProvider:
    def __init__(self, session_factory, *, assume_utc_naive: bool = False):
        self._sf = session_factory
        self._utc = assume_utc_naive            # only for SQLite (drops tz); PostgreSQL returns aware values

    def _t(self, v: Any):
        return v.replace(tzinfo=timezone.utc) if (self._utc and v is not None and v.tzinfo is None) else v

    async def load(self) -> LoadedData:
        unreadable = {"decisions": 0, "outcomes": 0, "interruptions": 0}
        notes: list[str] = []
        async with self._sf() as s:
            bind = s.bind
            ro = read_only_sql(bind.dialect.name if bind is not None else "postgresql")
            if ro:
                await s.execute(text(ro))
            drows = (await s.execute(select(_decisions).order_by(_decisions.c.decision_id))).mappings().all()
            orows = (await s.execute(select(_outcomes).order_by(_outcomes.c.outcome_id))).mappings().all()
            try:
                arows = (await s.execute(select(_activity).where(_activity.c.event_type.in_(list(INTERRUPTION_EVENTS)))
                                         .where(_activity.c.user_id.is_not(None))
                                         .order_by(_activity.c.created_at))).mappings().all()
            except Exception:
                arows = []
                notes.append("interruptions_unavailable: ai_activity could not be read; interruption rules had no input")
            await s.rollback()                    # ends the read-only transaction; nothing to commit
        decisions, outcomes, ints = [], [], []
        for r in drows:
            try:
                decisions.append(DecisionRef(r["decision_id"], r["chat_id"], self._t(r["created_at"])))
            except Exception:
                unreadable["decisions"] += 1
        for r in orows:
            try:
                outcomes.append(Outcome.from_dict({
                    "contract_version": r["contract_version"], "outcome_id": r["outcome_id"],
                    "decision_id": r["decision_id"], "chat_id": r["chat_id"],
                    "observed_at": (lambda t: t.isoformat() if t is not None else None)(self._t(r["observed_at"])),
                    "outcome_type": r["outcome_type"], "value": r["value"], "confidence": r["confidence"],
                    "source": r["source"], "metadata": r["metadata"]}))
            except Exception:
                unreadable["outcomes"] += 1
        for r in arows:
            try:
                ints.append(Interruption(INTERRUPTION_EVENTS[r["event_type"]], r["user_id"], self._t(r["created_at"])))
            except Exception:
                unreadable["interruptions"] += 1
        return LoadedData(tuple(decisions), tuple(outcomes), tuple(ints), unreadable, tuple(notes))
