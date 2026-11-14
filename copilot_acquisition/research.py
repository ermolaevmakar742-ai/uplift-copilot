"""READ-ONLY research join: click -> lead -> decision -> outcome -> reward. Pseudonymous output only: lead_ref (users.client_id),
decision ids, outcome status/type, reward values. Telegram ids and outcome ids (which embed them) are never returned.
AttributionClick.revenue is NOT used as a reward source: reward is shadow_decisions.reward (reward.v1) plus its append-only corrections."""
import hashlib
import json

from sqlalchemy import select, text

from copilot_acquisition.store import campaigns, clicks, ensure_tables, leads


def _json(v):
    """Raw text() queries return JSON columns as str on some drivers (asyncpg): decode defensively."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def effective_reward(reward, corrections):
    """(value, basis): the last delayed correction wins; the original reward is never rewritten."""
    corrections = _json(corrections)
    if isinstance(corrections, list) and corrections and isinstance(corrections[-1], dict) \
       and corrections[-1].get("corrected_reward") is not None:
        return corrections[-1]["corrected_reward"], "corrected"
    return reward, "original"


async def lead_chain(sf, click_id: str) -> dict | None:
    await ensure_tables(sf)
    async with sf() as s:
        c = (await s.execute(select(clicks).where(clicks.c.click_id == click_id))).mappings().first()
        if c is None:
            return None
        camp = (await s.execute(select(campaigns).where(campaigns.c.campaign_id == c["campaign_id"]))).mappings().first()
        out = {"click_id": c["click_id"], "campaign_id": c["campaign_id"], "source": camp and camp["source"], "medium": camp and camp["medium"],
               "creative_id": camp and camp["creative_id"], "status": c["status"], "lead_ref": c["lead_ref"], "decisions": []}
        if not c["lead_ref"]:
            return out
        lead = (await s.execute(select(leads).where(leads.c.lead_ref == c["lead_ref"]))).mappings().first()
        out["first_touch_click_id"], out["last_touch_click_id"] = lead and lead["first_click_id"], lead and lead["last_click_id"]
        rows = (await s.execute(text(
            "SELECT d.decision_id, d.outcome, d.outcome_status, d.outcome_ref, d.reward, d.reward_version, d.reward_corrections "
            "FROM shadow_decisions d JOIN users u ON u.id = d.chat_id WHERE u.client_id = :l ORDER BY d.id"), {"l": c["lead_ref"]})).mappings().all()
    for r in rows:
        val, basis = effective_reward(r["reward"], r["reward_corrections"])
        out["decisions"].append({"decision_id": r["decision_id"], "outcome": r["outcome"], "outcome_status": r["outcome_status"],
                                 "outcome_ref": hashlib.sha1(r["outcome_ref"].encode()).hexdigest()[:10] if r["outcome_ref"] else None,
                                 "reward": val, "reward_basis": basis, "reward_version": r["reward_version"]})
    return out


async def campaign_summary(sf) -> list:
    """Per campaign (FIRST-touch attribution): counts only; no ratios, no money. Raw data requirements for later metrics."""
    await ensure_tables(sf)
    async with sf() as s:
        camps = (await s.execute(select(campaigns))).mappings().all()
        out = []
        for cp in camps:
            cid = cp["campaign_id"]
            n_clicks = (await s.execute(text("SELECT count(*) FROM acquisition_clicks WHERE campaign_id = :c"), {"c": cid})).scalar()
            n_bound = (await s.execute(text("SELECT count(*) FROM acquisition_clicks WHERE campaign_id = :c AND status IN ('BOUND','CONVERTED')"), {"c": cid})).scalar()
            n_leads = (await s.execute(text("SELECT count(*) FROM acquisition_leads WHERE first_campaign_id = :c"), {"c": cid})).scalar()
            rows = (await s.execute(text(
                "SELECT d.outcome, d.reward, d.reward_corrections FROM acquisition_leads l JOIN users u ON u.client_id = l.lead_ref "
                "JOIN shadow_decisions d ON d.chat_id = u.id WHERE l.first_campaign_id = :c"), {"c": cid})).all()
            outcomes: dict = {}
            rewarded, total = 0, 0.0
            for o, rw, corr in rows:
                outcomes[o or "none"] = outcomes.get(o or "none", 0) + 1
                v, _ = effective_reward(rw, corr)
                if v is not None:
                    rewarded += 1
                    total += v
            out.append({"campaign_id": cid, "source": cp["source"], "medium": cp["medium"], "creative_id": cp["creative_id"],
                        "clicks": n_clicks, "bound_clicks": n_bound, "first_touch_leads": n_leads, "decisions": len(rows),
                        "outcomes": outcomes, "rewarded_decisions": rewarded, "reward_sum": round(total, 6)})
    return out
