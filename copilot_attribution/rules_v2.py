"""Attribution v2 rules (`attribution.v2`): temporal attribution is bounded by the SENT RESPONSE BOUNDARY, never by
decision.created_at. Pure, deterministic; no clock, DB, network, LLM, message text. v1 (`rules.py`) is unchanged.

  Decision -> SENT response boundary(ies) -> attribution window -> Outcome

A boundary counts for a decision only when ALL hold: status SENT and is_boundary, same chat, linked to THAT decision_id
(unlinked boundaries are never matched by guessing), and sent_at strictly after decision.created_at. FAILED boundaries are
never used (counted in metadata only). A decision's response boundary = the latest of its valid SENT parts (the user has
seen the whole reply only then); metadata.reply_complete says whether all announced parts were SENT.

DIRECT (outcome carries decision_id): identical to v1 rules (no fallback to temporal), result version v2.
TEMPORAL, in order: type policy (as v1); same-chat decisions (none -> NOT_ATTRIBUTED); decisions strictly before the
outcome (none -> PRE_DECISION); a decision without a valid SENT boundary whose created_at is within the window of the
outcome can neither be a candidate nor be ruled out -> UNKNOWN (no_sent_response_boundary, NO fallback to created_at);
boundary-verified decisions: outcome must be strictly after the boundary (else INELIGIBLE before_reply_completed) and within
the window measured FROM THE BOUNDARY (inclusive); interruptions (follow-up / fixed trigger / insult block) strictly between
boundary and outcome remove a decision; exactly one left -> ATTRIBUTED / TEMPORAL / LOW; more -> AMBIGUOUS.
Never latest / nearest / score.
An interruption that is a decision's own recorded action (`source_decision_id`) never interrupts that decision.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from copilot_attribution.contract import (CONTRACT_VERSION_V2, AttributionPolicy, AttributionResult, DecisionRef,
                                       Method, PolicyKind, Relationship, make_result)
from copilot_attribution.rules import DEFAULT_POLICY, _interruptions, _refs
from copilot_evidence.contracts import ContractError, _dt, _int
from copilot_outcomes.contracts import Outcome


@dataclass(frozen=True)
class BoundaryRef:
    """What attribution needs from a response boundary (duck-typed: no import of the boundary package)."""
    boundary_id: str
    decision_id: str | None
    chat_id: int
    sent_at: datetime
    status: str                      # "SENT" | "FAILED"
    is_boundary: bool
    part_index: int | None = None
    parts_total: int | None = None

    def __post_init__(self):
        if not (isinstance(self.boundary_id, str) and self.boundary_id):
            raise ContractError("boundary_id must be a non-empty str")
        if self.status not in ("SENT", "FAILED"):
            raise ContractError("status must be SENT or FAILED")
        if not isinstance(self.is_boundary, bool):
            raise ContractError("is_boundary must be bool")
        object.__setattr__(self, "chat_id", _int("chat_id", self.chat_id))
        object.__setattr__(self, "sent_at", _dt("sent_at", self.sent_at))

    @classmethod
    def of(cls, b: Any) -> "BoundaryRef":
        if isinstance(b, BoundaryRef):
            return b
        get = b.get if isinstance(b, Mapping) else lambda k: getattr(b, k, None)
        st = get("status")
        st = getattr(st, "value", st)
        meta = get("metadata") or {}
        is_b = get("is_boundary")
        return cls(get("response_boundary_id") or get("boundary_id"), get("decision_id"), get("chat_id"), get("sent_at"),
                   st, (st == "SENT") if is_b is None else is_b, meta.get("part_index"), meta.get("parts_total"))


def _boundaries(items: Iterable) -> dict[str, list[BoundaryRef]]:
    out: dict[str, list[BoundaryRef]] = {}
    seen = set()
    for b in items:
        r = BoundaryRef.of(b)
        if r.decision_id is None or (r.boundary_id, r.sent_at) in seen:
            continue                                      # unlinked boundaries are never matched by guessing
        seen.add((r.boundary_id, r.sent_at))
        out.setdefault(r.decision_id, []).append(r)
    return out


@dataclass(frozen=True)
class BoundaryInfo:
    end: datetime | None             # latest valid SENT part, None = no valid boundary
    ids: tuple[str, ...]
    complete: bool
    failed: int
    invalid: int


def boundary_info(d: DecisionRef, items: Iterable[BoundaryRef]) -> BoundaryInfo:
    valid, failed, invalid = [], 0, 0
    for b in items:
        if b.status == "FAILED" or not b.is_boundary:
            failed += 1
        elif b.chat_id != d.chat_id or not b.sent_at > d.created_at:
            invalid += 1                                  # integrity violation: reported, never used
        else:
            valid.append(b)
    if not valid:
        return BoundaryInfo(None, (), False, failed, invalid)
    total = {b.parts_total for b in valid if b.parts_total is not None}
    idx = {b.part_index for b in valid if b.part_index is not None}
    complete = len(total) == 1 and len(idx) == next(iter(total)) and idx == set(range(next(iter(total))))
    return BoundaryInfo(max(b.sent_at for b in valid), tuple(sorted({b.boundary_id for b in valid})), complete, failed, invalid)


def attribute_outcome_v2(outcome: Outcome, decisions: Iterable, boundaries: Iterable = (), *,
                         interruptions: Iterable = (), policy: AttributionPolicy | None = None) -> AttributionResult:
    if not isinstance(outcome, Outcome):
        raise ContractError("outcome must be an Outcome")
    policy = DEFAULT_POLICY if policy is None else policy
    if not isinstance(policy, AttributionPolicy):
        raise ContractError("policy must be an AttributionPolicy")
    refs, ints, bmap = _refs(decisions), _interruptions(interruptions), _boundaries(boundaries)
    obs, oid = outcome.observed_at, outcome.outcome_id
    base = {"outcome_type": outcome.outcome_type.value}

    def res(rel, meth, reason, decision=None, **meta):
        return make_result(oid, rel, meth, reason, observed_at=obs, decision=decision, metadata={**base, **meta},
                           contract_version=CONTRACT_VERSION_V2)

    pol = policy.outcome_policies.get(outcome.outcome_type)
    if pol is None:
        return res(Relationship.INELIGIBLE, Method.NONE, "outcome_type_not_supported")

    # ---------------- DIRECT: same rules as v1
    if outcome.decision_id is not None:
        d = refs.get(outcome.decision_id)
        if d is None:
            return res(Relationship.UNKNOWN, Method.NONE, "explicit_decision_unknown", explicit_decision_id=outcome.decision_id)
        meta = {"explicit_decision_id": d.decision_id}
        if d.chat_id != outcome.chat_id:
            return res(Relationship.INELIGIBLE, Method.NONE, "explicit_decision_chat_mismatch", **meta)
        if obs < d.created_at:
            return res(Relationship.PRE_DECISION, Method.NONE, "observed_before_decision", **meta)
        if obs == d.created_at:
            return res(Relationship.PRE_DECISION, Method.NONE, "observed_at_decision_time", **meta)
        if d.reply_completed_at is not None and obs <= d.reply_completed_at:
            return res(Relationship.INELIGIBLE, Method.NONE, "before_reply_completed", **meta)
        return res(Relationship.ATTRIBUTED, Method.DIRECT, "explicit_decision_id_verified", d,
                   elapsed_seconds=(obs - d.created_at).total_seconds())

    # ---------------- TEMPORAL (boundary-based)
    if pol.kind is PolicyKind.DIRECT_ONLY:
        return res(Relationship.INELIGIBLE, Method.NONE, "outcome_type_not_temporally_attributable")
    if pol.kind is PolicyKind.UNKNOWN:
        return res(Relationship.UNKNOWN, Method.NONE, "window_unknown_for_outcome_type")
    chat = [d for d in refs.values() if d.chat_id == outcome.chat_id]
    if not chat:
        return res(Relationship.NOT_ATTRIBUTED, Method.NONE, "no_decision_in_chat")
    before = [d for d in chat if d.created_at < obs]
    if not before:
        return res(Relationship.PRE_DECISION, Method.NONE,
                   "observed_at_decision_time" if any(d.created_at == obs for d in chat) else "observed_before_decision")
    W = timedelta(seconds=pol.window_seconds)
    info = {d.decision_id: boundary_info(d, bmap.get(d.decision_id, ())) for d in before}
    unverifiable = sorted(d.decision_id for d in before if info[d.decision_id].end is None and obs - d.created_at <= W)
    if unverifiable:
        return res(Relationship.UNKNOWN, Method.NONE, "no_sent_response_boundary", unverifiable_decision_ids=unverifiable,
                   failed_boundaries=sum(info[i].failed for i in unverifiable))
    withb = [d for d in before if info[d.decision_id].end is not None]
    inflight = [d for d in withb if obs <= info[d.decision_id].end]
    inwin = [d for d in withb if obs > info[d.decision_id].end and obs - info[d.decision_id].end <= W]
    free, hit = [], False
    for d in inwin:
        end = info[d.decision_id].end
        if any(i.chat_id == d.chat_id and end < i.observed_at < obs and i.source_decision_id != d.decision_id for i in ints):
            hit = True
        else:
            free.append(d)
    if not free:
        if inflight:
            return res(Relationship.INELIGIBLE, Method.NONE, "before_reply_completed")
        if hit:
            return res(Relationship.NOT_ATTRIBUTED, Method.NONE, "decision_interrupted")
        return res(Relationship.NOT_ATTRIBUTED, Method.NONE, "outside_window", window_seconds=pol.window_seconds)
    if len(free) > 1:
        return res(Relationship.AMBIGUOUS, Method.NONE, "multiple_eligible_decisions",
                   candidate_decision_ids=sorted(d.decision_id for d in free), window_seconds=pol.window_seconds)
    d = free[0]
    bi = info[d.decision_id]
    return res(Relationship.ATTRIBUTED, Method.TEMPORAL, "temporal_unique_eligible_decision", d,
               elapsed_seconds=(obs - d.created_at).total_seconds(),
               elapsed_since_boundary_seconds=(obs - bi.end).total_seconds(), boundary_sent_at=bi.end.isoformat(),
               response_boundary_ids=list(bi.ids), reply_complete=bi.complete, window_seconds=pol.window_seconds,
               window_start="response_boundary")


def attribute_all_v2(outcomes: Iterable, decisions: Iterable, boundaries: Iterable = (), *, interruptions: Iterable = (),
                     policy: AttributionPolicy | None = None) -> tuple[AttributionResult, ...]:
    refs, ints = list(_refs(decisions).values()), _interruptions(interruptions)
    bs = [BoundaryRef.of(b) for b in boundaries]
    uniq: dict[str, Outcome] = {}
    for o in outcomes:
        if not isinstance(o, Outcome):
            raise ContractError("outcomes must be Outcome instances")
        uniq.setdefault(json.dumps(o.to_dict(), sort_keys=True, separators=(",", ":")), o)
    ordered = sorted(uniq.items(), key=lambda kv: (kv[1].observed_at, kv[1].outcome_type.value, kv[1].outcome_id, kv[0]))
    return tuple(attribute_outcome_v2(o, refs, bs, interruptions=ints, policy=policy) for _, o in ordered)
