"""Voice Decision service: policy + cooldown + events. Never raises into the caller."""
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timezone

from .adapter import MockVoiceJobDispatcher
from .models import (POLICY_VERSION, VoiceContext, VoiceDecisionResult, VoicePriority, VoiceReason, new_decision_id)
from .policy import RulePolicyV1

logger = logging.getLogger("copilot_voice")
_TRUE = {"1", "true", "yes", "on"}
_LAST_VOICE_MAX = 5000
_last_voice: "OrderedDict[str, float]" = OrderedDict()   # in-process cooldown; resets on restart (documented)

_policy = RulePolicyV1()
_dispatcher = MockVoiceJobDispatcher()


def decision_enabled() -> bool:
    return os.getenv("COPILOT_VOICE_DECISION_ENABLED", "false").strip().lower() in _TRUE


def autosend_enabled() -> bool:
    return os.getenv("COPILOT_VOICE_AUTOSEND_ENABLED", "false").strip().lower() in _TRUE


def set_policy(policy) -> None:
    global _policy
    _policy = policy


def set_dispatcher(dispatcher) -> None:
    global _dispatcher
    _dispatcher = dispatcher


def reset_state() -> None:
    _last_voice.clear()


def _valid_chat_id(chat_id) -> str | None:
    if isinstance(chat_id, bool):
        return None
    try:
        v = int(chat_id)
    except (TypeError, ValueError):
        return None
    return str(v) if v != 0 else None


def _chat_ref(chat_id: str) -> str:
    try:
        from copilot_shadow.contract import client_id      # same pseudonym as shadow/ledger -> joinable
        return client_id(chat_id)
    except Exception:
        import hashlib
        return "cli_" + hashlib.sha256(("ares-voice" + chat_id).encode()).hexdigest()[:16]


def _emit(name: str, payload: dict) -> None:
    payload = {"ts": datetime.now(timezone.utc).isoformat(), **payload}
    logger.info("%s %s", name, json.dumps(payload, ensure_ascii=False, default=str))


def decide(ctx: VoiceContext, *, now: float | None = None) -> VoiceDecisionResult:
    """Pure-ish: applies cooldown + policy. correlation_id is preserved if given, else generated."""
    decision_id = new_decision_id()
    corr = ctx.correlation_id or decision_id
    chat = _valid_chat_id(ctx.chat_id)
    text = (ctx.reply_text or "").strip()

    def result(ok, score, reason, prio):
        return VoiceDecisionResult(ok, score, reason, prio, getattr(_policy, "voice_profile", "valeria_default"),
                                   text, chat or "", corr, decision_id, getattr(_policy, "version", POLICY_VERSION))

    if chat is None:
        return result(False, 0.0, VoiceReason.OTHER, VoicePriority.LOW)
    if ctx.seconds_since_last_voice is None and chat in _last_voice:
        t = now if now is not None else time.monotonic()
        ctx = replace(ctx, seconds_since_last_voice=t - _last_voice[chat])
    o = _policy.evaluate(ctx)
    return result(o.should_send, o.score, o.reason, o.priority)


def evaluate_and_dispatch(ctx: VoiceContext) -> VoiceDecisionResult | None:
    """Entry point. Disabled -> None and no side effects. Any failure is logged and swallowed."""
    if not decision_enabled():
        return None
    try:
        res = decide(ctx)
        ev = res.to_event()
        ev["chat_id"] = _chat_ref(res.chat_id) if res.chat_id else None
        ev["autosend"] = autosend_enabled()
        _emit("VOICE_DECISION", ev)
        if res.should_send_voice:
            _last_voice[res.chat_id] = time.monotonic()
            _last_voice.move_to_end(res.chat_id)
            while len(_last_voice) > _LAST_VOICE_MAX:
                _last_voice.popitem(last=False)
            if autosend_enabled():
                try:
                    _dispatcher.dispatch(res.to_job())
                except Exception as e:      # the decision stays valid; only the hand-off failed
                    _emit("VOICE_DECISION_ERROR", {"stage": "dispatch", "error": type(e).__name__,
                                                   "decision_id": res.decision_id, "correlation_id": res.correlation_id})
        return res
    except Exception as e:
        try:
            _emit("VOICE_DECISION_ERROR", {"error": type(e).__name__, "correlation_id": getattr(ctx, "correlation_id", "")})
        except Exception:
            pass
        return None
