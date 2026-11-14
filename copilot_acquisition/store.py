"""Acquisition tables (own MetaData, same DB) + the click / bind logic.

Reuses `attribution_clicks` (existing click registry: raw INSERT/UPDATE, never ORM-imported from main) and the `users.attribution_*`
columns. New tables hold NO Telegram id: the lead is referenced by the existing opaque users.client_id (`lead_ref`).
First touch is set once and never overwritten; last touch is tracked separately in acquisition_leads."""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import (Column, DateTime, Integer, MetaData, String, Table, func, select, text, update)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from copilot_acquisition import contract as C

metadata = MetaData()
campaigns = Table(
    "acquisition_campaigns", metadata,
    Column("campaign_id", String(16), primary_key=True), Column("registry_version", String(24), nullable=False),
    Column("source", String(64), nullable=False), Column("medium", String(64), nullable=False),
    Column("campaign_name", String(64), nullable=False), Column("creative_id", String(64), nullable=False),
    Column("destination", String(40), nullable=False), Column("token", String(16), nullable=False, unique=True),
    Column("status", String(12), nullable=False), Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False))
clicks = Table(
    "acquisition_clicks", metadata,
    Column("click_id", String(40), primary_key=True), Column("campaign_id", String(16), nullable=False, index=True),
    Column("token_hash", String(64), nullable=False, unique=True), Column("clicked_at", DateTime(timezone=True), nullable=False),
    Column("destination", String(40), nullable=False), Column("status", String(20), nullable=False, index=True),
    Column("handoff_at", DateTime(timezone=True)), Column("expires_at", DateTime(timezone=True)),
    Column("bound_at", DateTime(timezone=True)), Column("lead_ref", String(64), index=True))   # users.client_id, never a Telegram id
leads = Table(
    "acquisition_leads", metadata,
    Column("lead_ref", String(64), primary_key=True),
    Column("first_click_id", String(40), nullable=False), Column("first_campaign_id", String(16), nullable=False),
    Column("first_touch_at", DateTime(timezone=True), nullable=False),
    Column("last_click_id", String(40), nullable=False), Column("last_campaign_id", String(16), nullable=False),
    Column("last_touch_at", DateTime(timezone=True), nullable=False), Column("touches", Integer, nullable=False))

_ready: set = set()


def _aware(ts):
    return ts if isinstance(ts, datetime) and ts.tzinfo else (ts.replace(tzinfo=timezone.utc) if isinstance(ts, datetime) else None)


async def ensure_tables(sf) -> None:
    if id(sf) in _ready:
        return
    async with sf() as s:
        conn = await s.connection()
        await conn.run_sync(metadata.create_all)
        await s.commit()
    _ready.add(id(sf))


def _ins(s, table):
    return (sqlite_insert if (s.bind is not None and s.bind.dialect.name == "sqlite") else pg_insert)(table)


async def create_campaign(sf, *, source, medium, campaign_name, creative_id, destination, status="active") -> dict:
    """Registry entry (the source of truth for campaign metadata). Labels are validated plain strings (no PII), destination must be
    an allowlisted Telegram username. Raises ValueError otherwise."""
    for k, v in (("source", source), ("medium", medium), ("campaign_name", campaign_name), ("creative_id", creative_id)):
        if not C.valid_label(v):
            raise ValueError(f"invalid {k}")
    if not C.valid_destination(destination):
        raise ValueError("destination is not an allowlisted Telegram username")
    if status not in C.CAMPAIGN_STATUSES:
        raise ValueError("invalid status")
    await ensure_tables(sf)
    row = {"campaign_id": C.new_campaign_id(), "registry_version": C.VERSION, "source": source.strip().lower(),
           "medium": medium.strip().lower(), "campaign_name": campaign_name.strip(), "creative_id": creative_id.strip(),
           "destination": destination, "token": C.new_token(), "status": status}
    async with sf() as s:
        await s.execute(campaigns.insert().values(**row))
        await s.commit()
    return row


async def get_campaign_by_token(sf, token):
    if not (isinstance(token, str) and C.TOKEN_RE.match(token)):
        return None
    await ensure_tables(sf)
    async with sf() as s:
        r = (await s.execute(select(campaigns).where(campaigns.c.token == token))).mappings().first()
    return dict(r) if r else None


async def set_campaign_status(sf, campaign_id: str, status: str) -> bool:
    if status not in C.CAMPAIGN_STATUSES:
        raise ValueError("invalid status")
    await ensure_tables(sf)
    async with sf() as s:
        res = await s.execute(update(campaigns).where(campaigns.c.campaign_id == campaign_id).values(status=status))
        await s.commit()
        return bool(res.rowcount)


async def issue_click(sf, campaign: dict, now: datetime | None = None) -> dict:
    """New unique click for an ACTIVE campaign: row in the EXISTING attribution_clicks (source / campaign=campaign_id / creative) +
    side row (token hash only). Returns {click_id, token, url}. The plain token exists only in the returned URL."""
    now = _aware(now) or datetime.now(timezone.utc)
    if campaign.get("status") != "active" or not C.valid_destination(campaign.get("destination")):
        raise ValueError("campaign not redirectable")
    await ensure_tables(sf)
    click_id, token = C.new_click_id(), C.new_token()
    async with sf() as s:
        await s.execute(text("INSERT INTO attribution_clicks (click_id, source, campaign, placement, creative, revenue) "
                             "VALUES (:c, :s, :g, NULL, :cr, 0)"),
                        {"c": click_id, "s": campaign["source"], "g": campaign["campaign_id"], "cr": campaign["creative_id"]})
        await s.execute(clicks.insert().values(
            click_id=click_id, campaign_id=campaign["campaign_id"], token_hash=C.hash_token(token), clicked_at=now,
            destination=campaign["destination"], status=C.HANDOFF_GENERATED, handoff_at=now,
            expires_at=now + timedelta(seconds=C.token_ttl_seconds())))
        await s.commit()
    return {"click_id": click_id, "token": token, "url": C.handoff_url(campaign["destination"], token)}


