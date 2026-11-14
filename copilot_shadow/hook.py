"""Single integration entry point called from the live on_message.

Contract: schedule_shadow() is synchronous, never raises, never awaits, never
blocks. Everything touching live objects (user/client_state/history) happens
synchronously and only reads; the background task only persists plain data.
"""
import asyncio
import hashlib
import logging
import os
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from copilot_shadow.actions import REGIME_TO_ACTION
from copilot_shadow.analyzer import ANALYZER_VERSION, build_snapshot
from copilot_shadow.decide import decide
from copilot_shadow import arbiter, contract, lead_score, reply_class, response_policy, store
from copilot_shadow.analysis import analyze
from copilot_shadow import signals as S
from copilot_shadow.signals import Turn

logger = logging.getLogger("copilot_shadow")

TIMEOUT_S = 10.0
MAX_PENDING = 50
_SEEN_MAX = 2000

_tasks: set[asyncio.Task] = set()
_seen: "OrderedDict[tuple, None]" = OrderedDict()
_sem: asyncio.Semaphore | None = None


def _code_version() -> str:
    h = hashlib.sha1()
    for p in sorted(Path(__file__).parent.glob("*.py")):
        h.update(p.read_bytes())
    return "shadow-1.1.0+" + h.hexdigest()[:8]


CODE_VERSION = _code_version()


