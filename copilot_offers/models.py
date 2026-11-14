"""Account Offer Manager contracts. Plain data, no I/O. Partner/admin free text never enters a decision."""
import uuid
from dataclasses import dataclass, asdict, field
from enum import Enum

POLICY_VERSION = "account_offer_v1"
MODE_SHADOW = "shadow"


class OfferStatus(str, Enum):
    NO_OFFER_ASSIGNED = "NO_OFFER_ASSIGNED"      # the account has no assignment
    OFFER_ASSIGNED = "OFFER_ASSIGNED"            # assigned, active and fully validated
    OFFER_UNAVAILABLE = "OFFER_UNAVAILABLE"      # assigned, but the offer cannot be used right now
    ACCOUNT_UNKNOWN = "ACCOUNT_UNKNOWN"          # runtime account identity not known yet


class OfferReason(str, Enum):
    OFFER_ASSIGNED = "OFFER_ASSIGNED"
    NO_ASSIGNMENT = "NO_ASSIGNMENT"
    ACCOUNT_UNKNOWN = "ACCOUNT_UNKNOWN"
    PROGRAM_NOT_FOUND = "PROGRAM_NOT_FOUND"
    INACTIVE = "INACTIVE"
    INVALID = "INVALID"                          # failed offer validation (URL, name, description, promo, images)


def new_offer_decision_id() -> str:
    return "od_" + uuid.uuid4().hex


@dataclass(frozen=True)
class OfferInput:
    correlation_id: str          # production decision_id (pd_...)
    client_ref: str              # pseudonym (cli_...), never a raw chat id
    account_ref: str | None      # pseudonym of the Telegram account, never a token / raw account id
    stage_raw: str | None = None  # observability only (no gating)


@dataclass(frozen=True)
class OfferDecisionResult:
    offer_decision_id: str
    correlation_id: str
    client_ref: str
    account_ref: str | None
    program_id: str | None
    status: OfferStatus
    reason: OfferReason
    policy_version: str
    stage_canonical: str
    stage_raw: str | None
    mode: str

    def to_record(self) -> dict:
        d = asdict(self)
        d["status"], d["reason"] = self.status.value, self.reason.value
        return d


@dataclass(frozen=True)
class OfferImage:
    image_id: str
    url: str
    sort_order: int
    active: bool = True


@dataclass(frozen=True)
class OfferContext:
    """FUTURE structured contract (NOT passed to any LLM or sent to any client in this version). Offer data is DATA, never
    instructions: there is deliberately no terms / notes / arbitrary-admin-text field."""
    offer_id: str
    account_ref: str
    name: str
    brand: str
    description: str | None
    registration_url: str
    promo_code: str | None
    images: tuple[OfferImage, ...] = field(default_factory=tuple)
    active: bool = True


@dataclass(frozen=True)
class OfferResolution:
    """Typed runtime result of account -> assigned offer. `context` exists only for OFFER_ASSIGNED and is excluded from repr so it
    cannot leak into a log line by accident. It stays inside the runtime/shadow layer (never prompt, never Telegram)."""
    status: OfferStatus
    reason: OfferReason
    program_id: str | None = None
    context: OfferContext | None = field(default=None, repr=False, compare=False)
