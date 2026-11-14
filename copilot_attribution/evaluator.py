"""Offline Attribution Evaluator (phase 5E): applies the EXISTING Attribution v1 rules to given data and reports
what they do. Read-only analytics: no persistence, no SQL, no clock, no network, no LLM, no scoring, no prediction.

evaluate(decisions, outcomes, interruptions=(), policy=None) -> EvaluationResult        (pure, deterministic)
evaluate_from(provider, policy=None)                                                    (async; provider.load() only)

The result is immutable. It contains the per-outcome AttributionResults, exact counts (and percentages, None when the
denominator is 0 — never a fabricated 0.0), counts by outcome type / method / reason, the decision -> outcome latency of
attributed results (raw seconds + fixed buckets; min/max/median only when there is data) and the number of temporal-integrity
checks. Integrity guards raise EvaluationIntegrityError if a result ever violates the temporal rules:
  ATTRIBUTED  -> outcome strictly after its decision (DIRECT additionally needs the explicit decision_id to match)
  PRE_DECISION-> no same-chat decision (or the explicit one) lies strictly before the outcome
Nothing here says anything is "caused by" a decision; sample size is reported, quality is not inferred.
"""
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol

from copilot_attribution.contract import (AttributionPolicy, AttributionResult, DecisionRef, Method, Relationship)
from copilot_attribution.rules import DEFAULT_POLICY, attribute_all
from copilot_evidence.contracts import ContractError
from copilot_outcomes.contracts import Outcome

EVALUATOR_VERSION = "attribution-eval.v1"
# fixed latency buckets (upper bound inclusive, seconds); chosen once, not derived from data
LATENCY_BUCKETS = (("<=1m", 60), ("1m-5m", 300), ("5m-30m", 1800), ("30m-2h", 7200), ("2h-12h", 43200), (">12h", None))


class EvaluationIntegrityError(AssertionError):
    """A produced AttributionResult violates the temporal rules."""


class DataProvider(Protocol):
    async def load(self) -> Any: ...      # object with .decisions .outcomes .interruptions .unreadable .notes


@dataclass(frozen=True)
class Latency:
    count: int
    seconds: tuple[float, ...]            # sorted raw elapsed seconds of attributed results
    minimum: float | None
    maximum: float | None
    median: float | None
    buckets: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class EvaluationResult:
    version: str
    decisions: int
    outcomes_loaded: int
    outcomes: int                          # unique outcomes evaluated (identical duplicates collapse)
    chats: int
    interruptions: int
    unreadable: tuple[tuple[str, int], ...]
    notes: tuple[str, ...]
    relationships: tuple[tuple[str, int], ...]
    methods: tuple[tuple[str, int], ...]
    by_outcome_type: tuple[tuple[str, tuple[tuple[str, int], ...]], ...]
    reasons: tuple[tuple[str, str, int], ...]
    latency: Latency
    integrity_checked: int
    results: tuple[AttributionResult, ...]

    def percent(self, n: int) -> float | None:
        return None if self.outcomes == 0 else round(100.0 * n / self.outcomes, 1)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "dataset": {"decisions": self.decisions, "outcomes_loaded": self.outcomes_loaded, "outcomes": self.outcomes,
                        "chats": self.chats, "interruptions": self.interruptions,
                        "unreadable": dict(self.unreadable), "notes": list(self.notes)},
            "coverage": {k: {"count": n, "percent": self.percent(n)} for k, n in self.relationships},
            "by_method": dict(self.methods),
            "by_outcome_type": {t: dict(c) for t, c in self.by_outcome_type},
            "reasons": [{"relationship": r, "reason": x, "count": n} for r, x, n in self.reasons],
            "latency": {"count": self.latency.count, "seconds": list(self.latency.seconds),
                        "min": self.latency.minimum, "max": self.latency.maximum, "median": self.latency.median,
                        "buckets": dict(self.latency.buckets)},
            "integrity_checked": self.integrity_checked,
            "results": [r.to_dict() for r in self.results],
        }


def check_temporal_integrity(results: Iterable[AttributionResult], decisions: Mapping[str, DecisionRef],
                             outcomes: Mapping[str, Outcome]) -> int:
    """Raises EvaluationIntegrityError on any violation; returns the number of results checked."""
    by_chat: dict[int, list[DecisionRef]] = {}
    for d in decisions.values():
        by_chat.setdefault(d.chat_id, []).append(d)
    n = 0
    for r in results:
        o = outcomes[r.outcome_id]
        if r.observed_at != o.observed_at:
            raise EvaluationIntegrityError(f"{r.outcome_id}: result observed_at differs from the outcome's")
        if r.relationship is Relationship.ATTRIBUTED:
            d = decisions.get(r.decision_id)
            if d is None or d.chat_id != o.chat_id:
                raise EvaluationIntegrityError(f"{r.outcome_id}: attributed to an unknown/other-chat decision")
            if not o.observed_at > d.created_at or r.decision_created_at != d.created_at:
                raise EvaluationIntegrityError(f"{r.outcome_id}: attributed but not strictly after its decision")
            if r.method is Method.DIRECT and o.decision_id != r.decision_id:
                raise EvaluationIntegrityError(f"{r.outcome_id}: DIRECT without a matching explicit decision_id")
            if r.method is Method.TEMPORAL and o.decision_id is not None:
                raise EvaluationIntegrityError(f"{r.outcome_id}: TEMPORAL for an outcome carrying a decision_id")
        elif r.relationship is Relationship.PRE_DECISION:
            if o.decision_id is not None:
                d = decisions.get(o.decision_id)
                bad = d is None or o.observed_at > d.created_at
            else:
                bad = any(d.created_at < o.observed_at for d in by_chat.get(o.chat_id, ())) or not by_chat.get(o.chat_id)
            if bad:
                raise EvaluationIntegrityError(f"{r.outcome_id}: PRE_DECISION but a decision precedes the outcome")
        n += 1
    return n


