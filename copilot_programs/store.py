"""partner_programs table. Own MetaData: touches no existing table. Schema only: DDL is applied by the explicit
migration (migrate.py), never at import or runtime. Structured columns; only the two short lists
(countries, payment_methods) are JSON. updated_at / updated_by on every row; no foreign keys."""
from sqlalchemy import JSON, Boolean, Column, DateTime, Index, MetaData, Numeric, String, Table, Text
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
_J = JSON().with_variant(JSONB(), "postgresql")

partner_programs = Table(
    "partner_programs", metadata,
    Column("program_id", String(64), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("brand", String(120), nullable=False),
    Column("countries", _J, nullable=False),
    Column("currency", String(3)),
    Column("registration_url", Text),
    Column("promo_code", String(64)),
    Column("min_deposit", Numeric(18, 2)),
    Column("min_withdrawal", Numeric(18, 2)),
    Column("withdrawal_time", String(120)),
    Column("payment_methods", _J, nullable=False),
    Column("terms", Text),
    Column("support_contact", String(255)),
    Column("notes", Text),
    Column("active", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("updated_by", String(64), nullable=False),
    Index("ix_partner_programs_active", "active"),
)

ROLLBACK_DDL = ("DROP TABLE IF EXISTS partner_programs",)
