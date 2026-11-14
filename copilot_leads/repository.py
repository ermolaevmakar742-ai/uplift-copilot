"""Bounded, N+1-free reads (a fixed number of queries per call, independent of the page size) and the append-only override write.
Existing tables are declared here read-only with their own MetaData (never created / altered)."""
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, Column, DateTime, MetaData, String, Table, and_, func, insert, select
from sqlalchemy.exc import DBAPIError

from copilot_decisions.store import production_decisions as decisions_t
from copilot_leads import store
from copilot_leads.contract import LeadError, StageOverride
from copilot_outcomes.store import outcomes as outcomes_t

_ro = MetaData()
users_t = Table("users", _ro, Column("id", BigInteger, primary_key=True), Column("stage", String), Column("regime", String),
                Column("blocked", Boolean), Column("manual_mode", Boolean), Column("created_at", DateTime(timezone=True)),
                Column("last_message_at", DateTime(timezone=True)))
shadow_t = Table("shadow_decisions", _ro, Column("id", BigInteger, primary_key=True), Column("chat_id", BigInteger), Column("created_at", DateTime(timezone=True)),
                 Column("deposit_path", JSON), Column("momentum", JSON))
boundaries_t = Table("response_boundaries", _ro, Column("response_boundary_id", String, primary_key=True), Column("decision_id", String),
                     Column("chat_id", BigInteger), Column("sent_at", DateTime(timezone=True)), Column("status", String))
overrides_t = store.lead_stage_overrides

MAX_PAGE, MAX_SERIES, MAX_RECENT = 200, 30, 5


def _latest_per_chat(table, chat_ids, order_col, *cols):
    """One query: the newest row per chat among `chat_ids` (window function; portable PostgreSQL / SQLite)."""
    rn = func.row_number().over(partition_by=table.c.chat_id, order_by=order_col.desc()).label("rn")
    sub = select(table.c.chat_id, *cols, rn).where(table.c.chat_id.in_(chat_ids)).subquery()
    return select(sub).where(sub.c.rn == 1)


def _utc(v):
    from datetime import timezone
    return v if v is None or v.tzinfo else v.replace(tzinfo=timezone.utc)


class LeadRepository:
    def __init__(self, session_factory):
        self._sf = session_factory

    async def page(self, limit: int, offset: int) -> dict:
        limit = max(1, min(int(limit), MAX_PAGE))
        offset = max(0, int(offset))
        async with self._sf() as s:
            total = (await s.execute(select(func.count()).select_from(users_t))).scalar() or 0
            users = (await s.execute(select(users_t).order_by(users_t.c.last_message_at.desc().nullslast(), users_t.c.id).limit(limit).offset(offset))).mappings().all()
            ids = [u["id"] for u in users]
            snap, blockers, ovr = {}, {}, {}
            if ids:
                snap = {r["chat_id"]: r for r in (await s.execute(_latest_per_chat(decisions_t, ids, decisions_t.c.created_at,
                                                                                    decisions_t.c.client_state_snapshot, decisions_t.c.created_at.label("dec_at")))).mappings()}
                ovr = await self._latest_overrides(s, ids)
                blockers = await self._blockers(s, ids)
        return {"total": total, "limit": limit, "offset": offset, "users": [dict(u) for u in users], "snapshots": snap, "blockers": blockers, "overrides": ovr}

    async def _latest_overrides(self, s, ids) -> dict:
        try:
            q = _latest_per_chat(overrides_t, ids, overrides_t.c.created_at, overrides_t.c.override_id, overrides_t.c.new_stage, overrides_t.c.actor,
                                 overrides_t.c.created_at.label("ov_at"))
            return {r["chat_id"]: dict(r) for r in (await s.execute(q)).mappings()}
        except DBAPIError:
            await s.rollback()
            return {}                                                  # table not migrated: the board still works without overrides

    async def _blockers(self, s, ids) -> dict:
        try:
            q = _latest_per_chat(shadow_t, ids, shadow_t.c.created_at, shadow_t.c.deposit_path)
            return {r["chat_id"]: r["deposit_path"] for r in (await s.execute(q)).mappings()}
        except DBAPIError:
            await s.rollback()
            return {}

    async def user(self, chat_id: int):
        async with self._sf() as s:
            r = (await s.execute(select(users_t).where(users_t.c.id == chat_id))).mappings().first()
            return dict(r) if r else None

    async def storage_ready(self) -> bool:
        try:
            async with self._sf() as s:
                await s.execute(select(overrides_t.c.override_id).limit(1))
            return True
        except DBAPIError:
            return False

    async def detail(self, chat_id: int) -> dict:
        async with self._sf() as s:
            user = (await s.execute(select(users_t).where(users_t.c.id == chat_id))).mappings().first()
            if not user:
                return {}
            def lim(t, col, n, *where):
                return select(t).where(t.c.chat_id == chat_id, *where).order_by(col.desc()).limit(n)
            series = (await s.execute(select(decisions_t.c.decision_id, decisions_t.c.created_at, decisions_t.c.client_state_snapshot)
                                      .where(decisions_t.c.chat_id == chat_id).order_by(decisions_t.c.created_at.desc()).limit(MAX_SERIES))).mappings().all()
            boundaries = (await s.execute(lim(boundaries_t, boundaries_t.c.sent_at, MAX_RECENT))).mappings().all()
            outcomes = (await s.execute(lim(outcomes_t, outcomes_t.c.observed_at, MAX_RECENT))).mappings().all()
            blocker = (await self._blockers(s, [chat_id])).get(chat_id)
            try:
                history = (await s.execute(lim(overrides_t, overrides_t.c.created_at, 20))).mappings().all()
            except DBAPIError:
                await s.rollback()
                history = []
        return {"user": dict(user), "series": [dict(r) for r in series], "boundaries": [dict(r) for r in boundaries],
                "outcomes": [dict(r) for r in outcomes], "blocker": blocker, "overrides": [dict(r) for r in history]}

    async def last_override(self, session, chat_id: int):
        r = (await session.execute(select(overrides_t).where(overrides_t.c.chat_id == chat_id).order_by(overrides_t.c.created_at.desc(),
                                                                                                     overrides_t.c.override_id.desc()).limit(1))).mappings().first()
        return dict(r) if r else None

    async def append(self, chat_id: int, make) -> StageOverride:
        """One transaction: re-check that the lead exists, derive the CURRENT effective stage and the previous override, and append.
        `make(user_row, last_override_row)` returns the StageOverride (or raises LeadError)."""
        async with self._sf() as s:
            user = (await s.execute(select(users_t).where(users_t.c.id == chat_id))).mappings().first()
            if not user:
                raise LeadError("lead_not_found")
            try:
                last = await self.last_override(s, chat_id)
            except DBAPIError:
                await s.rollback()
                raise LeadError("override_storage_unavailable")
            ov = make(dict(user), last)
            try:
                await s.execute(insert(overrides_t).values(**{**ov.to_dict(), "created_at": ov.created_at}))
                await s.commit()
            except DBAPIError:
                await s.rollback()
                raise LeadError("override_storage_unavailable")
            return ov
