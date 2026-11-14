"""Shadow analysis row -> Evidence. Format translation only: Shadow formulas, rules and
confidence values are passed through untouched.

Input: a shadow_decisions row / dict with decision_id, chat_id, message_id, created_at and the
analytical modules momentum, contradiction, deposit_path (JSON dicts).

Mapped:
  momentum                       -> MOMENTUM        confidence = momentum.confidence
  contradiction (detected)       -> CONTRADICTION   confidence = contradiction.confidence
  deposit_path.current_node      -> STAGE           confidence = transition_confidence
  deposit_path.blocker           -> BLOCKER         confidence = blocker_confidence
  blocker PAYMENT_*              -> PAYMENT_SIGNAL  (same observation, metadata.derived_from=blocker)
  blocker TRUST_CONCERN          -> TRUST_SIGNAL    (same observation, metadata.derived_from=blocker)
Skipped with reason: module error/missing, no-evidence/not-detected results (absence is not
evidence), next_best_question (a recommendation, not an observation), STAGE from
node_source=production_stage (Shadow's STAGE_NODE differs from the stage.v1 mapping, e.g.
deposited->SUCCESS vs DEPOSIT; the Client State adapter owns production stages).
reliability is None: Shadow exposes none. confidence_type is kept in metadata.
"""
from typing import Any, Mapping

from copilot_contracts.stages import Stage, normalize_stage
from copilot_evidence.adapters.base import AdapterResult, Collector, as_int, failed, num, parse_dt
from copilot_evidence.contracts import EVIDENCE_CONTRACT_VERSION, EvidenceSource, EvidenceType, Provenance

_PAYMENT_BLOCKERS = {"PAYMENT_METHOD_UNCERTAINTY", "PAYMENT_AMOUNT_HESITATION"}
_TRUST_BLOCKERS = {"TRUST_CONCERN"}
_TEXT_NODE_SOURCES = {"current_turn_text", "earlier_turn_text"}


def _mod(row: Mapping, name: str, c: Collector) -> dict | None:
    m = row.get(name)
    if m is None:
        c.skip(name, "module_missing")
        return None
    if not isinstance(m, Mapping):
        c.skip(name, "module_not_a_mapping")
        return None
    if "error" in m:
        c.skip(name, f"module_error:{m.get('error')}")
        return None
    return dict(m)


def adapt_shadow_row(row: Mapping, *, created_at=None) -> AdapterResult:
    try:
        return _adapt(row, created_at)
    except Exception as e:  # fail-safe
        return failed(e)


def _adapt(row: Any, created_at) -> AdapterResult:
    c = Collector()
    if not isinstance(row, Mapping):
        c.skip("row", "not_a_mapping")
        return c.result()
    chat_id, message_id = as_int(row.get("chat_id")), as_int(row.get("message_id"))
    did = row.get("decision_id")
    source_id = did if isinstance(did, str) and did.strip() else None
    ts = parse_dt(created_at) or parse_dt(row.get("created_at"))
    if chat_id is None:
        c.skip("row", "missing_chat_id")
        return c.result()
    if ts is None:
        c.skip("row", "missing_or_naive_created_at")
        return c.result()
    key = source_id or f"c{chat_id}m{message_id}"
    prov = Provenance(EvidenceSource.SHADOW, source_id)

    dp = _mod(row, "deposit_path", c)
    ctx_stage = Stage.UNKNOWN
    if dp is not None and dp.get("node_source") in _TEXT_NODE_SOURCES:
        ctx_stage = normalize_stage(dp.get("current_node"))

    def emit(etype: EvidenceType, value: dict, conf, stage: Stage, module: dict, **meta):
        c.add(etype.value, evidence_id=f"shadow:{key}:{etype.value}", chat_id=chat_id,
              message_id=message_id, created_at=ts, source=EvidenceSource.SHADOW, evidence_type=etype,
              value=value, confidence=num(conf), reliability=None, observed_at=ts, stage=stage,
              provenance=prov, metadata={"confidence_type": module.get("confidence_type"),
                                         "module_version": module.get("version"), **meta},
              contract_version=EVIDENCE_CONTRACT_VERSION)

    mom = _mod(row, "momentum", c)
    if mom is not None:
        if not mom.get("user_turns_seen"):
            c.skip("momentum", "no_user_turns_seen")
        elif "direction" not in mom or "score" not in mom:
            c.skip("momentum", "missing_score_or_direction")
        else:
            emit(EvidenceType.MOMENTUM,
                 {"score": mom["score"], "direction": mom["direction"],
                  "positive_signals": mom.get("positive_signals") or [],
                  "negative_signals": mom.get("negative_signals") or []},
                 mom.get("confidence"), ctx_stage, mom)

    ctr = _mod(row, "contradiction", c)
    if ctr is not None:
        if not ctr.get("detected"):
            c.skip("contradiction", "not_detected")
        elif not ctr.get("type"):
            c.skip("contradiction", "missing_type")
        else:
            emit(EvidenceType.CONTRADICTION,
                 {"type": ctr["type"], "intent_signal": ctr.get("intent_signal"),
                  "behavioral_commitment": ctr.get("behavioral_commitment")},
                 ctr.get("confidence"), ctx_stage, ctr)

    if row.get("next_best_question") is not None:
        c.skip("next_best_question", "recommendation_not_observation")

    if dp is not None:
        src = dp.get("node_source")
        if src in _TEXT_NODE_SOURCES:
            st = normalize_stage(dp.get("current_node"))
            if st is Stage.UNKNOWN:
                c.skip("deposit_path.stage", "unrecognised_node")
            else:
                emit(EvidenceType.STAGE,
                     {"current_node": dp.get("current_node"), "previous_node": dp.get("previous_node"),
                      "furthest_node": dp.get("furthest_node"), "regression": dp.get("regression"),
                      "node_source": src},
                     dp.get("transition_confidence"), st, dp)
        elif src == "production_stage":
            c.skip("deposit_path.stage", "node_from_production_stage_mapping_differs_from_stage.v1")
        else:
            c.skip("deposit_path.stage", f"no_evidence:{src}")

        blocker = dp.get("blocker")
        if blocker:
            bval = {"blocker": blocker, "blocked_transition": dp.get("blocked_transition")}
            emit(EvidenceType.BLOCKER, bval, dp.get("blocker_confidence"), ctx_stage, dp)
            if blocker in _PAYMENT_BLOCKERS:
                emit(EvidenceType.PAYMENT_SIGNAL, bval, dp.get("blocker_confidence"), ctx_stage, dp,
                     derived_from="deposit_path.blocker")
            elif blocker in _TRUST_BLOCKERS:
                emit(EvidenceType.TRUST_SIGNAL, bval, dp.get("blocker_confidence"), ctx_stage, dp,
                     derived_from="deposit_path.blocker")
    return c.result()
