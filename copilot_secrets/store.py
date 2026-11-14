"""Schema (own MetaData; DDL only via migration.py).
  llm_secrets      one row per key VERSION. encrypted_value is NULL once a version is revoked / deleted (crypto-erase). At most one ACTIVE row per slot.
  llm_secret_audit append-only operation log (never contains a value)."""
from sqlalchemy import BigInteger, Boolean, Column, DateTime, Index, Integer, MetaData, String, Table, Text, UniqueConstraint, text

metadata = MetaData()
llm_secrets = Table(
    "llm_secrets", metadata,
    Column("secret_id", String(40), primary_key=True),
    Column("slot", String(16), nullable=False),
    Column("version", Integer, nullable=False),
    Column("provider", String(64), nullable=False),
    Column("key_slot", String(16), nullable=False),
    Column("encrypted_value", Text),
    Column("masked", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("last_tested_at", DateTime(timezone=True)),
    Column("last_test_result", String(16)),
    UniqueConstraint("slot", "version", name="uq_llm_secrets_slot_version"),
    Index("uq_llm_secrets_one_active", "slot", unique=True, postgresql_where=text("status = 'ACTIVE'"), sqlite_where=text("status = 'ACTIVE'")),
)
llm_secret_audit = Table(
    "llm_secret_audit", metadata,
    Column("event_id", String(40), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("actor", String(64), nullable=False),
    Column("key_slot", String(16), nullable=False),
    Column("provider", String(64)),
    Column("operation", String(16), nullable=False),
    Column("success", Boolean, nullable=False),
    Column("reason", String(120)),
    Column("version", Integer),
    Index("ix_llm_secret_audit_time", "created_at"),
)
TABLES = ("llm_secrets", "llm_secret_audit")
APPEND_ONLY_DDL = (
    # the audit log is immutable
    """CREATE OR REPLACE FUNCTION llm_secret_audit_append_only() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'llm_secret_audit is append-only'; END; $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS trg_llm_secret_audit_append_only ON llm_secret_audit",
    """CREATE TRIGGER trg_llm_secret_audit_append_only BEFORE UPDATE OR DELETE ON llm_secret_audit
       FOR EACH ROW EXECUTE FUNCTION llm_secret_audit_append_only()""",
    # secrets: no DELETE (history stays), a revoked / deleted version is terminal, erased ciphertext can never come back
    """CREATE OR REPLACE FUNCTION llm_secrets_guard() RETURNS trigger AS $$
       BEGIN
         IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'llm_secrets rows are never deleted'; END IF;
         IF OLD.status IN ('REVOKED','DELETED') AND NEW.status <> OLD.status THEN RAISE EXCEPTION 'revoked/deleted secret versions are terminal'; END IF;
         IF OLD.encrypted_value IS NULL AND NEW.encrypted_value IS NOT NULL THEN RAISE EXCEPTION 'erased ciphertext cannot be restored'; END IF;
         IF NEW.slot <> OLD.slot OR NEW.version <> OLD.version OR NEW.secret_id <> OLD.secret_id THEN RAISE EXCEPTION 'identity columns are immutable'; END IF;
         RETURN NEW;
       END; $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS trg_llm_secrets_guard ON llm_secrets",
    """CREATE TRIGGER trg_llm_secrets_guard BEFORE UPDATE OR DELETE ON llm_secrets FOR EACH ROW EXECUTE FUNCTION llm_secrets_guard()""",
)
ROLLBACK_DDL = ("DROP TRIGGER IF EXISTS trg_llm_secrets_guard ON llm_secrets", "DROP FUNCTION IF EXISTS llm_secrets_guard()",
                "DROP TRIGGER IF EXISTS trg_llm_secret_audit_append_only ON llm_secret_audit", "DROP FUNCTION IF EXISTS llm_secret_audit_append_only()",
                "DROP TABLE IF EXISTS llm_secret_audit", "DROP TABLE IF EXISTS llm_secrets")
