"""Reward Resolver: an ALREADY OBSERVED outcome -> (reward, reward_version, reward_reason). Versioned, pure.

There is NO agreed reward semantics for COPILOT yet (no economic scale for registration / payment / deposit). So this version
resolves NOTHING to a number: every reward is None with an explicit reason. In particular CONTINUED (the client wrote again)
is never a conversion and never equals DEPOSIT. To define a reward, add a NEW version with an explicit table (see
`Resolver(table, version)`); old rows keep the version they were written with. Unobserved/unknown outcomes never get a reward."""
from dataclasses import dataclass, field
from types import MappingProxyType

NEUTRAL_VERSION = "reward.v0-undefined"
OUTCOME_TYPES = ("DEPOSIT", "REGISTRATION", "PAYMENT_ATTEMPT", "PAYMENT_SUCCESS", "PAYMENT_FAILED", "HUMAN_HANDOFF",
                 "DROPPED", "CONTINUED", "UNKNOWN")
# TODO(product decision): agree a scale, e.g. DEPOSIT / PAYMENT_SUCCESS > REGISTRATION > ... ; until then empty.
NEUTRAL_TABLE = MappingProxyType({})


@dataclass(frozen=True)
class Resolver:
    table: dict = field(default_factory=dict)       # outcome_type -> reward in [0, 1]
    version: str = NEUTRAL_VERSION

    def __post_init__(self):
        for k, v in self.table.items():
            if k not in OUTCOME_TYPES or k == "UNKNOWN" or isinstance(v, bool) or not isinstance(v, (int, float)) \
               or not 0.0 <= v <= 1.0:
                raise ValueError(f"invalid reward table entry {k!r}")
        if self.table and self.version == NEUTRAL_VERSION:
            raise ValueError("a non-empty reward table needs its own version")

    def resolve(self, outcome_type) -> dict:
        ot = outcome_type.value if hasattr(outcome_type, "value") else outcome_type
        if ot is None or ot == "UNKNOWN" or ot not in OUTCOME_TYPES:
            reason, reward = "outcome_unknown", None
        elif ot in self.table:
            reason, reward = "table:" + ot, float(self.table[ot])
        elif ot == "CONTINUED":
            reason, reward = "continued_is_not_a_conversion", None
        else:
            reason, reward = "reward_undefined_for:" + ot, None
        return {"reward": reward, "reward_version": self.version, "reward_reason": reason}


DEFAULT_RESOLVER = Resolver()


def resolve(outcome_type) -> dict:
    return DEFAULT_RESOLVER.resolve(outcome_type)


# ======================================================================================================================
# reward.v1: staged, auditable, versioned. BASE OUTCOME + TRAJECTORY MODIFIERS + NEGATIVE OUTCOMES. Pure.
#
# Event audit (what COPILOT really observes today):
#   deposit_success (postback, amount) -> DEPOSIT          register_complete (postback) -> REGISTRATION
#   deposit_attempt (postback)         -> PAYMENT_ATTEMPT  funnel 'lost' (admin)        -> DROPPED
#   client message (telegram)          -> CONTINUED        nothing at all in the window  -> silence (outcome_status expired)
#   NOT present as events: chargeback, refund, complaint, block-as-event (User.blocked is a bare flag with no time),
#   self-exclusion (client_state says User has no such column), PAYMENT_SUCCESS/FAILED, withdraw (no outcome type).
# Missing sources are reported with *_DATA_UNAVAILABLE flags; no event is ever invented.
# ======================================================================================================================
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

REWARD_V1 = "reward.v1"
MONEY_OUTCOMES = ("DEPOSIT", "PAYMENT_SUCCESS")
DELAYED_WINDOW_SECONDS = 30 * 86400            # chargeback / refund can arrive up to 30 days after the money
NEGATIVE_KINDS = ("CHARGEBACK", "REFUND", "COMPLAINT", "BLOCK")
# which negative-event sources physically exist in COPILOT data today
DATA_AVAILABILITY = {"CHARGEBACK": False, "REFUND": False, "COMPLAINT": False,
                     "BLOCK": False}           # BLOCK: User.blocked is state-only (no event time) -> not usable as an event


