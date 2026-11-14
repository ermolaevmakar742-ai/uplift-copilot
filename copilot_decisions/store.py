"""production_decisions: append-only. This module only INSERTs (ON CONFLICT DO NOTHING).
No UPDATE, no DDL here: the schema is created by the explicit migration (migrate.py)."""
from sqlalchemy import (JSON, BigInteger, Boolean, Column, DateTime, Index, Integer, MetaData, String,
                        Table, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

metadata = MetaData()
_J = JSON().with_variant(JSONB(), "postgresql")

production_decisions = Table(
    "production_decisions", metadata,
    Column("id", BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True),
    Column("decision_id", String(40), nullable=False),
    Column("chat_id", BigInteger, nullable=False),
    Column("message_id", BigInteger),                       # NULL only when production has none
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("decision_path", String(32), nullable=False),
    Column("regime", String(16)),
    Column("model", String(128)),
    Column("flow_version", String(32)),
    Column("used_fallback", Boolean),
    Column("reply_parts", Integer),
    Column("trigger", String(64)),
    Column("experiment_id", Integer),
    Column("experiment_variant", String(32)),
    Column("objection_signal", String(16)),
    Column("language_mode", String(16)),
    Column("client_state_snapshot", _J),
    Column("metadata", _J, nullable=False),
    UniqueConstraint("decision_id", name="uq_pd_decision_id"),
    UniqueConstraint("chat_id", "message_id", name="uq_pd_chat_message"),   # NULL message_id never conflicts
    Index("ix_pd_chat_created", "chat_id", "created_at"),
    Index("ix_pd_message_id", "message_id"),
)

# Postgres-only guard: any UPDATE is rejected (history must not be rewritten).
APPEND_ONLY_DDL = (
    """CREATE OR REPLACE FUNCTION production_decisions_append_only() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'production_decisions is append-only'; END; $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS trg_production_decisions_append_only ON production_decisions",
    """CREATE TRIGGER trg_production_decisions_append_only BEFORE UPDATE ON production_decisions
       FOR EACH ROW EXECUTE FUNCTION production_decisions_append_only()""",
)


async def insert_decision(session_factory, row: dict) -> bool:
    """True if inserted, False if a record for this decision_id / (chat_id, message_id) already exists."""
    async with session_factory() as s:
        dialect = s.bind.dialect.name if s.bind is not None else "postgresql"
        ins = (sqlite_insert if dialect == "sqlite" else pg_insert)(production_decisions).values(**row)
        res = await s.execute(ins.on_conflict_do_nothing())
        await s.commit()
        return bool(res.rowcount)