def _latency(results: tuple[AttributionResult, ...]) -> Latency:
    secs = sorted(float(r.metadata["elapsed_seconds"]) for r in results
                  if r.relationship is Relationship.ATTRIBUTED and "elapsed_seconds" in r.metadata)
    counts = [0] * len(LATENCY_BUCKETS)
    for s in secs:
        for i, (_, hi) in enumerate(LATENCY_BUCKETS):
            if hi is None or s <= hi:
                counts[i] += 1
                break
    n = len(secs)
    med = None if n == 0 else (secs[n // 2] if n % 2 else (secs[n // 2 - 1] + secs[n // 2]) / 2)
    return Latency(n, tuple(secs), secs[0] if n else None, secs[-1] if n else None, med,
                   tuple((LATENCY_BUCKETS[i][0], counts[i]) for i in range(len(counts))))


def evaluate(decisions: Iterable, outcomes: Iterable, *, interruptions: Iterable = (),
             policy: AttributionPolicy | None = None, unreadable: Mapping[str, int] | None = None,
             notes: Iterable[str] = ()) -> EvaluationResult:
    refs: dict[str, DecisionRef] = {}
    for d in decisions:
        r = DecisionRef.of(d)
        if refs.setdefault(r.decision_id, r) != r:
            raise ContractError(f"conflicting definitions of decision {r.decision_id!r}")
    outs = list(outcomes)
    if not all(isinstance(o, Outcome) for o in outs):
        raise ContractError("outcomes must be Outcome instances")
    ints = list(interruptions)
    results = attribute_all(outs, list(refs.values()), interruptions=ints, policy=policy)
    unique = {json.dumps(o.to_dict(), sort_keys=True, separators=(",", ":")): o for o in outs}
    # attribute_all sorts identically, so results and `ordered` line up one-to-one (exact object per result)
    ordered = sorted(unique.items(), key=lambda kv: (kv[1].observed_at, kv[1].outcome_type.value, kv[1].outcome_id, kv[0]))
    checked = 0
    pairs = [(r, o) for r, (_, o) in zip(results, ordered)]
    for r, o in pairs:
        checked += check_temporal_integrity([r], refs, {r.outcome_id: o})

    rel_order = [r.value for r in Relationship]
    rel = {k: 0 for k in rel_order}
    meth = {m.value: 0 for m in Method}
    types: dict[str, dict[str, int]] = {}
    reasons: dict[tuple[str, str], int] = {}
    for r, o in pairs:
        rel[r.relationship.value] += 1
        meth[r.method.value] += 1
        t = types.setdefault(o.outcome_type.value, {"total": 0, **{k: 0 for k in rel_order}})
        t["total"] += 1
        t[r.relationship.value] += 1
        key = (r.relationship.value, r.reason)
        reasons[key] = reasons.get(key, 0) + 1
    chats = {d.chat_id for d in refs.values()} | {o.chat_id for o in unique.values()}
    return EvaluationResult(
        version=EVALUATOR_VERSION, decisions=len(refs), outcomes_loaded=len(outs), outcomes=len(results),
        chats=len(chats), interruptions=len(ints),
        unreadable=tuple(sorted((k, int(v)) for k, v in (unreadable or {}).items())), notes=tuple(notes),
        relationships=tuple((k, rel[k]) for k in rel_order), methods=tuple((k, meth[k]) for k in meth),
        by_outcome_type=tuple((t, tuple(sorted(c.items(), key=lambda kv: (kv[0] != "total", kv[0]))))
                              for t, c in sorted(types.items())),
        reasons=tuple((r, x, n) for (r, x), n in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
        latency=_latency(results), integrity_checked=checked, results=results)


async def evaluate_from(provider: DataProvider, *, policy: AttributionPolicy | None = None) -> EvaluationResult:
    """Load through the provider's read-only load() and evaluate. The evaluator itself never touches storage."""
    data = await provider.load()
    return evaluate(data.decisions, data.outcomes, interruptions=data.interruptions, policy=policy,
                    unreadable=data.unreadable, notes=data.notes)