@dataclass(frozen=True)
class RewardPolicy:
    """All economics are explicit parameters, none tuned on data. Defaults are NEUTRAL placeholders (no decay, no touch
    penalty, no complaint/block penalty, 1:1 chargeback reversal) until product confirms values (params_confirmed)."""
    version: str = REWARD_V1
    base_rewards: dict = field(default_factory=lambda: {"DEPOSIT": 1.0, "PAYMENT_SUCCESS": 1.0, "CONTINUED": 0.0,
                                                        "DROPPED": 0.0})
    decay: float = 1.0                                  # reward *= decay ** (touches - 1), positive base only
    max_touches_before_penalty: int | None = None       # None = touch penalty disabled
    penalty_per_extra_touch: float = 0.0
    chargeback_multiplier: float = 1.0                  # reward -= (amount / amount_unit) * multiplier  (also refunds)
    amount_unit: float | None = None                    # currency units per 1.0 of reward; None = amounts cannot be scaled
    complaint_penalty: float = 0.0
    block_penalty: float = 0.0
    delayed_window_seconds: int = DELAYED_WINDOW_SECONDS
    reward_min: float = -1.0
    reward_max: float = 1.0
    params_confirmed: bool = False

    def __post_init__(self):
        def num(v):
            return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        for k, v in self.base_rewards.items():
            if k not in OUTCOME_TYPES or k == "UNKNOWN" or not num(v) or not self.reward_min <= v <= self.reward_max:
                raise ValueError(f"invalid base reward {k!r}")
        if not (num(self.decay) and 0 < self.decay <= 1):
            raise ValueError("decay must be in (0, 1]")
        if self.max_touches_before_penalty is not None and (
                isinstance(self.max_touches_before_penalty, bool) or not isinstance(self.max_touches_before_penalty, int)
                or self.max_touches_before_penalty < 0):
            raise ValueError("max_touches_before_penalty must be a non-negative int or None")
        for k in ("penalty_per_extra_touch", "chargeback_multiplier", "complaint_penalty", "block_penalty"):
            if not num(getattr(self, k)) or getattr(self, k) < 0:
                raise ValueError(f"{k} must be a non-negative number")
        if self.amount_unit is not None and not (num(self.amount_unit) and self.amount_unit > 0):
            raise ValueError("amount_unit must be > 0 or None")
        if not (isinstance(self.delayed_window_seconds, int) and self.delayed_window_seconds > 0):
            raise ValueError("delayed_window_seconds must be a positive int")
        if not (num(self.reward_min) and num(self.reward_max) and self.reward_min < self.reward_max):
            raise ValueError("invalid reward range")

    def params(self) -> dict:
        return {"base_rewards": dict(sorted(self.base_rewards.items())), "decay": self.decay,
                "max_touches_before_penalty": self.max_touches_before_penalty,
                "penalty_per_extra_touch": self.penalty_per_extra_touch,
                "chargeback_multiplier": self.chargeback_multiplier, "amount_unit": self.amount_unit,
                "complaint_penalty": self.complaint_penalty, "block_penalty": self.block_penalty,
                "delayed_window_seconds": self.delayed_window_seconds, "reward_min": self.reward_min,
                "reward_max": self.reward_max, "params_confirmed": self.params_confirmed}

    def params_id(self) -> str:
        return "rp_" + hashlib.sha1(json.dumps([self.version, self.params()], sort_keys=True).encode()).hexdigest()[:12]


DEFAULT_POLICY_V1 = RewardPolicy()


