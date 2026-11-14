"""Lead Journey service: shapes real stored data for the panel. No estimates: every value is either a stored fact or null.
No LLM, network or randomness; nothing here is reachable from the bot's reply path."""
from datetime import datetime, timezone
from typing import Callable

from copilot_contracts.stages import Stage
from copilot_leads.contract import AXIS, CONTRACT_VERSION, SEPARATE, LeadError, mask_chat_id, parse_chat_id
from copilot_leads.repository import LeadRepository
from copilot_leads.stage_override import effective_stage, plan_move

MIN_SERIES_POINTS = 2


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _iso(v):
    if v is None:
        return None
    return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).isoformat()


def _signals(snapshot) -> dict:
    snap = snapshot if isinstance(snapshot, dict) else {}
    intent = snap.get("intent")
    return {"intent": intent if isinstance(intent, str) and intent else None, "sentiment": _num(snap.get("sentiment")),
            "trust": _num(snap.get("trust")), "messages_count": _num(snap.get("messages_count"))}


def _blocker(deposit_path):
    b = deposit_path.get("blocker") if isinstance(deposit_path, dict) else None
    return b if isinstance(b, str) and b else None


def _card(u, snap, blocker_path, ovr) -> dict:
    stage, origin = effective_stage(u.get("stage"), ovr)
    sig = _signals(snap["client_state_snapshot"] if snap else None)
    return {"chat_id": str(u["id"]), "label": mask_chat_id(u["id"]), "stage": stage.value, "stage_origin": origin,
            "production_stage_raw": u.get("stage"), "intent": sig["intent"], "sentiment": sig["sentiment"], "trust": sig["trust"],
            "blocker": _blocker(blocker_path), "last_activity": _iso(u.get("last_message_at")), "blocked": bool(u.get("blocked")),
            "manual_mode": bool(u.get("manual_mode")), "signals_at": _iso(snap["dec_at"]) if snap else None}


def _series(rows) -> dict:
    pts = []
    for r in reversed(rows):                                  # oldest -> newest
        sig = _signals(r["client_state_snapshot"])
        if sig["sentiment"] is not None or sig["messages_count"] is not None:
            pts.append({"at": _iso(r["created_at"]), "sentiment": sig["sentiment"], "messages_count": sig["messages_count"]})
    ok = sum(1 for p in pts if p["sentiment"] is not None) >= MIN_SERIES_POINTS
    return {"status": "OK" if ok else "INSUFFICIENT_DATA", "points": pts if ok else []}


class LeadService:
    def __init__(self, session_factory, clock: Callable[[], datetime] | None = None):
        self.repo = LeadRepository(session_factory)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def journey(self, limit: int = 100, offset: int = 0) -> dict:
        d = await self.repo.page(limit, offset)
        cards = [_card(u, d["snapshots"].get(u["id"]), d["blockers"].get(u["id"]), d["overrides"].get(u["id"])) for u in d["users"]]
        return {"contract_version": CONTRACT_VERSION, "axis": [s.value for s in AXIS], "separate": [s.value for s in SEPARATE],
                "total": d["total"], "limit": d["limit"], "offset": d["offset"], "leads": cards,
                "override_storage": "ready" if await self.repo.storage_ready() else "not_migrated"}

    async def detail(self, chat_id) -> dict:
        cid = parse_chat_id(chat_id)
        d = await self.repo.detail(cid)
        if not d:
            raise LeadError("lead_not_found")
        last = d["overrides"][0] if d["overrides"] else None
        snap = {"client_state_snapshot": d["series"][0]["client_state_snapshot"], "dec_at": d["series"][0]["created_at"]} if d["series"] else None
        card = _card(d["user"], snap, d["blocker"], last)
        return {"lead": card, "series": _series(d["series"]),
                "boundaries": [{"decision_id": b["decision_id"], "status": b["status"], "sent_at": _iso(b["sent_at"])} for b in d["boundaries"]],
                "outcomes": [{"outcome_type": o["outcome_type"], "source": o["source"], "decision_id": o["decision_id"], "observed_at": _iso(o["observed_at"])}
                             for o in d["outcomes"]],
                "decisions": [{"decision_id": r["decision_id"], "created_at": _iso(r["created_at"])} for r in d["series"][:5]],
                "overrides": [{"override_id": o["override_id"], "previous_stage": o["previous_stage"], "new_stage": o["new_stage"], "actor": o["actor"],
                               "created_at": _iso(o["created_at"])} for o in d["overrides"]]}

    async def move(self, chat_id, from_stage, to_stage, actor: str) -> dict:
        cid = parse_chat_id(chat_id)
        now = self._clock()
        ov = await self.repo.append(cid, lambda user, last: plan_move(user, last, from_stage, to_stage, actor, now))
        return ov.to_dict()
