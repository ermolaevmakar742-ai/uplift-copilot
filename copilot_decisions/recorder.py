"""Fail-safe recorder. Called from on_message right BEFORE the reply is sent.

record_production_decision() is synchronous, non-blocking and never raises: it
builds the immutable record from values production already computed, returns
the decision_id immediately and persists in a background task.
It does not read the DB, call a model, send anything or change any state.
"""
import asyncio
import logging
import os
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from copilot_decisions import store
from copilot_decisions.contract import (CONTRACT_VERSION, DECISION_ID_PREFIX, FIELD_SPEC, SNAPSHOT_NOT_CAPTURED,
                                     UNKNOWN, ProductionDecision, build_client_state_snapshot)

try:  # Decision Evidence capture (own kill switch, default off): never block startup
    from copilot_evidence.capture import schedule_capture as _evidence_capture
except Exception:
    def _evidence_capture(*_a, **_k):
        return False

try:  # Shadow Uplift (own flag COPILOT_UPLIFT_SHADOW_ENABLED, default off): observes only, never touches a reply
    from copilot_uplift.capture import schedule_shadow as _uplift_shadow
except Exception:
    def _uplift_shadow(*_a, **_k):
        return False

try:  # Psychology Engine shadow (own flag COPILOT_PSYCHOLOGY_SHADOW_ENABLED, default off; HARD only): observes only, never touches a reply
    from copilot_psychology.shadow import schedule_assessment as _psychology_shadow
except Exception:
    def _psychology_shadow(*_a, **_k):
        return False

logger = logging.getLogger("copilot_decisions")

DECISION_PATH = "llm_reply"
TIMEOUT_S = 10.0
MAX_PENDING = 50
_SEEN_MAX = 2000
_BREAKER_FAILS, _BREAKER_PAUSE_S = 3, 300

_tasks: set = set()
_seen: "OrderedDict[tuple, str]" = OrderedDict()
_state = {"fails": 0, "paused_until": 0.0}
_sem: asyncio.Semaphore | None = None


