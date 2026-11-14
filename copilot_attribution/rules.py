"""Attribution v1 rules: pure, deterministic functions. No clock, no DB, no network, no LLM, no message text.

attribute_outcome(outcome, decisions, interruptions=(), policy=DEFAULT_POLICY) -> AttributionResult
attribute_all(outcomes, decisions, ...) -> tuple[AttributionResult]   (sorted; identical outcomes collapse)

DIRECT (outcome carries decision_id; temporal rules are then NEVER used, no fallback):
  unsupported type -> INELIGIBLE; decision unknown -> UNKNOWN; other chat -> INELIGIBLE;
  observed <= decision.created_at -> PRE_DECISION (timestamps are never corrected);
  observed <= reply_completed_at (when known) -> INELIGIBLE; else ATTRIBUTED / DIRECT / HIGH.
  The window does not apply to an exact link (windows only guard guesses).
TEMPORAL (no decision_id), in this order:
  type policy: unsupported -> INELIGIBLE; DIRECT_ONLY -> INELIGIBLE; UNKNOWN window -> UNKNOWN;
  same-chat decisions only (none -> NOT_ATTRIBUTED); decisions strictly before the outcome (none -> PRE_DECISION,
  "at_decision_time" when the outcome sits exactly on a decision); within window, upper bound inclusive
  (none -> NOT_ATTRIBUTED/outside_window); reply boundary when known (all excluded -> INELIGIBLE);
  interruptions (follow-up / fixed trigger / insult block in the same chat strictly between decision and outcome)
  remove a decision (all removed -> NOT_ATTRIBUTED/decision_interrupted);
  exactly one left -> ATTRIBUTED / TEMPORAL / LOW; two or more -> AMBIGUOUS. Never nearest/latest/score wins.
Order of inputs never changes the result.
"""
import json
from datetime import timedelta
from typing import Any, Iterable, Mapping

from copilot_attribution.contract import (AttributionPolicy, AttributionResult, DecisionRef, Interruption,
                                       Method, OutcomePolicy, PolicyKind, Relationship, make_result)
from copilot_evidence.contracts import ContractError
from copilot_outcomes.contracts import Outcome, OutcomeType

FOLLOWUP_FIRST_DELAY_SECONDS = 12 * 3600     # main._FOLLOWUP_DELAYS_HOURS[1] = 12

DEFAULT_POLICY = AttributionPolicy({
    OutcomeType.CONTINUED: OutcomePolicy(
        PolicyKind.WINDOW, FOLLOWUP_FIRST_DELAY_SECONDS,
        "the first bot follow-up fires 12h after the user's last message (followup worker); later continuation has "
        "a competing cause (audit 5A R3)"),
    OutcomeType.REGISTRATION: OutcomePolicy(
        PolicyKind.UNKNOWN, None,
        "conversion lag after a reply is unbounded and no real registration data exists (audit 5A R4)"),
    OutcomeType.PAYMENT_ATTEMPT: OutcomePolicy(
        PolicyKind.UNKNOWN, None, "conversion lag unbounded, no real data (audit 5A R4)"),
    OutcomeType.DEPOSIT: OutcomePolicy(
        PolicyKind.UNKNOWN, None, "conversion lag unbounded, no real data (audit 5A R4)"),
    OutcomeType.DROPPED: OutcomePolicy(
        PolicyKind.DIRECT_ONLY, None,
        "only an admin 'lost' label exists: a manual label, not an observed reaction (audit 5A class C)"),
})


def _refs(decisions: Iterable) -> dict[str, DecisionRef]:
    out: dict[str, DecisionRef] = {}
    for d in decisions:
        r = DecisionRef.of(d)
        if out.setdefault(r.decision_id, r) != r:
            raise ContractError(f"conflicting definitions of decision {r.decision_id!r}")
    return out


def _interruptions(items: Iterable) -> tuple[Interruption, ...]:
    out = []
    for i in items:
        if isinstance(i, Interruption):
            out.append(i)
        elif isinstance(i, Mapping):
            out.append(Interruption(i.get("kind"), i.get("chat_id"), i.get("observed_at")))
        else:
            raise ContractError("interruptions must be Interruption instances")
    return tuple(sorted(set(out), key=lambda x: (x.observed_at, x.chat_id, x.kind.value)))


