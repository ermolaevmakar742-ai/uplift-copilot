"""Client State -> Evidence. Reads only fields build_client_state() really produces
(or the ledger's whitelisted snapshot with the same names). Never mutates the input.

Mapped:  stage -> STAGE (stage = map_production_stage, UNKNOWN when ambiguous; raw value kept)
         conversation.intent -> INTENT, conversation.sentiment -> SENTIMENT
         identity.region -> CLIENT_FACT (metadata.derived_from=declared_profile)
Skipped: missing/empty values; intent "unknown" (empty text) and the sentiment that goes with it
(0.5 default, indistinguishable from neutral); region "?"; complexity and messages_count (no canonical type).
confidence/reliability None: Client State heuristics expose none. source_id: caller-supplied or None.
"""
from copy import deepcopy
from typing import Any, Mapping

from copilot_contracts.stages import map_production_stage
from copilot_evidence.adapters.base import AdapterResult, Collector, as_int, failed, num, parse_dt
from copilot_evidence.contracts import EVIDENCE_CONTRACT_VERSION, EvidenceSource, EvidenceType, Provenance
from copilot_contracts.stages import Stage


def _get(state: Mapping, flat: str, *path: str):
    """Full-state path first, then the flat snapshot key."""
    cur: Any = state
    for k in path:
        cur = cur.get(k) if isinstance(cur, Mapping) else None
    return cur if cur is not None else state.get(flat)


def adapt_client_state(state: Mapping, *, chat_id=None, created_at=None, message_id=None,
                       source_id=None) -> AdapterResult:
    try:
        return _adapt(state, chat_id, created_at, message_id, source_id)
    except Exception as e:  # fail-safe
        return failed(e)


def _adapt(state: Any, chat_id, created_at, message_id, source_id) -> AdapterResult:
    c = Collector()
    if not isinstance(state, Mapping):
        c.skip("state", "not_a_mapping")
        return c.result()
    state = deepcopy(dict(state))  # adapter works on a private copy
    cid = as_int(chat_id) if chat_id is not None else as_int(state.get("user_id"))
    ts = parse_dt(created_at)
    if cid is None:
        c.skip("state", "missing_chat_id")
        return c.result()
    if ts is None:
        c.skip("state", "missing_or_naive_created_at")
        return c.result()
    mid = as_int(message_id)
    sid = source_id if isinstance(source_id, str) and source_id.strip() else None
    prov = Provenance(EvidenceSource.CLIENT_STATE, sid)
    obs = parse_dt(state.get("updated_at"), assume_utc=True)  # client_state uses utcnow()
    key = f"c{cid}m{mid if mid is not None else '-'}"

    def emit(etype: EvidenceType, field: str, value, stage=Stage.UNKNOWN, **meta):
        c.add(field, evidence_id=f"client_state:{key}:{etype.value}:{field}", chat_id=cid,
              message_id=mid, created_at=ts, source=EvidenceSource.CLIENT_STATE, evidence_type=etype,
              value=value, confidence=None, reliability=None, observed_at=obs, stage=stage,
              provenance=prov, metadata={"field": field, **meta},
              contract_version=EVIDENCE_CONTRACT_VERSION)

    stage = _get(state, "stage", "stage")
    if isinstance(stage, str) and stage.strip():
        emit(EvidenceType.STAGE, "stage", {"production_stage": stage}, map_production_stage(stage))
    else:
        c.skip("stage", "missing")

    intent = _get(state, "intent", "conversation", "intent")
    sentiment = _get(state, "sentiment", "conversation", "sentiment")
    has_text = isinstance(intent, str) and intent.strip() and intent != "unknown"
    if has_text:
        emit(EvidenceType.INTENT, "intent", intent)
    else:
        c.skip("intent", "missing_or_unknown")
    if not has_text:
        c.skip("sentiment", "default_value_without_text")
    elif num(sentiment) is None:
        c.skip("sentiment", "missing")
    else:
        emit(EvidenceType.SENTIMENT, "sentiment", sentiment)

    region = _get(state, "region", "identity", "region")
    if isinstance(region, str) and 2 <= len(region.strip()) <= 3 and region.strip() != "??":
        emit(EvidenceType.CLIENT_FACT, "region", {"field": "region", "value": region.strip()},
             derived_from="declared_profile")
    else:
        c.skip("region", "missing_or_placeholder")
    for f in ("complexity", "messages_count"):
        c.skip(f, "no_canonical_evidence_type")
    return c.result()
