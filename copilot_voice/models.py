"""Voice Decision contracts: plain, serialisable, no I/O."""
import uuid
from dataclasses import dataclass, asdict, field
from enum import Enum

POLICY_VERSION = "voice_policy_v1"
DEFAULT_VOICE_PROFILE = "valeria_default"


class VoiceReason(str, Enum):
    CLIENT_REQUESTED_VOICE = "client_requested_voice"
    EMOTIONAL_EXPLANATION = "emotional_explanation"
    COMPLEX_EXPLANATION = "complex_explanation"
    PERSUASION = "persuasion"
    OBJECTION_HANDLING = "objection_handling"
    RAPPORT = "rapport"
    NATURAL_VOICE_PREFERRED = "natural_voice_preferred"
    LONG_TEXT = "long_text"
    OTHER = "other"


class VoicePriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


def new_decision_id() -> str:
    return "vd_" + uuid.uuid4().hex


@dataclass(frozen=True)
class VoiceContext:
    """Everything the policy may look at. Built by the caller from data it already has."""
    chat_id: object
    reply_text: str
    inbound_text: str = ""
    regime: str = ""
    stage: str = ""
    correlation_id: str = ""          # production decision_id when available (links to ledger/shadow/outcomes)
    is_service: bool = False          # technical / service scenario -> never voice
    seconds_since_last_voice: float | None = None   # None = no voice sent recently / unknown


@dataclass(frozen=True)
class VoiceDecisionResult:
    should_send_voice: bool
    score: float
    reason: VoiceReason
    priority: VoicePriority
    voice_profile: str
    text: str
    chat_id: str
    correlation_id: str
    decision_id: str
    policy_version: str = POLICY_VERSION

    def to_job(self) -> dict:
        d = asdict(self)
        d["reason"] = self.reason.value
        d["priority"] = self.priority.value
        return d

    def to_event(self) -> dict:
        """Log-safe view: no text, only its length; chat id is hashed by the caller."""
        d = self.to_job()
        d["text_length"] = len(d.pop("text"))
        return d