def attribute_outcome(outcome: Outcome, decisions: Iterable, *, interruptions: Iterable = (),
                      policy: AttributionPolicy | None = None) -> AttributionResult:
    if not isinstance(outcome, Outcome):
        raise ContractError("outcome must be an Outcome")
    policy = DEFAULT_POLICY if policy is None else policy
    if not isinstance(policy, AttributionPolicy):
        raise ContractError("policy must be an AttributionPolicy")
    refs, ints = _refs(decisions), _interruptions(interruptions)
    obs, oid = outcome.observed_at, outcome.outcome_id
    base = {"outcome_type": outcome.outcome_type.value}

    def res(rel, meth, reason, decision=None, **meta):
        return make_result(oid, rel, meth, reason, observed_at=obs, decision=decision, metadata={**base, **meta})

    pol = policy.outcome_policies.get(outcome.outcome_type)
    if pol is None:
        return res(Relationship.INELIGIBLE, Method.NONE, "outcome_type_not_supported")

    # ---------------- DIRECT
    if outcome.decision_id is not None:
        d = refs.get(outcome.decision_id)
        if d is None:
            return res(Relationship.UNKNOWN, Method.NONE, "explicit_decision_unknown",
                       explicit_decision_id=outcome.decision_id)
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
                   elapsed_seconds=(obs - d.created_at).total_seconds(),
                   **({} if d.reply_completed_at is not None else {"caveats": ["reply_boundary_unavailable"]}))

    # ---------------- TEMPORAL
    if pol.kind is PolicyKind.DIRECT_ONLY:
        return res(Relationship.INELIGIBLE, Method.NONE, "outcome_type_not_temporally_attributable")
    if pol.kind is PolicyKind.UNKNOWN:
        return res(Relationship.UNKNOWN, Method.NONE, "window_unknown_for_outcome_type")
    chat = [d for d in refs.values() if d.chat_id == outcome.chat_id]
    if not chat:
        return res(Relationship.NOT_ATTRIBUTED, Method.NONE, "no_decision_in_chat")
    after = [d for d in chat if d.created_at < obs]
    if not after:
        at_time = any(d.created_at == obs for d in chat)
        return res(Relationship.PRE_DECISION, Method.NONE,
                   "observed_at_decision_time" if at_time else "observed_before_decision")
    delta = timedelta(seconds=pol.window_seconds)
    inside = [d for d in after if obs - d.created_at <= delta]
    if not inside:
        return res(Relationship.NOT_ATTRIBUTED, Method.NONE, "outside_window", window_seconds=pol.window_seconds)
    replied = [d for d in inside if d.reply_completed_at is None or obs > d.reply_completed_at]
    if not replied:
        return res(Relationship.INELIGIBLE, Method.NONE, "before_reply_completed")
    free, hit = [], set()
    for d in replied:
        kinds = sorted({i.kind.value for i in ints if i.chat_id == d.chat_id and d.created_at < i.observed_at < obs})
        if kinds:
            hit.update(kinds)
        else:
            free.append(d)
    if not free:
        return res(Relationship.NOT_ATTRIBUTED, Method.NONE, "decision_interrupted", interrupted_by=sorted(hit))
    if len(free) > 1:
        return res(Relationship.AMBIGUOUS, Method.NONE, "multiple_eligible_decisions",
                   candidate_decision_ids=sorted(d.decision_id for d in free), window_seconds=pol.window_seconds)
    d = free[0]
    return res(Relationship.ATTRIBUTED, Method.TEMPORAL, "temporal_unique_eligible_decision", d,
               elapsed_seconds=(obs - d.created_at).total_seconds(), window_seconds=pol.window_seconds,
               **({} if d.reply_completed_at is not None else {"caveats": ["reply_boundary_unavailable"]}))


def attribute_all(outcomes: Iterable, decisions: Iterable, *, interruptions: Iterable = (),
                  policy: AttributionPolicy | None = None) -> tuple[AttributionResult, ...]:
    refs, ints = list(_refs(decisions).values()), _interruptions(interruptions)
    uniq: dict[str, Outcome] = {}
    for o in outcomes:
        if not isinstance(o, Outcome):
            raise ContractError("outcomes must be Outcome instances")
        uniq.setdefault(json.dumps(o.to_dict(), sort_keys=True, separators=(",", ":")), o)
    ordered = sorted(uniq.items(), key=lambda kv: (kv[1].observed_at, kv[1].outcome_type.value, kv[1].outcome_id, kv[0]))
    return tuple(attribute_outcome(o, refs, interruptions=ints, policy=policy) for _, o in ordered)
