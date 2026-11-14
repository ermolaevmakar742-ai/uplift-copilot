"""Persistence for shadow decisions. Own table, own MetaData: does not touch
production tables or main.Base. Idempotent on (chat_id, message_id)."""
import asyncio
import logging

from sqlalchemy import (JSON, BigInteger, Boolean, Column, DateTime, Float, Integer,
                        MetaData, String, Table, func, select, update)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

logger = logging.getLogger("copilot_shadow")

metadata = MetaData()
_J = JSON().with_variant(JSONB(), "postgresql")

shadow_decisions = Table(
    "shadow_decisions", metadata,
    Column("id", BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True),
    Column("decision_id", String(40), nullable=False, unique=True),
    Column("chat_id", BigInteger, nullable=False),
    Column("message_id", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("state_snapshot", _J, nullable=False),
    Column("candidate_actions", _J, nullable=False),
    Column("chosen_action", String(32), nullable=False),
    Column("chosen_rule", String(128)),
    Column("wait_baseline", _J, nullable=False),
    Column("decision_status", String(24), nullable=False),
    Column("shadow_mode", Boolean, nullable=False),
    Column("model_name", String(64), nullable=False),
    Column("model_version", String(32), nullable=False),
    Column("analyzer_version", String(32), nullable=False),
    Column("code_version", String(64), nullable=False),
    Column("production_regime", String(16)),
    Column("production_action_proxy", String(32)),
    Column("reply_chars", Integer),
    # analytical shadow modules (nullable: older rows / failed analysis stay NULL)
    Column("contradiction", _J),
    Column("momentum", _J),
    Column("next_best_question", _J),
    Column("deposit_path", _J),
    # contract v1 (additive, nullable): recommendation -> production action -> outcome on one decision_id
    Column("client_id", String(24)),
    Column("session_id", String(24)),
    Column("decision_point", String(24)),
    Column("dedup_key", String(32), unique=True),
    Column("state_ref", String(32)),
    Column("canonical_state", _J),
    Column("response_policy", _J),
    Column("shadow_recommendation", String(32)),
    Column("shadow_confidence", Float),
    Column("production_action", String(32)),
    Column("production_response", String(16)),
    Column("outcome", String(48)),
    Column("outcome_timestamp", DateTime(timezone=True)),
    Column("contract_version", String(24)),
    # outcome lifecycle (additive): NULL/pending -> observed | expired | unknown (terminal)
    Column("outcome_status", String(16)),
    Column("outcome_ref", String(255)),            # outcomes.outcome_id of the observed event
    Column("outcome_attribution", String(32)),     # explicitly_linked | temporal_chat_fallback (not causal)
    Column("outcome_closed_at", DateTime(timezone=True)),
    # shadow observations v2 (additive, nullable; none of these feeds a production decision)
    Column("reply_classification", _J),
    Column("classifier_version", String(24)),
    Column("lead_score", Integer),
    Column("lead_score_detail", _J),
    Column("scorer_version", String(24)),
    # Shadow Store v2 / research fields (additive, nullable; research only, never read by a production decision)
    Column("feature_version", String(16)),
    Column("features_ref", String(24)),
    Column("available_actions", _J),
    Column("production_policy_version", String(48)),
    Column("production_propensity", Float),                 # NULL = unknown (production records none)
    Column("production_propensity_source", String(12)),     # known | unknown | surrogate
    Column("bandit_action", String(32)),
    Column("bandit_policy_version", String(32)),
    Column("bandit_propensity", Float),                     # the bandit's OWN probability, never the production one
    Column("uplift_score", Float),
    Column("uplift_version", String(24)),
    Column("reward", Float),
    Column("reward_version", String(32)),
    Column("reward_reason", String(64)),
    Column("reward_detail", _J),            # staged audit trail + policy params (reward.v1)
    Column("reward_corrections", _J),       # append-only list of delayed-event corrections; never edits `reward`
    Column("observation_window", Integer),
    Column("eligibility_flags", _J),
    # canonical production action (action_mapping.map_decision v2 over ledger metadata); production_action stays the raw label
    Column("canonical_action", String(32)),               # actions.v1 id only when status = MAPPED, else NULL
    Column("action_mapping_status", String(12)),          # MAPPED | AMBIGUOUS | UNMAPPED
    Column("action_mapping_version", String(24)),
    Column("action_mapping_reason", String(64)),
    # legacy uq (chat_id, message_id) is kept only on pre-existing tables; dedup_key supersedes it
)

_ready: set[int] = set()
ANALYSIS_COLUMNS = ("contradiction", "momentum", "next_best_question", "deposit_path")
CONTRACT_COLUMNS = ("client_id", "session_id", "decision_point", "dedup_key", "state_ref", "canonical_state",
                    "response_policy", "shadow_recommendation", "shadow_confidence", "production_action",
                    "production_response", "outcome", "outcome_timestamp", "contract_version",
                    "outcome_status", "outcome_ref", "outcome_attribution", "outcome_closed_at",
                    "reply_classification", "classifier_version", "lead_score", "lead_score_detail",
                    "scorer_version",
                    "feature_version", "features_ref", "available_actions", "production_policy_version",
                    "production_propensity", "production_propensity_source", "bandit_action",
                    "bandit_policy_version", "bandit_propensity", "uplift_score", "uplift_version", "reward",
                    "reward_version", "reward_reason", "observation_window", "eligibility_flags",
                    "reward_detail", "reward_corrections", "canonical_action", "action_mapping_status",
                    "action_mapping_version", "action_mapping_reason")


def _add_missing_columns(sync_conn) -> None:
    """Additive-only migration of OUR table (no production table is touched)."""
    from sqlalchemy import inspect, text
    have = {c["name"] for c in inspect(sync_conn).get_columns("shadow_decisions")}
    for name in ANALYSIS_COLUMNS + CONTRACT_COLUMNS:
        if name not in have:
            ddl = shadow_decisions.c[name].type.compile(dialect=sync_conn.dialect)
            sync_conn.execute(text(f'ALTER TABLE shadow_decisions ADD COLUMN {name} {ddl}'))
    if "dedup_key" not in have:
        sync_conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_dedup_key "
                               "ON shadow_decisions (dedup_key)"))


