"""decision_evidence table: one immutable canonical DecisionEvidence snapshot per production decision.
Own MetaData (does not touch production tables, main.Base or production_decisions). This module defines
the schema only; DDL is applied by the explicit migration (migrate.py), never at import/runtime.

The `snapshot` column holds DecisionEvidence.to_dict() verbatim; the other columns are a thin index over
it (verified against the snapshot on every read). UNIQUE(decision_id) = one snapshot per decision, and
also gives the decision_id index."""
from sqlalchemy import JSON, BigInteger, Column, DateTime, MetaData, String, Table, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
_J = JSON().with_variant(JSONB(), "postgresql")

decision_evidence = Table(
    "decision_evidence", metadata,
    Column("decision_evidence_id", String(40), primary_key=True),
    Column("decision_id", String(40), nullable=False),
    Column("chat_id", BigInteger, nullable=False),
    Column("message_id", BigInteger),                                  # NULL when production has none
    Column("created_at", DateTime(timezone=True), nullable=False),     # snapshot creation time (from contract)
    Column("contract_version", String(40), nullable=False),
    Column("snapshot", _J, nullable=False),
    UniqueConstraint("decision_id", name="uq_de_decision_id"),
)

# Postgres-only guard: any UPDATE is rejected (history must not be rewritten).
APPEND_ONLY_DDL = (
    """CREATE OR REPLACE FUNCTION decision_evidence_append_only() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'decision_evidence is immutable'; END; $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS trg_decision_evidence_append_only ON decision_evidence",
    """CREATE TRIGGER trg_decision_evidence_append_only BEFORE UPDATE ON decision_evidence
       FOR EACH ROW EXECUTE FUNCTION decision_evidence_append_only()""",
)
ROLLBACK_DDL = (
    "DROP TRIGGER IF EXISTS trg_decision_evidence_append_only ON decision_evidence",
    "DROP FUNCTION IF EXISTS decision_evidence_append_only()",
    "DROP TABLE IF EXISTS decision_evidence",
)
