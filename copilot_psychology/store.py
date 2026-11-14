"""Persistence: ONE append-only table `psychology_assessments` (own MetaData). One row per production decision (UNIQUE decision_id)."""
from sqlalchemy import BigInteger, Column, DateTime, Index, MetaData, String, Table, UniqueConstraint, JSON
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
_J = JSON().with_variant(JSONB(), "postgresql")
psychology_assessments = Table(
    "psychology_assessments", metadata,
    Column("assessment_id", String(40), primary_key=True),
    Column("decision_id", String(40), nullable=False),
    Column("chat_id", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("knowledge_version", String(32), nullable=False),
    Column("engine_version", String(40), nullable=False),
    Column("strategy_version", String(32), nullable=False),
    Column("psychological_state", String(40), nullable=False),
    Column("recommended_strategy", String(40), nullable=False),
    Column("payload", _J, nullable=False),
    UniqueConstraint("decision_id", name="uq_psychology_decision_id"),
    Index("ix_psychology_chat_time", "chat_id", "created_at"),
)
TABLES = ("psychology_assessments",)
APPEND_ONLY_DDL = (
    """CREATE OR REPLACE FUNCTION psychology_assessments_append_only() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'psychology_assessments is append-only'; END; $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS trg_psychology_assessments_append_only ON psychology_assessments",
    """CREATE TRIGGER trg_psychology_assessments_append_only BEFORE UPDATE OR DELETE ON psychology_assessments
       FOR EACH ROW EXECUTE FUNCTION psychology_assessments_append_only()""",
)
ROLLBACK_DDL = ("DROP TRIGGER IF EXISTS trg_psychology_assessments_append_only ON psychology_assessments",
                "DROP FUNCTION IF EXISTS psychology_assessments_append_only()", "DROP TABLE IF EXISTS psychology_assessments")