@dataclass(frozen=True)
class DelayedEvent:
    """A negative event observed after the outcome (future data source; none exists today)."""
    kind: str                     # CHARGEBACK | REFUND | COMPLAINT | BLOCK
    observed_at: datetime
    event_id: str
    amount: float | None = None   # money events only

    def __post_init__(self):
        if self.kind not in NEGATIVE_KINDS or not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("invalid delayed event")
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True)
class RewardInput:
    outcome_status: str                       # observed | expired (silence) | unknown | pending
    outcome_type: str | None = None
    decision_at: datetime | None = None
    outcome_at: datetime | None = None
    touches: int | None = None                # assistant messages the lead received up to the outcome (None = unknown)
    amount: float | None = None               # money amount of the outcome event (informational audit trail)
    delayed_events: tuple = ()
    now: datetime | None = None               # injected clock (provisional/final); never read from the system here


@dataclass(frozen=True)
class RewardResult:
    reward: float | None
    reward_version: str
    outcome: str | None
    base_reward: float | None
    touches: int | None
    touch_penalty: float
    touch_decay_factor: float
    negative_adjustments: tuple
    time_to_outcome_seconds: float | None
    flags: tuple
    status: str                               # final | provisional | undefined
    params_id: str
    params: dict
    delayed_window: dict

    def to_dict(self) -> dict:
        return {"reward": self.reward, "reward_version": self.reward_version, "outcome": self.outcome,
                "base_reward": self.base_reward, "touches": self.touches, "touch_penalty": self.touch_penalty,
                "touch_decay_factor": self.touch_decay_factor,
                "negative_adjustments": [dict(a) for a in self.negative_adjustments],
                "time_to_outcome_seconds": self.time_to_outcome_seconds, "flags": list(self.flags),
                "status": self.status, "params_id": self.params_id, "params": self.params,
                "delayed_window": self.delayed_window}


def _aware(ts):
    return ts if isinstance(ts, datetime) and ts.tzinfo else None


def _negative_adjustments(policy, events, outcome_at, flags) -> tuple:
    out = []
    end = None if outcome_at is None else outcome_at + timedelta(seconds=policy.delayed_window_seconds)
    for ev in sorted(events or (), key=lambda e: (e.observed_at, e.event_id)):
        if outcome_at is None or ev.observed_at < outcome_at or ev.observed_at > end:
            flags.append("DELAYED_EVENT_OUTSIDE_WINDOW:" + ev.kind)
            continue
        if ev.kind in ("CHARGEBACK", "REFUND"):
            if policy.amount_unit is None or not isinstance(ev.amount, (int, float)) or isinstance(ev.amount, bool) \
               or ev.amount < 0:
                flags.append(ev.kind + "_AMOUNT_UNSCALED")
                out.append({"kind": ev.kind, "event_id": ev.event_id, "amount": ev.amount, "adjustment": None})
                continue
            adj = -round(ev.amount / policy.amount_unit * policy.chargeback_multiplier, 6)
        elif ev.kind == "COMPLAINT":
            adj = -policy.complaint_penalty
        else:
            adj = -policy.block_penalty
        out.append({"kind": ev.kind, "event_id": ev.event_id, "amount": ev.amount, "adjustment": adj})
    return tuple(out)