async def bind_token(sf, token: str, chat_id: int, now: datetime | None = None, *, retries: int = 6, wait_s: float = 0.5) -> str:
    """Bind a presented token to the sending Telegram user. Returns one of:
    bound | already_bound | token_reused | expired | invalid_token | lead_not_ready | not_bindable.
    Idempotent (the same lead presenting the same token again is a no-op); a token binds ONE lead only; first touch is never
    overwritten; the new tables only ever see the opaque users.client_id."""
    now = _aware(now) or datetime.now(timezone.utc)
    if not (isinstance(token, str) and C.TOKEN_RE.match(token)):
        return "invalid_token"
    await ensure_tables(sf)
    th = C.hash_token(token)
    async with sf() as s:
        click = (await s.execute(select(clicks).where(clicks.c.token_hash == th))).mappings().first()
    if click is None:
        return "invalid_token"
    lead_ref = None
    for i in range(retries):                       # the users row / client_id is created by the normal message flow
        async with sf() as s:
            r = (await s.execute(text("SELECT client_id FROM users WHERE id = :i"), {"i": chat_id})).first()
        if r and r[0]:
            lead_ref = r[0]
            break
        if i + 1 < retries:
            await asyncio.sleep(wait_s)
    if lead_ref is None:
        return "lead_not_ready"
    if click["status"] in (C.BOUND, C.CONVERTED):
        return "already_bound" if click["lead_ref"] == lead_ref else "token_reused"
    if click["status"] != C.HANDOFF_GENERATED:
        return "not_bindable"
    if _aware(click["expires_at"]) is not None and now > _aware(click["expires_at"]):
        async with sf() as s:
            await s.execute(update(clicks).where(clicks.c.click_id == click["click_id"], clicks.c.status == C.HANDOFF_GENERATED)
                            .values(status=C.EXPIRED))
            await s.commit()
        return "expired"
    async with sf() as s:
        cas = await s.execute(update(clicks).where(clicks.c.click_id == click["click_id"], clicks.c.status == C.HANDOFF_GENERATED)
                              .values(status=C.BOUND, bound_at=now, lead_ref=lead_ref))
        if not cas.rowcount:
            await s.rollback()
            return "not_bindable"                   # lost a race: someone else bound it
        ids = {"c": click["click_id"], "u": chat_id, "t": now}
        await s.execute(text("UPDATE attribution_clicks SET user_id = :u WHERE click_id = :c AND user_id IS NULL"), ids)
        await s.execute(text("UPDATE attribution_clicks SET registered_at = :t WHERE click_id = :c AND registered_at IS NULL"), ids)
        meta = (await s.execute(text("SELECT source, campaign, placement, creative FROM attribution_clicks WHERE click_id = :c"), ids)).first()
        if meta:                                    # legacy first-touch fields: only when still empty (never overwritten)
            await s.execute(text("UPDATE users SET attribution_click_id = :c, attribution_source = :s, attribution_campaign = :g, "
                                 "attribution_placement = :p, attribution_creative = :cr WHERE id = :u AND attribution_click_id IS NULL"),
                            {"c": click["click_id"], "s": meta[0], "g": meta[1], "p": meta[2], "cr": meta[3], "u": chat_id})
        ins = _ins(s, leads).values(lead_ref=lead_ref, first_click_id=click["click_id"], first_campaign_id=click["campaign_id"],
                                    first_touch_at=now, last_click_id=click["click_id"], last_campaign_id=click["campaign_id"],
                                    last_touch_at=now, touches=1).on_conflict_do_nothing(index_elements=["lead_ref"])
        if not (await s.execute(ins)).rowcount:     # lead already exists (also when two tokens bind concurrently): first touch stays, last moves
            await s.execute(update(leads).where(leads.c.lead_ref == lead_ref).values(
                last_click_id=click["click_id"], last_campaign_id=click["campaign_id"], last_touch_at=now, touches=leads.c.touches + 1))
        await s.commit()
    return "bound"


async def mark_converted(sf, click_id: str) -> bool:
    """BOUND -> CONVERTED (compare-and-set). Not called automatically: conversion is read from outcomes by the research join."""
    await ensure_tables(sf)
    async with sf() as s:
        res = await s.execute(update(clicks).where(clicks.c.click_id == click_id, clicks.c.status == C.BOUND).values(status=C.CONVERTED))
        await s.commit()
        return bool(res.rowcount)


async def expire_unbound(sf, now: datetime | None = None) -> int:
    """Handoffs never completed after the TTL -> UNBOUND (a click is not a lead)."""
    now = _aware(now) or datetime.now(timezone.utc)
    await ensure_tables(sf)
    async with sf() as s:
        res = await s.execute(update(clicks).where(clicks.c.status == C.HANDOFF_GENERATED, clicks.c.expires_at < now).values(status=C.UNBOUND))
        await s.commit()
        return res.rowcount or 0