async def ensure_table(session_factory) -> None:
    key = id(session_factory)
    if key in _ready:
        return
    async with session_factory() as s:
        conn = await s.connection()
        await conn.run_sync(metadata.create_all)  # CREATE TABLE IF NOT EXISTS only
        await conn.run_sync(_add_missing_columns)
        await s.commit()
    _ready.add(key)


async def save_decision(session_factory, row: dict) -> bool:
    """Returns True if a new row was inserted, False if duplicate."""
    await ensure_table(session_factory)
    async with session_factory() as s:
        dialect = s.bind.dialect.name if s.bind is not None else "postgresql"
        ins = (sqlite_insert if dialect == "sqlite" else pg_insert)(shadow_decisions).values(**row)
        res = await s.execute(ins.on_conflict_do_nothing())   # any unique conflict: decision_id, dedup_key, legacy (chat,msg)
        await s.commit()
        return bool(res.rowcount)


async def link_production_action(session_factory, decision_id: str, action: str, responded: str = "RESPONDED") -> bool:
    """Attach the production action to its shadow decision. Set-once: an existing value is never overwritten."""
    await ensure_table(session_factory)
    t = shadow_decisions
    async with session_factory() as s:
        res = await s.execute(update(t).where(t.c.decision_id == decision_id, t.c.production_action.is_(None))
                              .values(production_action=action, production_response=responded))
        await s.commit()
        return bool(res.rowcount)


async def close_outcome(session_factory, decision_id: str, status: str, outcome: str | None = None,
                        outcome_timestamp=None, ref: str | None = None, attribution: str | None = None,
                        closed_at=None) -> bool:
    """Terminal lifecycle write. SET-ONCE and idempotent: only a decision that is still open (status NULL or
    'pending') and has no outcome is touched; anything already closed is never overwritten. True = written."""
    from datetime import datetime, timezone
    if status not in ("observed", "expired", "unknown"):
        raise ValueError("status must be observed|expired|unknown")
    if status == "observed" and not outcome:
        raise ValueError("observed requires an outcome")
    await ensure_table(session_factory)
    t = shadow_decisions
    vals = dict(outcome_status=status, outcome_closed_at=closed_at or datetime.now(timezone.utc),
                outcome_ref=ref, outcome_attribution=attribution)
    if status == "observed":
        vals.update(outcome=outcome[:48], outcome_timestamp=outcome_timestamp or vals["outcome_closed_at"])
    async with session_factory() as s:
        res = await s.execute(update(t).where(
            t.c.decision_id == decision_id, t.c.outcome.is_(None),
            (t.c.outcome_status.is_(None)) | (t.c.outcome_status == "pending")).values(**vals))
        await s.commit()
        return bool(res.rowcount)


async def record_outcome(session_factory, decision_id: str, outcome: str, outcome_timestamp=None) -> bool:
    """Attach an observed outcome to its shadow decision. Set-once (first outcome wins); no attribution claim."""
    return await close_outcome(session_factory, decision_id, "observed", outcome, outcome_timestamp)


async def set_reward(session_factory, decision_id: str, reward: float | None, reward_version: str,
                     reward_reason: str | None = None, detail: dict | None = None) -> bool:
    """Record the resolved reward (None = undefined) with its version and audit detail. SET-ONCE: a decision that already
    carries a reward_version is never overwritten; a new reward definition lives in a new version, not over old data."""
    if isinstance(reward, bool) or not (reward is None or (isinstance(reward, (int, float)) and -1.0 <= reward <= 1.0)):
        raise ValueError("reward must be None or within [-1, 1]")
    await ensure_table(session_factory)
    t = shadow_decisions
    async with session_factory() as s:
        res = await s.execute(update(t).where(t.c.decision_id == decision_id, t.c.reward_version.is_(None))
                              .values(reward=reward, reward_version=reward_version, reward_reason=reward_reason,
                                      reward_detail=detail))
        await s.commit()
        return bool(res.rowcount)


async def add_reward_correction(session_factory, decision_id: str, correction: dict) -> bool:
    """Append a delayed-event correction next to the stored reward. The original `reward` is never touched. Idempotent per
    event: a correction whose events were all applied before is skipped. False when nothing was appended."""
    await ensure_table(session_factory)
    t = shadow_decisions
    async with session_factory() as s:
        row = (await s.execute(select(t.c.reward_version, t.c.reward_corrections).where(t.c.decision_id == decision_id))).first()
        if row is None or row[0] is None:
            return False                                   # no stored reward to correct
        current = list(row[1] or [])
        done = {e for c in current for e in c.get("event_ids", ())}
        if not correction.get("event_ids") or set(correction["event_ids"]) <= done:
            return False
        res = await s.execute(update(t).where(t.c.decision_id == decision_id, t.c.reward_version.is_not(None))
                              .values(reward_corrections=current + [correction]))
        await s.commit()
        return bool(res.rowcount)
