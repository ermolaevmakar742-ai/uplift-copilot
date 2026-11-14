"""outcomes table: append-only record of OBSERVED outcomes, exactly as the Outcome contract defines them.
Own MetaData (touches no other table). Schema only: DDL is applied by the explicit migration (migrate.py).

decision_id is nullable and NOT unique and NOT a foreign key: outcomes are written independently of decisions
(postback outcomes usually carry no decision_id); attribution is the joiner's job at read time, never storage's.
observed_at is the source event's time (never INSERT time: no server default)."""
from sqlalchemy import JSON, BigInteger, Column, DateTime, Float, Index, MetaData, String, Table
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
_J = JSON().with_variant(JSONB(), "postgresql")

outcomes = Table(
    "outcomes", metadata,
    Column("outcome_id", String(255), primary_key=True),
    Column("decision_id", String(40)),                                   # NULL = not linked by the source
    Column("chat_id", BigInteger, nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("outcome_type", String(32), nullable=False),
    Column("value", _J, nullable=False),                                  # JSON null allowed, SQL NULL not
    Column("confidence", Float(53)),                                      # NULL = source gave none
    Column("source", String(32), nullable=False),
    Column("contract_version", String(40), nullable=False),
    Column("metadata", _J, nullable=False),
    Index("ix_outcomes_decision_id", "decision_id"),
    Index("ix_outcomes_chat_observed", "chat_id", "observed_at"),
)

# Postgres-only guard: any UPDATE is rejected (history must not be rewritten).
APPEND_ONLY_DDL = (
    """CREATE OR REPLACE FUNCTION outcomes_append_only() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'outcomes is immutable'; END; $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS trg_outcomes_append_only ON outcomes",
    """CREATE TRIGGER trg_outcomes_append_only BEFORE UPDATE ON outcomes
       FOR EACH ROW EXECUTE FUNCTION outcomes_append_only()""",
)
ROLLBACK_DDL = (
    "DROP TRIGGER IF EXISTS trg_outcomes_append_only ON outcomes",
    "DROP FUNCTION IF EXISTS outcomes_append_only()",
    "DROP TABLE IF EXISTS outcomes",
)
