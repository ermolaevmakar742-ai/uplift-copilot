"""Single integration entry point called from the live on_message, after the reply was sent.

Account -> assigned offer lookup, observation only. Contract: schedule_offer_shadow() is synchronous, never raises, never awaits, never blocks. It reads only plain values from
live objects; everything else (DB reads, decision, event, append-only write) runs in a bounded background task. The result never
reaches the reply, the history, the client state, the funnel or the LLM."""
import asyncio
import hashlib
import logging
import uuid
from collections import OrderedDict

from . import events
from .accounts import account_ref_for
from .decision import decide
from .models import OfferInput
from .resolver import resolve_assigned_offer

logger = logging.getLogger("copilot_offers")
TIMEOUT_S = 10.0
MAX_PENDING = 50
_tasks: set = set()
_seen: "OrderedDict[str, None]" = OrderedDict()      # production decision ids already handled by this process
_SEEN_MAX = 5000


def _client_ref(chat_id) -> str:
    try:
        from copilot_shadow.contract import client_id          # same pseudonym as shadow / ledger / voice
        return client_id(chat_id)
    except Exception:
        return "cli_" + hashlib.sha256(f"ares-offers-client:{int(chat_id)}".encode()).hexdigest()[:16]


def build_input(*, chat_id, user, client_state, decision_id, account_id) -> OfferInput:
    corr = decision_id if isinstance(decision_id, str) and decision_id else "nopd_" + uuid.uuid4().hex
    return OfferInput(correlation_id=corr, client_ref=_client_ref(chat_id), account_ref=account_ref_for(account_id),
                      stage_raw=getattr(user, "stage", None))


def _claim(corr: str) -> bool:
    """One offer decision per production decision (pd_...): False if this process already handled it. Ids without a production
    decision (nopd_...) are unique by construction. The DB check in events.persist covers restarts."""
    if not corr.startswith("pd_"):
        return True
    if corr in _seen:
        return False
    _seen[corr] = None
    while len(_seen) > _SEEN_MAX:
        _seen.popitem(last=False)
    return True


async def run(session_factory, inp: OfferInput, *, loader=resolve_assigned_offer, persister=events.persist):
    """Resolve + decide + emit (+ persist when SHADOW). Returns the decision, or None (duplicate / error). Never raises."""
    if not _claim(inp.correlation_id):
        return None
    try:
        res = await loader(session_factory, inp.account_ref)
        decision = decide(inp, res)
        if events.shadow_enabled():
            try:
                if await persister(session_factory, decision.to_record()) is False:
                    return None                                    # already recorded for this production decision
            except Exception as e:
                events.emit("OFFER_DECISION_ERROR", {"stage": "persist", "error": type(e).__name__,
                                                     "offer_decision_id": decision.offer_decision_id,
                                                     "correlation_id": decision.correlation_id})
        events.emit("OFFER_DECISION", {**decision.to_record(), "shadow_persist": events.shadow_enabled()})
        return decision
    except Exception as e:
        _seen.pop(inp.correlation_id, None)                        # nothing was recorded: a retry may try again
        try:
            events.emit("OFFER_DECISION_ERROR", {"stage": "decide", "error": type(e).__name__,
                                                 "correlation_id": inp.correlation_id})
        except Exception:
            pass
        return None


async def _guarded(session_factory, inp):
    try:
        await asyncio.wait_for(run(session_factory, inp), TIMEOUT_S)
    except Exception:
        logger.warning("OFFER shadow task failed or timed out")


def schedule_offer_shadow(*, session_factory, chat_id, user, client_state=None, decision_id=None,
                          account_id=None, **_ignored) -> bool:
    try:
        if not events.engine_enabled() or len(_tasks) >= MAX_PENDING:
            return False
        inp = build_input(chat_id=chat_id, user=user, client_state=client_state, decision_id=decision_id,
                          account_id=account_id)
        task = asyncio.get_running_loop().create_task(_guarded(session_factory, inp))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return True
    except Exception:
        try:
            logger.warning("OFFER shadow schedule failed")
        except Exception:
            pass
        return False
