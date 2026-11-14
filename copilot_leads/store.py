"""Persistence: ONE new append-only table `lead_stage_overrides` (own MetaData). Read-only views of existing tables are declared in
repository.py with their own MetaData and are never created or altered here."""
from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, Index, MetaData, String, Table

metadata = MetaData()
lead_stage_overrides = Table(
    "lead_stage_overrides", metadata,
    Column("override_id", String(40), primary_key=True),
    Column("chat_id", BigInteger, nullable=False),
    Column("previous_stage", String(16), nullable=False),
    Column("new_stage", String(16), nullable=False),
    Column("actor", String(64), nullable=False),
    Column("source", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("previous_override_id", String(40)),
    Column("contract_version", String(40), nullable=False),
    CheckConstraint("source = 'human'", name="ck_lead_override_source"),
    CheckConstraint("previous_stage <> new_stage", name="ck_lead_override_changed"),
    Index("ix_lead_override_chat_time", "chat_id", "created_at"),
)
TABLES = ("lead_stage_overrides",)
APPEND_ONLY_DDL = (
    """CREATE OR REPLACE FUNCTION lead_stage_overrides_append_only() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'lead_stage_overrides is append-only'; END; $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS trg_lead_stage_overrides_append_only ON lead_stage_overrides",
    """CREATE TRIGGER trg_lead_stage_overrides_append_only BEFORE UPDATE OR DELETE ON lead_stage_overrides
       FOR EACH ROW EXECUTE FUNCTION lead_stage_overrides_append_only()""",
)
ROLLBACK_DDL = ("DROP TRIGGER IF EXISTS trg_lead_stage_overrides_append_only ON lead_stage_overrides",
                "DROP FUNCTION IF EXISTS lead_stage_overrides_append_only()", "DROP TABLE IF EXISTS lead_stage_overrides")
