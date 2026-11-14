"""Uplift persistence schema: three append-only tables, own MetaData, schema only (DDL = migrate.py).
  uplift_shadow            one Shadow Uplift record per decision (UNIQUE decision_id)
  uplift_evaluations       stored offline evaluations (snapshots)
  uplift_activation_events the activation controller's append-only history (the state is the replay of these events)
`payload` is the contract's own to_dict(); indexed columns are verified against it on every read. Database triggers reject UPDATE and DELETE."""
from sqlalchemy import JSON, BigInteger, Column, DateTime, Float, Index, MetaData, String, Table, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
_J = JSON().with_variant(JSONB(), "postgresql")

uplift_shadow = Table(
    "uplift_shadow", metadata,
    Column("shadow_id", String(40), primary_key=True),
    Column("decision_id", String(40), nullable=False),
    Column("chat_id", BigInteger, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
    Column("policy_version", String(64), nullable=False),
    Column("data_quality", String(32), nullable=False),
    Column("contract_version", String(40), nullable=False),
    Column("payload", _J, nullable=False),
    UniqueConstraint("decision_id", name="uq_uplift_shadow_decision_id"),
)
uplift_evaluations = Table(
    "uplift_evaluations", metadata,
    Column("evaluation_id", String(40), primary_key=True),
    Column("evaluation_timestamp", DateTime(timezone=True), nullable=False),
    Column("policy_version", String(64), nullable=False),
    Column("data_quality", String(32), nullable=False),
    Column("dataset_version", String(64), nullable=False),
    Column("contract_version", String(40), nullable=False),
    Column("payload", _J, nullable=False),
    Index("ix_uplift_eval_ts", "evaluation_timestamp"),
)
uplift_activation_events = Table(
    "uplift_activation_events", metadata,
    Column("event_id", String(40), primary_key=True),
    Column("previous_state", String(16), nullable=False),
    Column("new_state", String(16), nullable=False),
    Column("changed_by", String(500), nullable=False),
    Column("changed_at", DateTime(timezone=True), nullable=False),
    Column("policy_version", String(64), nullable=False),
    Column("contract_version", String(40), nullable=False),
    Column("payload", _J, nullable=False),
    Index("ix_uplift_act_changed_at", "changed_at"),
)
uplift_assignments = Table(
    "uplift_assignments", metadata,
    Column("assignment_id", String(40), primary_key=True),
    Column("chat_id", BigInteger, nullable=False),
    Column("arm", String(16), nullable=False),
    Column("assignment_probability", Float, nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=False),
    Column("context", String(64), nullable=False),
    Column("contract_version", String(40), nullable=False),
    Column("payload", _J, nullable=False),
    UniqueConstraint("chat_id", name="uq_uplift_assignments_chat_id"),
    Index("ix_uplift_assignments_decided_at", "decided_at"),
)
TABLES = ("uplift_shadow", "uplift_evaluations", "uplift_activation_events", "uplift_assignments")


def _trigger_ddl(t: str):
    return (f"""CREATE OR REPLACE FUNCTION {t}_append_only() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION '{t} is immutable'; END; $$ LANGUAGE plpgsql""",
            f"DROP TRIGGER IF EXISTS trg_{t}_append_only ON {t}",
            f"""CREATE TRIGGER trg_{t}_append_only BEFORE UPDATE OR DELETE ON {t}
       FOR EACH ROW EXECUTE FUNCTION {t}_append_only()""")


APPEND_ONLY_DDL = tuple(d for t in TABLES for d in _trigger_ddl(t))


def _sqlite_trigger_ddl(t: str):
    return (
        f"""CREATE TRIGGER IF NOT EXISTS trg_{t}_no_update BEFORE UPDATE ON {t}
        BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END""",
        f"""CREATE TRIGGER IF NOT EXISTS trg_{t}_no_delete BEFORE DELETE ON {t}
        BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END""",
    )


SQLITE_APPEND_ONLY_DDL = tuple(d for t in TABLES for d in _sqlite_trigger_ddl(t))
ROLLBACK_DDL = tuple(d for t in TABLES for d in (f"DROP TRIGGER IF EXISTS trg_{t}_append_only ON {t}",
                                                 f"DROP FUNCTION IF EXISTS {t}_append_only()", f"DROP TABLE IF EXISTS {t}"))
