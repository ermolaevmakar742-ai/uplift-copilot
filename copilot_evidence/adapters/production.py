"""Production Decision Ledger record -> ONE Evidence about what the SYSTEM did.

This is an observation of a system action, NOT a fact about the client: evidence_type is ACTION
(and metadata.subject="system_action", not_a_client_fact=True). stage is UNKNOWN: no stage is implied by regime/trigger.
No INTENT/STAGE/CLIENT_FACT is derived. trigger/objection_signal/language_mode are kept as metadata,
not promoted to client signals. legacy/unsupported fields and client_state_snapshot are not copied (the Client
State adapter owns the latter). source_id = decision_id. confidence/reliability None.
"""
from typing import Any, Mapping

from copilot_contracts.stages import Stage
from copilot_evidence.adapters.base import AdapterResult, Collector, as_int, failed, parse_dt
from copilot_evidence.contracts import EVIDENCE_CONTRACT_VERSION, EvidenceSource, EvidenceType, Provenance

_ACTION = ("decision_path", "regime", "model", "flow_version", "used_fallback", "reply_parts")
_CONTEXT = ("trigger", "experiment_variant", "objection_signal", "language_mode")


def adapt_production_decision(decision: Any) -> AdapterResult:
    try:
        return _adapt(decision)
    except Exception as e:  # fail-safe
        return failed(e)


def _adapt(decision) -> AdapterResult:
    c = Collector()
    if isinstance(decision, Mapping):
        d = dict(decision)
    elif callable(getattr(decision, "to_dict", None)):
        d = dict(decision.to_dict())
    else:
        c.skip("decision", "unreadable")
        return c.result()
    did, cid = d.get("decision_id"), as_int(d.get("chat_id"))
    ts = parse_dt(d.get("created_at"))
    if not (isinstance(did, str) and did.strip()):
        c.skip("decision", "missing_decision_id")
    elif cid is None:
        c.skip("decision", "missing_chat_id")
    elif ts is None:
        c.skip("decision", "missing_or_naive_created_at")
    else:
        value = {k: d.get(k) for k in _ACTION if d.get(k) is not None}
        if not value:
            c.skip("decision", "no_action_fields")
        else:
            meta = {k: d.get(k) for k in _CONTEXT if d.get(k) is not None}
            md = d.get("metadata") if isinstance(d.get("metadata"), Mapping) else {}
            meta.update({"subject": "system_action", "not_a_client_fact": True,
                         "ledger_contract_version": md.get("contract_version")})
            c.add("decision", evidence_id=f"production:{did}", chat_id=cid,
                  message_id=as_int(d.get("message_id")), created_at=ts, source=EvidenceSource.PRODUCTION,
                  evidence_type=EvidenceType.ACTION, value=value, confidence=None, reliability=None,
                  observed_at=ts, stage=Stage.UNKNOWN,
                  provenance=Provenance(EvidenceSource.PRODUCTION, did), metadata=meta,
                  contract_version=EVIDENCE_CONTRACT_VERSION)
    for f in ("client_state_snapshot",):
        if d.get(f) is not None:
            c.skip(f, "not_copied_owned_by_other_adapter_or_legacy")
    return c.result()
