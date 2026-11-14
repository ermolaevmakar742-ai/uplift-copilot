"""state_delta.v1. Immutable, strict, deterministic ids. Pure (stdlib + copilot_evidence.contracts helpers).

StateSnapshot: the five comparable fields of the client state as production records them
(stage, intent, sentiment, region, messages = messages_count). A field that is not known is None.
`from_client_state` applies the SAME "no information" rules as the Evidence adapter: intent "unknown" (empty text) is None,
and the default sentiment that goes with it is None too; region "?"/"??" is None; blank stage is None.

FieldDelta status per field:
  UNCHANGED  both values known and equal        CHANGED  both known and different
  UNKNOWN    EITHER side unknown (missing before + existing after is UNKNOWN, never UNCHANGED)
Snapshots are copied into frozen values at construction: later mutation of the source dicts cannot change a delta.
"""
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from copilot_evidence.contracts import ContractError, _dt, _enum, _freeze, _int, _thaw

CONTRACT_VERSION = "state_delta.v1"
FIELDS = ("stage", "intent", "sentiment", "region", "messages")


class FieldStatus(str, Enum):
    UNCHANGED = "UNCHANGED"
    CHANGED = "CHANGED"
    UNKNOWN = "UNKNOWN"


def _text(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


@dataclass(frozen=True)
class StateSnapshot:
    stage: str | None = None
    intent: str | None = None
    sentiment: float | None = None
    region: str | None = None
    messages: int | None = None
    observed_at: datetime | None = None

    def __post_init__(self):
        s = object.__setattr__
        for f in ("stage", "intent", "region"):
            v = getattr(self, f)
            if v is not None and not (isinstance(v, str) and v.strip()):
                raise ContractError(f"{f} must be a non-empty str or None")
        sent = self.sentiment
        if sent is not None and (isinstance(sent, bool) or not isinstance(sent, (int, float)) or not math.isfinite(sent)):
            raise ContractError("sentiment must be a finite number or None")
        m = self.messages
        if m is not None and (isinstance(m, bool) or not isinstance(m, int) or m < 0):
            raise ContractError("messages must be an int >= 0 or None")
        s(self, "observed_at", _dt("observed_at", self.observed_at, optional=True))

    @classmethod
    def from_client_state(cls, state: Mapping | None, observed_at: datetime | None = None) -> "StateSnapshot":
        if not isinstance(state, Mapping):
            return cls(observed_at=observed_at)

        def g(flat, *path):
            cur: Any = state
            for k in path:
                cur = cur.get(k) if isinstance(cur, Mapping) else None
            return cur if cur is not None else state.get(flat)
        intent = _text(g("intent", "conversation", "intent"))
        intent = None if intent == "unknown" else intent
        sent = g("sentiment", "conversation", "sentiment")
        sent = sent if (intent is not None and isinstance(sent, (int, float)) and not isinstance(sent, bool)
                        and math.isfinite(sent)) else None
        region = _text(g("region", "identity", "region"))
        region = None if region in ("?", "??") else region
        msgs = g("messages_count", "conversation", "messages_count")
        msgs = msgs if isinstance(msgs, int) and not isinstance(msgs, bool) and msgs >= 0 else None
        return cls(_text(g("stage", "stage")), intent, sent, region, msgs, observed_at)

    def to_dict(self) -> dict:
        return {"stage": self.stage, "intent": self.intent, "sentiment": self.sentiment, "region": self.region,
                "messages": self.messages, "observed_at": self.observed_at.isoformat() if self.observed_at else None}

    @classmethod
    def from_dict(cls, d: Mapping) -> "StateSnapshot":
        keys = FIELDS + ("observed_at",)
        if not isinstance(d, Mapping) or set(d) != set(keys):
            raise ContractError("snapshot: exact keys required")
        return cls(d["stage"], d["intent"], d["sentiment"], d["region"], d["messages"], d["observed_at"])


@dataclass(frozen=True)
class FieldDelta:
    field: str
    status: FieldStatus
    before: Any
    after: Any

    def __post_init__(self):
        if self.field not in FIELDS:
            raise ContractError(f"unknown field {self.field!r}")
        object.__setattr__(self, "status", _enum(FieldStatus, "status", self.status))

    def to_dict(self) -> dict:
        return {"field": self.field, "status": self.status.value, "before": self.before, "after": self.after}


def compare(before: Any, after: Any) -> FieldStatus:
    if before is None or after is None:
        return FieldStatus.UNKNOWN
    return FieldStatus.UNCHANGED if before == after else FieldStatus.CHANGED


def compute_id(chat_id: int, anchor: str | None, before: dict, after: dict) -> str:
    raw = json.dumps([CONTRACT_VERSION, chat_id, anchor, before, after], sort_keys=True, separators=(",", ":"))
    return "sd_" + hashlib.sha256(raw.encode()).hexdigest()[:20]


@dataclass(frozen=True)
class StateDelta:
    delta_id: str
    chat_id: int
    anchor_decision_id: str | None
    before: StateSnapshot
    after: StateSnapshot
    fields: tuple[FieldDelta, ...]
    metadata: Mapping[str, Any]
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self):
        s = object.__setattr__
        s(self, "chat_id", _int("chat_id", self.chat_id))
        if self.anchor_decision_id is not None and not (isinstance(self.anchor_decision_id, str) and self.anchor_decision_id):
            raise ContractError("anchor_decision_id must be a non-empty str or None")
        if not isinstance(self.before, StateSnapshot) or not isinstance(self.after, StateSnapshot):
            raise ContractError("before/after must be StateSnapshot")
        if self.before.observed_at and self.after.observed_at and not self.after.observed_at > self.before.observed_at:
            raise ContractError("the 'after' state must be observed strictly later than the 'before' state")
        if self.contract_version != CONTRACT_VERSION:
            raise ContractError(f"unsupported contract_version: {self.contract_version!r}")
        s(self, "metadata", _freeze(self.metadata))
        if not isinstance(self.metadata, Mapping):
            raise ContractError("metadata must be a mapping")
        expected = tuple(FieldDelta(f, compare(getattr(self.before, f), getattr(self.after, f)),
                                    getattr(self.before, f), getattr(self.after, f)) for f in FIELDS)
        if tuple(self.fields) != expected:
            raise ContractError("fields do not match the snapshots (statuses are derived, not supplied)")
        s(self, "fields", expected)
        if self.delta_id != compute_id(self.chat_id, self.anchor_decision_id, self.before.to_dict(), self.after.to_dict()):
            raise ContractError("delta_id does not match content")

    def field(self, name: str) -> FieldDelta:
        return next(f for f in self.fields if f.field == name)

    def to_dict(self) -> dict:
        return {"contract_version": self.contract_version, "delta_id": self.delta_id, "chat_id": self.chat_id,
                "anchor_decision_id": self.anchor_decision_id, "before": self.before.to_dict(),
                "after": self.after.to_dict(), "fields": [f.to_dict() for f in self.fields],
                "metadata": _thaw(self.metadata)}

    @classmethod
    def from_dict(cls, d: Mapping) -> "StateDelta":
        keys = ("contract_version", "delta_id", "chat_id", "anchor_decision_id", "before", "after", "fields", "metadata")
        if not isinstance(d, Mapping):
            raise ContractError("state delta must be a mapping")
        missing, extra = [k for k in keys if k not in d], sorted(set(d) - set(keys))
        if missing or extra:
            raise ContractError(f"state delta: missing={missing} unknown={extra}")
        if not isinstance(d["fields"], (list, tuple)):
            raise ContractError("fields must be a list")
        fields = []
        for f in d["fields"]:
            if not isinstance(f, Mapping) or set(f) != {"field", "status", "before", "after"}:
                raise ContractError("field delta: exact keys required")
            fields.append(FieldDelta(f["field"], f["status"], f["before"], f["after"]))
        return cls(d["delta_id"], d["chat_id"], d["anchor_decision_id"], StateSnapshot.from_dict(d["before"]),
                   StateSnapshot.from_dict(d["after"]), tuple(fields), d["metadata"], d["contract_version"])