def ledger_enabled() -> bool:
    return os.getenv("COPILOT_DECISION_LEDGER_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _planned_reply_parts(reply, splitter):
    """Mirror of send_humanized's normalisation, then production's own splitter."""
    import re
    if not isinstance(reply, str) or splitter is None:
        return UNKNOWN
    t = re.sub(r"\n\s*\n+", " ", reply.strip())
    t = re.sub(r"\n+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return 0
    return len(splitter(t))


def _language_mode():
    try:
        from language_layer.config import get_config
        c = get_config()
        return "active" if c.enabled else "shadow" if c.shadow else "off"
    except Exception:
        return UNKNOWN


def build_decision(*, chat_id, message_id, user=None, regime=UNKNOWN, model=UNKNOWN, intended_model=UNKNOWN,
                   flow_version=UNKNOWN, trigger=UNKNOWN, experiment=None, experiment_variant=UNKNOWN,
                   used_fallback=UNKNOWN, reply_text=None, splitter=None, objection_signal=UNKNOWN,
                   client_state=None, now=None, decision_path=DECISION_PATH, extra_metadata=None) -> ProductionDecision:
    if not (isinstance(decision_path, str) and 0 < len(decision_path) <= 32):
        raise ValueError("decision_path must be a str of 1..32 chars")
    unavailable: dict[str, str] = {}
    status: dict[str, str] = {}

    def val(name, v, reason="not_available_in_scope"):
        if v is UNKNOWN:
            unavailable[name] = reason
            return None
        return v

    parts = _planned_reply_parts(reply_text, splitter)
    parts = val("reply_parts", parts, "reply_text_or_splitter_missing")
    trig = val("trigger", trigger)
    if trig == "":
        trig, status["trigger"] = None, "none_detected"
    var = val("experiment_variant", experiment_variant, "assignment_failed_or_not_reached")
    if experiment_variant is not UNKNOWN and var is None:
        status["experiment_variant"] = "no_assignment"
    obj = val("objection_signal", objection_signal)
    if obj == "":
        obj, status["objection_signal"] = None, "no_marker"
    lang = val("language_mode", _language_mode(), "language_layer_config_unreadable")
    if message_id is None:
        unavailable["message_id"] = "message_id_unavailable"
    unavailable["experiment_id"] = "not_exposed_by_get_experiment_variant"
    snap = build_client_state_snapshot(client_state)
    if snap is None:
        unavailable["client_state_snapshot"] = "client_state_not_built"

    meta = {
        "contract_version": CONTRACT_VERSION,
        "unavailable": unavailable,
        "status": status,
        "not_captured": {"route": "pre_override_route_not_captured", **{f"snapshot.{k}": v for k, v in SNAPSHOT_NOT_CAPTURED.items()}},
        "reply_parts_source": "planned_via_split_function",
        "experiment_name": experiment,
        "decision_path_note": "only the LLM reply path is recorded (fixed triggers, manual mode, insult/troll, injection replies are not)",
    }
    if decision_path != DECISION_PATH:                         # branch decisions (phase 5M): same record, branch identified by decision_path
        meta["decision_path_note"] = f"branch decision '{decision_path}': recorded from the existing production branch; no text is stored or classified"
        if extra_metadata:
            if not isinstance(extra_metadata, dict) or "branch" in meta:
                raise ValueError("extra_metadata must be a dict")
            meta["branch"] = dict(extra_metadata)
    if intended_model is not UNKNOWN:
        meta["intended_model"] = intended_model
    mdl = val("model", model)
    return ProductionDecision(
        decision_id=DECISION_ID_PREFIX + uuid.uuid4().hex,
        chat_id=int(chat_id),
        message_id=None if message_id is None else int(message_id),
        created_at=now or datetime.now(timezone.utc),
        decision_path=decision_path,
        regime=val("regime", regime),
        model=mdl,
        flow_version=val("flow_version", flow_version),
        used_fallback=val("used_fallback", used_fallback),
        reply_parts=parts,
        trigger=trig,
        experiment_id=None,
        experiment_variant=var,
        objection_signal=obj,
        language_mode=lang,
        client_state_snapshot=snap,
        metadata=meta,
    )


async def _persist(session_factory, row: dict) -> None:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(4)
    try:
        async with _sem:
            inserted = await asyncio.wait_for(store.insert_decision(session_factory, row), TIMEOUT_S)
        _state["fails"] = 0
        logger.info("LEDGER %s decision=%s chat=%s msg=%s", "saved" if inserted else "duplicate-skipped",
                    row["decision_id"], row["chat_id"], row["message_id"])
    except asyncio.CancelledError:
        raise
    except Exception:
        _state["fails"] += 1
        if _state["fails"] >= _BREAKER_FAILS:
            _state["paused_until"] = time.monotonic() + _BREAKER_PAUSE_S
            logger.error("LEDGER circuit open for %ss after %s failures", _BREAKER_PAUSE_S, _state["fails"])
        logger.exception("LEDGER persist failed decision=%s", row.get("decision_id"))


def record_production_decision(*, session_factory, chat_id, message_id, **fields) -> str | None:
    """Returns the decision_id (to hand to shadow) or None when disabled/failed. Never raises."""
    try:
        if not ledger_enabled():
            return None
        key = (int(chat_id), None if message_id is None else int(message_id))
        if key[1] is not None and key in _seen:
            return _seen[key]                                  # in-process retry: same id, no second record
        decision = build_decision(chat_id=chat_id, message_id=message_id, **fields)
        if time.monotonic() < _state["paused_until"] or len(_tasks) >= MAX_PENDING:
            logger.warning("LEDGER skipped (paused/backlog) chat=%s", chat_id)
            return decision.decision_id                        # id still valid for linkage; record not persisted
        if key[1] is not None:
            _seen[key] = decision.decision_id
            while len(_seen) > _SEEN_MAX:
                _seen.popitem(last=False)
        task = asyncio.get_running_loop().create_task(_persist(session_factory, decision.to_row()))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        _evidence_capture(decision, session_factory)           # pre-decision snapshot; own flag, never raises
        _uplift_shadow(decision, session_factory)              # shadow calculation only; own flag, never raises
        _psychology_shadow(decision, session_factory)          # HARD-only psychology assessment (shadow); own flag, never raises
        return decision.decision_id
    except Exception:
        logger.exception("LEDGER record failed chat=%s", chat_id)
        return None
