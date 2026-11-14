"""Learning tables: OWN MetaData in the existing database (no new DB). Lazy CREATE TABLE IF NOT EXISTS, like the shadow store.
They reference shadow decisions by decision_id and never copy client state / outcome / reward beyond the versioned label that an
example needs to be reproducible. No raw dialogue text is stored anywhere here."""
from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, Integer, MetaData, String, Table, func, select, update)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

metadata = MetaData()
_J = JSON().with_variant(JSONB(), "postgresql")

critiques = Table(
    "learning_critiques", metadata,
    Column("decision_id", String(40), primary_key=True), Column("critic_version", String(24), primary_key=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("stage", String(24)), Column("reply_class", String(24)), Column("state_ref", String(32)),
    Column("scores", _J, nullable=False), Column("reasons", _J, nullable=False), Column("confidence", Float, nullable=False),
    Column("usable", Boolean, nullable=False), Column("response_ref", _J), Column("response_features", _J),
    Column("candidate", _J))                                  # shadow artifact: template text only, never sent

examples = Table(
    "learning_examples", metadata,
    Column("example_id", String(24), primary_key=True), Column("decision_id", String(40), nullable=False),
    Column("example_version", String(16), nullable=False), Column("critic_version", String(24), nullable=False),
    Column("state_signature", String(80)), Column("stage", String(24)), Column("reply_class", String(24)),
    Column("reasons", _J, nullable=False), Column("critic_overall", Integer), Column("usable", Boolean, nullable=False),
    Column("pattern_id", String(24)), Column("pattern_version", String(16)),
    Column("quality", String(16), nullable=False),            # unevaluated | success | neutral | failure | unreliable
    Column("reward", Float), Column("reward_version", String(32)), Column("reward_basis", String(12)),
    Column("history", _J, nullable=False), Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("evaluated_at", DateTime(timezone=True)))

patterns = Table(
    "learning_patterns", metadata,
    Column("pattern_id", String(24), primary_key=True), Column("pattern_version", String(16), nullable=False),
    Column("signature", _J, nullable=False), Column("signature_text", String(200), nullable=False),
    Column("sample_count", Integer, nullable=False), Column("success", Integer, nullable=False),
    Column("neutral", Integer, nullable=False), Column("failure", Integer, nullable=False),
    Column("unevaluated", Integer, nullable=False), Column("confidence", Float, nullable=False),
    Column("first_seen", DateTime(timezone=True)), Column("last_seen", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False))

improvements = Table(
    "learning_improvements", metadata,
    Column("improvement_id", String(24), primary_key=True), Column("improvement_version", String(24), nullable=False),
    Column("learner_version", String(16), nullable=False), Column("pattern_id", String(24), nullable=False),
    Column("problem", String(64), nullable=False), Column("observed_pattern", _J, nullable=False),
    Column("evidence_count", Integer, nullable=False), Column("current_behavior", String(240)),
    Column("candidate_change", String(240)), Column("expected_effect", String(240)), Column("confidence", Float, nullable=False),
    Column("supporting_examples", _J, nullable=False), Column("contraindications", _J, nullable=False),
    Column("state", String(20), nullable=False), Column("history", _J, nullable=False), Column("offline_eval", _J),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False))

_ready: set = set()


async def ensure_tables(session_factory) -> None:
    key = id(session_factory)
    if key in _ready:
        return
    async with session_factory() as s:
        conn = await s.connection()
        await conn.run_sync(metadata.create_all)
        await s.commit()
    _ready.add(key)


def _insert(s, table):
    dialect = s.bind.dialect.name if s.bind is not None else "postgresql"
    return (sqlite_insert if dialect == "sqlite" else pg_insert)(table)


async def save_critique(session_factory, c: dict) -> bool:
    """Idempotent on (decision_id, critic_version). True = inserted."""
    await ensure_tables(session_factory)
    row = {"decision_id": c["decision_id"], "critic_version": c["critic_version"], "stage": c.get("stage"),
           "reply_class": c.get("reply_class"), "state_ref": c.get("state_ref"), "scores": c["scores"], "reasons": c["reasons"],
           "confidence": c["confidence"], "usable": bool(c["usable"]), "response_ref": c.get("response_ref"),
           "response_features": c.get("response_features"), "candidate": c.get("candidate_better_response")}
    async with session_factory() as s:
        res = await s.execute(_insert(s, critiques).values(**row).on_conflict_do_nothing())
        await s.commit()
        return bool(res.rowcount)