def shadow_enabled() -> bool:
    return os.getenv("COPILOT_SHADOW_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _build_row(chat_id, message_id, user, client_state, history, regime, reply_text, decision_id=None) -> dict:
    snap = build_snapshot(user, client_state, history, regime)
    d = decide(snap)
    proxy = REGIME_TO_ACTION.get((regime or "").lower())
    cid = contract.client_id(chat_id)
    canonical = arbiter.arbitrate_state(arbiter.evidence_from_client_state(user, client_state))
    return {
        # one decision_id per production decision: reuse the ledger's id when given
        "decision_id": decision_id if isinstance(decision_id, str) and decision_id else "shd_" + uuid.uuid4().hex,
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "state_snapshot": snap,
        "candidate_actions": d["candidate_actions"],
        "chosen_action": d["chosen_action"],
        "chosen_rule": d["chosen_rule"],
        "wait_baseline": d["wait_baseline"],
        "decision_status": d["decision_status"],
        "shadow_mode": d["shadow_mode"],
        "model_name": d["model_name"],
        "model_version": d["model_version"],
        "analyzer_version": ANALYZER_VERSION,
        "code_version": CODE_VERSION,
        "production_regime": regime,
        "production_action_proxy": proxy.value if proxy else None,
        "reply_chars": len(reply_text) if isinstance(reply_text, str) else None,
        # contract v1 (session_id / dedup_key are completed in _enrich once turns are known)
        "client_id": cid,
        "decision_point": contract.DECISION_POINT_REPLY,
        "state_ref": contract.state_ref(snap),
        "canonical_state": canonical,
        "shadow_recommendation": d["chosen_action"],
        "production_action": proxy.value if proxy else None,   # what production did (regime proxy)
        "production_response": "RESPONDED",                    # hook runs only after a reply was sent
        "contract_version": contract.CONTRACT_VERSION,
    }


def _aware(ts):
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    if isinstance(ts, datetime) and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts if isinstance(ts, datetime) else None


async def _load_turns(session_factory, chat_id, hist) -> list:
    """Turns up to (and including) the current user message. Prefers a read-only
    SELECT on dialogs (gives ids/timestamps); falls back to the in-memory history
    snapshot (no ids). Never writes."""
    hist_turns = [Turn(r, c) for r, c in hist if r in ("user", "assistant") and isinstance(c, str)]
    cur = next((t.text for t in reversed(hist_turns) if t.role == "user"), None)
    if cur is None:
        return hist_turns
    try:
        async with session_factory() as s:
            rows = (await s.execute(
                text("SELECT id, role, content, created_at FROM dialogs "
                     "WHERE user_id = :u ORDER BY id DESC LIMIT 30"), {"u": chat_id})).all()
    except Exception:
        logger.warning("SHADOW dialogs read failed chat=%s; using history snapshot", chat_id)
        return hist_turns
    rows = rows[::-1]
    cut = next((i for i in range(len(rows) - 1, -1, -1)
                if rows[i][1] == "user" and (rows[i][2] or "")[:4000] == cur[:4000]), None)
    if cut is None:
        return hist_turns
    return [Turn(r[1], r[2] or "", r[0], _aware(r[3])) for r in rows[:cut + 1]]


def _complete_contract(row: dict, turns, res: dict, ledger_meta=None, reply_text=None,
                       learning_out=None) -> None:
    """session_id + dedup_key + Response Policy shadow recommendation. Pure; never raises into _enrich's caller."""
    row["session_id"] = contract.session_id(row["chat_id"], turns)
    row["dedup_key"] = contract.dedup_key(row["client_id"], row["session_id"], row["decision_point"],
                                          row["message_id"])
    canon = row.get("canonical_state") or {}
    ctr = res.get("contradiction") or {}
    last_user = next((t for t in reversed(turns) if t.role == "user"), None)
    rc = reply_class.classify(last_user.text if last_user else "")      # text read transiently, never stored
    row["reply_classification"] = rc
    row["classifier_version"] = rc["classifier_version"]
    fs_user = [f for f in S.annotate(turns) if f.role == "user"]
    snap = row.get("state_snapshot") or {}
    ls = lead_score.score({
        "stage": snap.get("stage"), "intent": snap.get("intent"), "sentiment": snap.get("sentiment"),
        "reply_classification": rc, "momentum": res.get("momentum"), "risk": snap.get("risk"),
        "deposit_reported": any(f.deposit_done for f in fs_user),
        "payment_signal": bool(fs_user and (fs_user[-1].payment or fs_user[-1].pay_method)),
        "contradiction": res.get("contradiction"), "canonical_state": canon, "user_turns": len(fs_user)})
    row["lead_score"] = ls["lead_score"]
    row["lead_score_detail"] = {k: ls[k] for k in ("score_band", "score_components", "reason_codes")}
    row["scorer_version"] = ls["scorer_version"]
    sig = response_policy.signals_from_turns(S.annotate(turns))
    sig.update({
        "momentum": res.get("momentum"),
        "next_best_question": res.get("next_best_question"),
        "contradiction": bool(ctr.get("detected")) or any(c.get("status") == "unresolved" and c.get("contradiction")
                                                         for c in canon.values()),
        "stage": (row.get("state_snapshot") or {}).get("stage"),
        "risk": (row.get("state_snapshot") or {}).get("risk"),
        "manual_mode": (row.get("state_snapshot") or {}).get("manual_mode"),
    })
    try:                                       # Shadow Store v2 research fields (+ optional bandit shadow, flag-gated)
        from copilot_bandit.research import v2_fields
        row.update(v2_fields(decision_id=row["decision_id"], chat_id=row["chat_id"], snapshot=snap,
                             ledger_meta=ledger_meta, risk=snap.get("risk"),
                             manual_mode=snap.get("manual_mode")))
    except Exception:
        logger.exception("SHADOW research fields failed chat=%s", row.get("chat_id"))
    if learning_out is not None:               # Dialogue Learning (shadow only, flag-gated): transient text, no raw text kept
        try:
            from copilot_learning.contract import learning_enabled
            if learning_enabled():
                from copilot_learning.critic import critique
                prev = [t.text for t in turns if t.role == "assistant"]
                learning_out["critique"] = critique(
                    response_text=reply_text, client_text=last_user.text if last_user else None, prev_assistant=prev,
                    stage=snap.get("stage"), reply_classification=rc, next_best_question=res.get("next_best_question"),
                    state_ref=row.get("state_ref"), decision_id=row["decision_id"])
        except Exception:
            logger.exception("SHADOW learning critique failed chat=%s", row.get("chat_id"))
    pol = response_policy.recommend(sig)
    row["response_policy"] = pol
    row["shadow_confidence"] = pol["confidence"]


LEDGER_JOIN_ATTEMPTS, LEDGER_JOIN_WAIT_S = 3, 0.3


async def _ledger_meta(session_factory, decision_id):
    """Existing Decision Ledger row of this decision (regime / trigger / decision_path), joined on the SHARED decision_id.
    Read-only SELECT; None when the id is not a ledger id, the row is absent, or the table is unreadable (-> UNMAPPED)."""
    if not (isinstance(decision_id, str) and decision_id.startswith("pd_")):
        return None
    for i in range(LEDGER_JOIN_ATTEMPTS):               # the ledger row is written by a background task: allow a short lag
        try:
            async with session_factory() as s:
                r = (await s.execute(text('SELECT regime, "trigger", decision_path FROM production_decisions '
                                          'WHERE decision_id = :d'), {"d": decision_id})).first()
        except Exception:
            return None
        if r is not None:
            return {"regime": r[0], "trigger": r[1], "decision_path": r[2]}
        if i + 1 < LEDGER_JOIN_ATTEMPTS:
            await asyncio.sleep(LEDGER_JOIN_WAIT_S)
    return None


async def _enrich(session_factory, row: dict, hist, reply_text=None) -> dict:
    """Adds the four analytical modules to the row. Never raises. Returns shadow-side learning artifacts ({} normally)."""
    art: dict = {}
    try:
        t0 = time.perf_counter()
        turns = await _load_turns(session_factory, row["chat_id"], hist)
        res = analyze(turns, (row.get("state_snapshot") or {}).get("stage")).to_dict()
        meta = res.pop("meta")
        meta["analysis_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        row.update(res)
        try:
            _complete_contract(row, turns, res,
                               await _ledger_meta(session_factory, row["decision_id"]), reply_text, art)
        except Exception:
            logger.exception("SHADOW contract completion failed chat=%s", row.get("chat_id"))
        row["state_snapshot"] = {**row["state_snapshot"], "analysis_meta": meta}
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("SHADOW enrich failed chat=%s msg=%s", row.get("chat_id"), row.get("message_id"))
    return art


async def _persist(session_factory, row: dict, hist=(), reply_text=None) -> None:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(4)
    try:
        async with _sem:
            async def work():
                art = await _enrich(session_factory, row, hist, reply_text)
                saved = await store.save_decision(session_factory, row)
                await _save_learning(session_factory, art)
                return saved
            inserted = await asyncio.wait_for(work(), TIMEOUT_S)
        logger.info("SHADOW %s chat=%s msg=%s action=%s status=%s",
                    "saved" if inserted else "duplicate-skipped", row["chat_id"],
                    row["message_id"], row["chosen_action"], row["decision_status"])
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("SHADOW persist failed chat=%s msg=%s", row.get("chat_id"), row.get("message_id"))


async def _save_learning(session_factory, art: dict) -> None:
    """Persist the critic record (references / scores / reasons only). Never raises; a failure never affects the shadow row."""
    try:
        c = (art or {}).get("critique")
        if c:
            from copilot_learning.store import save_critique
            await save_critique(session_factory, c)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("SHADOW learning save failed")


def schedule_shadow(*, session_factory, chat_id, message_id, user, client_state,
                    history, regime, reply_text=None, decision_id=None) -> bool:
    """Fire-and-forget. Returns True if a task was scheduled. Never raises."""
    try:
        if not shadow_enabled():
            return False
        key = (int(chat_id), int(message_id), contract.DECISION_POINT_REPLY)
        if key in _seen:
            return False
        if len(_tasks) >= MAX_PENDING:
            logger.warning("SHADOW dropped (backlog=%s) chat=%s", len(_tasks), chat_id)
            return False
        row = _build_row(chat_id, message_id, user, client_state, history, regime, reply_text, decision_id)
        hist = tuple((m.get("role"), m.get("content")) for m in (history or ()) if isinstance(m, dict))
        _seen[key] = None
        while len(_seen) > _SEEN_MAX:
            _seen.popitem(last=False)
        task = asyncio.get_running_loop().create_task(_persist(session_factory, row, hist, reply_text))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return True
    except Exception:
        logger.exception("SHADOW schedule failed chat=%s", chat_id)
        return False