def resolve_v1(inp: RewardInput, policy: RewardPolicy = DEFAULT_POLICY_V1) -> RewardResult:
    """events/state -> RewardResult. Deterministic; no clock, DB or randomness. See the module notes above."""
    flags: list = []
    if not policy.params_confirmed:
        flags.append("POLICY_PARAMS_UNCONFIRMED")
    d_at, o_at = _aware(inp.decision_at), _aware(inp.outcome_at)
    tto = max(0.0, (o_at - d_at).total_seconds()) if d_at and o_at else None
    win = {"kind": "chargeback_refund", "seconds": policy.delayed_window_seconds,
           "closes_at": None if o_at is None else (o_at + timedelta(seconds=policy.delayed_window_seconds)).isoformat(),
           "sources_available": {k: bool(DATA_AVAILABILITY[k]) for k in NEGATIVE_KINDS}}

    def result(reward, status, base=None, pen=0.0, decay=1.0, neg=(), outcome=None):
        return RewardResult(reward, policy.version, outcome, base, inp.touches, pen, decay, tuple(neg), tto,
                            tuple(flags), status, policy.params_id(), policy.params(), win)

    st = inp.outcome_status
    if st == "pending":
        flags.append("OUTCOME_PENDING")
        return result(None, "undefined")
    if st == "unknown":
        flags.append("OUTCOME_UNKNOWN")          # coverage unproven: silence cannot be claimed either
        return result(None, "undefined")
    if st == "expired":
        flags.append("SILENCE_ZERO_REWARD")      # silence is exactly 0: never negative, no touch penalty
        return result(0.0, "final", base=0.0, outcome="SILENCE")
    if st != "observed":
        flags.append("OUTCOME_STATUS_INVALID")
        return result(None, "undefined")
    ot = inp.outcome_type
    if ot not in policy.base_rewards:
        flags.append("BASE_REWARD_UNDEFINED:" + str(ot))
        return result(None, "undefined", outcome=ot)
    base = float(policy.base_rewards[ot])
    touches = inp.touches if isinstance(inp.touches, int) and not isinstance(inp.touches, bool) and inp.touches >= 0 else None
    if touches is None:
        flags.append("TOUCHES_UNAVAILABLE")
    decay = policy.decay ** (max(touches, 1) - 1) if (touches is not None and base > 0) else 1.0
    pen = 0.0
    if policy.max_touches_before_penalty is not None and touches is not None:
        pen = policy.penalty_per_extra_touch * max(0, touches - policy.max_touches_before_penalty)
    for k in NEGATIVE_KINDS:
        if not DATA_AVAILABILITY[k]:
            flags.append(k + "_DATA_UNAVAILABLE")
    neg = _negative_adjustments(policy, inp.delayed_events, o_at, flags)
    total = base * decay - pen + sum(a["adjustment"] for a in neg if a["adjustment"] is not None)
    clamped = min(policy.reward_max, max(policy.reward_min, total))
    if clamped != total:
        flags.append("REWARD_CLAMPED")
    status = "final"
    if ot in MONEY_OUTCOMES:
        now = _aware(inp.now)
        if o_at is None or now is None or now < o_at + timedelta(seconds=policy.delayed_window_seconds):
            status = "provisional"
            flags.append("DELAYED_NEGATIVE_WINDOW_OPEN")
    return result(round(clamped, 6), status, base=base, pen=round(pen, 6), decay=round(decay, 6), neg=neg, outcome=ot)


def correct_v1(original: dict, delayed_events, policy: RewardPolicy = DEFAULT_POLICY_V1,
               applied_event_ids=()) -> dict | None:
    """Correction for an ALREADY stored reward (its detail dict) once delayed negative events arrive. Never rewrites the
    original: returns a new append-only record that references it, or None when nothing new applies. Uses the policy
    parameters the original was computed with unless a different `policy` is passed (then it is a new params_id).
    `original` is the stored reward_detail (carries reward, outcome_at, params_id); `applied_event_ids` are events already
    used by earlier stored corrections (idempotent)."""
    seen = {a["event_id"] for a in original.get("negative_adjustments", ()) if a.get("adjustment") is not None}
    seen |= set(applied_event_ids)
    fresh = tuple(e for e in delayed_events or () if e.event_id not in seen)
    if not fresh:
        return None
    o_at = _aware(datetime.fromisoformat(original["outcome_at"])) if original.get("outcome_at") else None
    flags: list = []
    neg = _negative_adjustments(policy, fresh, o_at, flags)
    delta = round(sum(a["adjustment"] for a in neg if a["adjustment"] is not None), 6)
    base_total = original.get("reward")
    corrected = None if base_total is None else round(min(policy.reward_max, max(policy.reward_min, base_total + delta)), 6)
    return {"supersedes_params_id": original.get("params_id"), "reward_version": policy.version,
            "params_id": policy.params_id(), "delta": delta, "corrected_reward": corrected,
            "negative_adjustments": [dict(a) for a in neg], "flags": flags,
            "event_ids": sorted(a["event_id"] for a in neg)}
