"""prediction_comparisons: append-only, own MetaData, schema only (DDL = migrate.py). One row per evaluation: a later
evaluation of the same prediction is a NEW row (evaluated_at is part of the id), never an update. `payload` is the contract's
own to_dict(); the other columns index it and are verified against it on every read."""
from sqlalchemy import JSON, BigInteger, Column, DateTime, Index, MetaData, String, Table
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
_J = JSON().with_variant(JSONB(), "postgresql")

prediction_comparisons = Table(
    "prediction_comparisons", metadata,
    Column("comparison_id", String(40), primary_key=True),
    Column("prediction_id", String(40), nullable=False),
    Column("decision_id", String(40), nullable=False),
    Column("chat_id", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),
    Column("evaluated_at", DateTime(timezone=True), nullable=False),
    Column("contract_version", String(40), nullable=False),
    Column("payload", _J, nullable=False),
    Index("ix_pc_decision_id", "decision_id"),
    Index("ix_pc_prediction_id", "prediction_id"),
)
APPEND_ONLY_DDL = (
    """CREATE OR REPLACE FUNCTION prediction_comparisons_append_only() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'prediction_comparisons is immutable'; END; $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS trg_prediction_comparisons_append_only ON prediction_comparisons",
    """CREATE TRIGGER trg_prediction_comparisons_append_only BEFORE UPDATE ON prediction_comparisons
       FOR EACH ROW EXECUTE FUNCTION prediction_comparisons_append_only()""",
)
ROLLBACK_DDL = (
    "DROP TRIGGER IF EXISTS trg_prediction_comparisons_append_only ON prediction_comparisons",
    "DROP FUNCTION IF EXISTS prediction_comparisons_append_only()",
    "DROP TABLE IF EXISTS prediction_comparisons",
)
