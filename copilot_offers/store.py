"""Offer tables (own MetaData; touch NO existing table, partner_programs stays as is). Schema only: DDL is applied by migrate.py,
never implicitly. No raw chat id, no Telegram token/account id, no binary data (images are http(s) URL references)."""
from sqlalchemy import (BigInteger, Boolean, Column, DateTime, Index, Integer, MetaData, String, Table, Text, func)

metadata = MetaData()

# 1 Telegram account -> at most 1 offer (PK = account_ref). Replace = update, remove = delete.
offer_account_assignments = Table(
    "offer_account_assignments", metadata,
    Column("account_ref", String(32), primary_key=True),
    Column("program_id", String(64), nullable=False),          # partner_programs.program_id (no FK: that table stays independent)
    Column("assigned_by", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_offer_assignments_program_id", "program_id"),
)

# Offer content that partner_programs has no column for (kept separate so the existing table / CRM keep working unchanged).
offer_details = Table(
    "offer_details", metadata,
    Column("program_id", String(64), primary_key=True),
    Column("description", Text),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

offer_images = Table(
    "offer_images", metadata,
    Column("image_id", String(40), primary_key=True),          # img_<sha1(program_id, url)[:16]>: stable per (offer, url)
    Column("program_id", String(64), nullable=False),
    Column("url", Text, nullable=False),
    Column("sort_order", Integer, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_offer_images_program_id", "program_id", "sort_order"),
)

# Observability: what the runtime WOULD use for this account at this decision. Append-only.
offer_decisions = Table(
    "offer_decisions", metadata,
    Column("id", BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True),
    Column("offer_decision_id", String(40), nullable=False, unique=True),
    Column("correlation_id", String(40), nullable=False),     # production decision_id (pd_...)
    Column("client_ref", String(64), nullable=False),         # cli_... pseudonym
    Column("account_ref", String(32)),
    Column("program_id", String(64)),
    Column("status", String(24), nullable=False),
    Column("reason", String(32), nullable=False),
    Column("policy_version", String(32), nullable=False),
    Column("stage_canonical", String(16), nullable=False),
    Column("stage_raw", String(32)),
    Column("mode", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_offer_decisions_correlation_id", "correlation_id"),
    Index("ix_offer_decisions_client_ref", "client_ref"),
    Index("ix_offer_decisions_program_id", "program_id"),
    Index("ix_offer_decisions_created_at", "created_at"),
)

APPEND_ONLY_DDL = (
    """CREATE OR REPLACE FUNCTION offer_decisions_append_only() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'offer_decisions is immutable'; END; $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS trg_offer_decisions_append_only ON offer_decisions",
    """CREATE TRIGGER trg_offer_decisions_append_only BEFORE UPDATE OR DELETE ON offer_decisions
       FOR EACH ROW EXECUTE FUNCTION offer_decisions_append_only()""",
)
TABLES = (offer_account_assignments, offer_details, offer_images, offer_decisions)
ROLLBACK_DDL = (
    "DROP TRIGGER IF EXISTS trg_offer_decisions_append_only ON offer_decisions",
    "DROP FUNCTION IF EXISTS offer_decisions_append_only()",
    "DROP TABLE IF EXISTS offer_decisions",
    "DROP TABLE IF EXISTS offer_images",
    "DROP TABLE IF EXISTS offer_details",
    "DROP TABLE IF EXISTS offer_account_assignments",
)
