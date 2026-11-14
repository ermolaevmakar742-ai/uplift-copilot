"""response_boundary.v1: immutable, strict. Pure (stdlib + copilot_evidence.contracts helpers); never reads a clock.

One record per Telegram message actually sent for a production reply (a reply may be several parts).
  status SENT    the Telegram API call succeeded. telegram_message_id is the real id of the sent message (int > 0).
                 This is the only status that is a response boundary for attribution.
  status FAILED  the send raised. telegram_message_id is None (nothing was sent). NEVER a response boundary:
                 `is_boundary` is False. `sent_at` then means "when the failure was observed".
  sent_at        timezone-aware instant of the event, supplied by the caller (never invented here).
  decision_id    the Production Decision Ledger id ("pd_..."), or None. None = UNLINKED: the send path had no decision
                 (nothing is looked up or guessed). metadata.linkage says so explicitly ("decision" | "unlinked").
  metadata       must carry timestamp_source (where sent_at came from); typically also part_index / parts_total and,
                 when Telegram returned it, telegram_date (kept for cross-checks, not used as sent_at).
response_boundary_id is a deterministic content hash (SENT: chat + Telegram message id; FAILED: chat, decision, part, time).
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from copilot_evidence.contracts import ContractError, _dt, _enum, _freeze, _int, _thaw

CONTRACT_VERSION = "response_boundary.v1"
DECISION_ID_PREFIX = "pd_"
LINKAGES = ("decision", "unlinked")


class BoundaryStatus(str, Enum):
    SENT = "SENT"
    FAILED = "FAILED"


def compute_id(status: str, chat_id: int, telegram_message_id, decision_id, part_index, sent_at_iso: str) -> str:
    if status == "SENT":
        raw = ["SENT", chat_id, telegram_message_id]
    else:
        raw = ["FAILED", chat_id, decision_id, part_index, sent_at_iso]
    return "rb_" + hashlib.sha256(json.dumps([CONTRACT_VERSION] + raw, separators=(",", ":")).encode()).hexdigest()[:24]


@dataclass(frozen=True)
class ResponseBoundary:
    response_boundary_id: str
    decision_id: str | None
    chat_id: int
    telegram_message_id: int | None
    sent_at: datetime
    status: BoundaryStatus
    metadata: Mapping[str, Any]
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self):
        s = object.__setattr__
        st = _enum(BoundaryStatus, "status", self.status)
        s(self, "status", st)
        d = self.decision_id
        if d is not None and not (isinstance(d, str) and d.startswith(DECISION_ID_PREFIX) and len(d) > len(DECISION_ID_PREFIX)):
            raise ContractError("decision_id must be None or a Ledger id ('pd_...')")
        s(self, "chat_id", _int("chat_id", self.chat_id))
        s(self, "sent_at", _dt("sent_at", self.sent_at))
        mid = self.telegram_message_id
        if st is BoundaryStatus.SENT:
            if isinstance(mid, bool) or not isinstance(mid, int) or mid <= 0:
                raise ContractError("a SENT boundary needs the real Telegram message id (int > 0)")
        elif mid is not None:
            raise ContractError("a FAILED record has no Telegram message id")
        if self.contract_version != CONTRACT_VERSION:
            raise ContractError(f"unsupported contract_version: {self.contract_version!r}")
        meta = _freeze(self.metadata)
        if not isinstance(meta, Mapping):
            raise ContractError("metadata must be a mapping")
        s(self, "metadata", meta)
        src = meta.get("timestamp_source")
        if not (isinstance(src, str) and src.strip()):
            raise ContractError("metadata.timestamp_source is required (where sent_at came from)")
        if meta.get("linkage") != ("unlinked" if d is None else "decision"):
            raise ContractError("metadata.linkage must be 'unlinked' without decision_id and 'decision' with one")
        pi, pt = meta.get("part_index"), meta.get("parts_total")
        if (pi is None) != (pt is None):
            raise ContractError("part_index and parts_total go together")
        if pi is not None and (isinstance(pi, bool) or isinstance(pt, bool) or not isinstance(pi, int)
                               or not isinstance(pt, int) or not 0 <= pi < pt):
            raise ContractError("need 0 <= part_index < parts_total")
        expected = compute_id(st.value, self.chat_id, mid, d, pi, self.sent_at.isoformat())
        if self.response_boundary_id != expected:
            raise ContractError("response_boundary_id does not match content")

    @property
    def is_boundary(self) -> bool:
        """True only for SENT: a FAILED record is never a response boundary."""
        return self.status is BoundaryStatus.SENT

    def to_dict(self) -> dict:
        return {"contract_version": self.contract_version, "response_boundary_id": self.response_boundary_id,
                "decision_id": self.decision_id, "chat_id": self.chat_id,
                "telegram_message_id": self.telegram_message_id, "sent_at": self.sent_at.isoformat(),
                "status": self.status.value, "metadata": _thaw(self.metadata)}

    @classmethod
    def from_dict(cls, d: Mapping) -> "ResponseBoundary":
        if not isinstance(d, Mapping):
            raise ContractError("response boundary must be a mapping")
        keys = ("contract_version", "response_boundary_id", "decision_id", "chat_id", "telegram_message_id",
                "sent_at", "status", "metadata")
        missing, extra = [k for k in keys if k not in d], sorted(set(d) - set(keys))
        if missing or extra:
            raise ContractError(f"response boundary: missing={missing} unknown={extra}")
        return cls(response_boundary_id=d["response_boundary_id"], decision_id=d["decision_id"], chat_id=d["chat_id"],
                   telegram_message_id=d["telegram_message_id"], sent_at=d["sent_at"], status=d["status"],
                   metadata=d["metadata"], contract_version=d["contract_version"])
