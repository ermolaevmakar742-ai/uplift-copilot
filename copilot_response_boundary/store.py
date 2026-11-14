"""response_boundaries table: append-only. Own MetaData; touches no other table. Schema only (DDL = migrate.py).
decision_id nullable, NOT unique (a reply has several parts) and NOT a foreign key (unlinked sends exist).
telegram_message_id is NULL only for FAILED records. sent_at is the event time (no server default)."""
from sqlalchemy import JSON, BigInteger, Column, DateTime, Index, MetaData, String, Table
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
_J = JSON().with_variant(JSONB(), "postgresql")

response_boundaries = Table(
    "response_boundaries", metadata,
    Column("response_boundary_id", String(64), primary_key=True),
    Column("decision_id", String(40)),
    Column("chat_id", BigInteger, nullable=False),
    Column("telegram_message_id", BigInteger),
    Column("sent_at", DateTime(timezone=True), nullable=False),
    Column("status", String(16), nullable=False),
    Column("contract_version", String(40), nullable=False),
    Column("metadata", _J, nullable=False),
    Index("ix_rb_decision_id", "decision_id"),
    Index("ix_rb_chat_sent", "chat_id", "sent_at"),
)

APPEND_ONLY_DDL = (
    """CREATE OR REPLACE FUNCTION response_boundaries_append_only() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'response_boundaries is immutable'; END; $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS trg_response_boundaries_append_only ON response_boundaries",
    """CREATE TRIGGER trg_response_boundaries_append_only BEFORE UPDATE ON response_boundaries
       FOR EACH ROW EXECUTE FUNCTION response_boundaries_append_only()""",
)
ROLLBACK_DDL = (
    "DROP TRIGGER IF EXISTS trg_response_boundaries_append_only ON response_boundaries",
    "DROP FUNCTION IF EXISTS response_boundaries_append_only()",
    "DROP TABLE IF EXISTS response_boundaries",
)
