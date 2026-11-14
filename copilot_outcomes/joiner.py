"""Deterministic, read-only Outcome joiner: which OBSERVED outcomes happened AFTER a decision.

Observational only. No causal claim: the only relations are "observed after" (membership in a set),
ATTR_EXPLICIT and ATTR_FALLBACK. Nothing is scored, ranked or chosen; every outcome is kept.

Rules
  * An outcome belongs to a decision only if observed_at > decision.created_at (strictly: before or exactly
    at decision time is never an outcome) and observed_at - created_at <= outcome_window_seconds (upper
    bound inclusive). The window is an explicit argument; its default is the contract constant.
  * Explicit decision_id is the primary key and is never replaced by a heuristic: the outcome may only join
    THAT decision (even if later decisions exist in between). If that decision is unknown, the chat
    differs, or the time/window rule fails, the outcome joins nothing (no fallback).
  * No decision_id: temporal_chat_fallback = same chat, observed after, inside the window, attached to the
    single NECOPILOTT preceding eligible decision of that chat (never to two). Confidence is untouched.
  * `decisions` is the context of known decisions; fallback can only choose among those.
  * Dedup: identical outcomes (all fields) collapse to one; same outcome_id with different content is kept.
    Order: observed_at, outcome_type, outcome_id, canonical content.
Pure stdlib + contracts. No DB, no network, no LLM, no production imports.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from copilot_evidence.contracts import ContractError, _dt, _int
from copilot_outcomes.contracts import (ATTR_EXPLICIT, ATTR_FALLBACK, DEFAULT_OUTCOME_WINDOW_SECONDS, Outcome)


@dataclass(frozen=True)
class DecisionRef:
    decision_id: str
    chat_id: int
    created_at: datetime

    def __post_init__(self):
        if not (isinstance(self.decision_id, str) and self.decision_id.strip()):
            raise ContractError("decision_id must be a non-empty str")
        object.__setattr__(self, "chat_id", _int("chat_id", self.chat_id))
        object.__setattr__(self, "created_at", _dt("created_at", self.created_at))

    @classmethod
    def of(cls, decision: Any) -> "DecisionRef":
        """From a DecisionRef, a mapping, or any object with decision_id/chat_id/created_at (e.g. ProductionDecision)."""
        if isinstance(decision, DecisionRef):
            return decision
        get = decision.get if isinstance(decision, Mapping) else lambda k: getattr(decision, k, None)
        return cls(get("decision_id"), get("chat_id"), get("created_at"))


@dataclass(frozen=True)
class OutcomeWindow:
    seconds: int | float
    start: datetime          # decision.created_at (exclusive)
    end: datetime            # start + seconds (inclusive)


@dataclass(frozen=True)
class AttributedOutcome:
    outcome: Outcome
    attribution: str         # ATTR_EXPLICIT | ATTR_FALLBACK


@dataclass(frozen=True)
class OutcomeSet:
    decision_id: str
    outcomes: tuple[AttributedOutcome, ...]
    attribution: tuple[str, ...]      # distinct attribution markers present, sorted
    window: OutcomeWindow


def _key(o: Outcome) -> str:
    return json.dumps(o.to_dict(), sort_keys=True, separators=(",", ":"))


def _order(o: Outcome) -> tuple:
    return (o.observed_at, o.outcome_type.value, o.outcome_id, _key(o))


def _window(seconds: Any) -> int | float:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not seconds > 0 \
            or seconds == float("inf"):
        raise ContractError("outcome_window_seconds must be a finite number > 0")
    return seconds


def _refs(decisions: Iterable) -> dict[str, DecisionRef]:
    out: dict[str, DecisionRef] = {}
    for d in decisions:
        r = DecisionRef.of(d)
        if out.setdefault(r.decision_id, r) != r:
            raise ContractError(f"conflicting definitions of decision {r.decision_id!r}")
    return out


def join_all(decisions: Iterable, outcomes: Iterable, *,
             outcome_window_seconds: int | float = DEFAULT_OUTCOME_WINDOW_SECONDS) -> tuple[OutcomeSet, ...]:
    """One OutcomeSet per decision (sorted by created_at, decision_id). Each outcome joins at most ONE decision."""
    secs = _window(outcome_window_seconds)
    delta = timedelta(seconds=secs)
    refs = _refs(decisions)
    uniq: dict[str, Outcome] = {}
    for o in outcomes:
        if not isinstance(o, Outcome):
            raise ContractError("outcomes must be Outcome instances")
        uniq.setdefault(_key(o), o)
    by_chat: dict[int, list[DecisionRef]] = {}
    for r in refs.values():
        by_chat.setdefault(r.chat_id, []).append(r)

    joined: dict[str, list[AttributedOutcome]] = {k: [] for k in refs}

    def eligible(o: Outcome, r: DecisionRef) -> bool:
        return o.chat_id == r.chat_id and r.created_at < o.observed_at <= r.created_at + delta

    for o in sorted(uniq.values(), key=_order):
        if o.decision_id is not None:
            r = refs.get(o.decision_id)
            if r is not None and eligible(o, r):
                joined[r.decision_id].append(AttributedOutcome(o, ATTR_EXPLICIT))
            continue                                    # explicit id never falls back to a heuristic
        cands = [r for r in by_chat.get(o.chat_id, ()) if eligible(o, r)]
        if cands:
            r = max(cands, key=lambda c: (c.created_at, c.decision_id))
            joined[r.decision_id].append(AttributedOutcome(o, ATTR_FALLBACK))

    result = []
    for r in sorted(refs.values(), key=lambda r: (r.created_at, r.decision_id)):
        items = tuple(joined[r.decision_id])
        result.append(OutcomeSet(r.decision_id, items, tuple(sorted({a.attribution for a in items})),
                                 OutcomeWindow(secs, r.created_at, r.created_at + delta)))
    return tuple(result)


def join(decision: Any, outcomes: Iterable, *, decisions: Iterable = (),
         outcome_window_seconds: int | float = DEFAULT_OUTCOME_WINDOW_SECONDS) -> OutcomeSet:
    """OutcomeSet of `decision`. Pass the chat's other known decisions via `decisions` so a fallback outcome
    goes to the nearest preceding one (without them, `decision` is the only candidate)."""
    target = DecisionRef.of(decision)
    sets = join_all([target, *decisions], outcomes, outcome_window_seconds=outcome_window_seconds)
    return next(s for s in sets if s.decision_id == target.decision_id)
