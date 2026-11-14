"""Contracts. A secret VALUE exists only inside SecretValue (masked repr, not picklable / serialisable); everything else is metadata."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol


class Slot(str, Enum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    EMERGENCY = "EMERGENCY"


class Status(str, Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"      # reversible: ciphertext kept, no runtime access
    REVOKED = "REVOKED"        # terminal: replaced by rotation; ciphertext erased
    DELETED = "DELETED"        # terminal: removed by an admin; ciphertext erased


class Operation(str, Enum):
    KEY_CREATED = "KEY_CREATED"
    KEY_UPDATED = "KEY_UPDATED"
    KEY_DELETED = "KEY_DELETED"
    KEY_TESTED = "KEY_TESTED"
    KEY_ACTIVATED = "KEY_ACTIVATED"
    KEY_DISABLED = "KEY_DISABLED"


class TestResult(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    DRY_RUN = "DRY_RUN"


class SecretError(Exception):
    """Carries only a safe machine code; never a value."""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SecretValue:
    """The plaintext key for runtime use. repr/str/format are masked; it cannot be pickled, copied or JSON-serialised by accident."""
    __slots__ = ("_v",)

    def __init__(self, value: str):
        object.__setattr__(self, "_v", value)

    def reveal(self) -> str:
        return self._v

    def __setattr__(self, *_):
        raise AttributeError("immutable")

    def __repr__(self) -> str:
        return "<SecretValue ***>"

    __str__ = __repr__

    def __format__(self, spec) -> str:
        return "<SecretValue ***>"

    def __reduce__(self):
        raise TypeError("SecretValue cannot be serialised")

    def __copy__(self):
        raise TypeError("SecretValue cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("SecretValue cannot be copied")

    def __bool__(self):
        return bool(self._v)

    def __eq__(self, other):
        return NotImplemented

    __hash__ = None


@dataclass(frozen=True)
class SecretMetadata:
    """Everything the UI / API may see. No value, no ciphertext."""
    secret_id: str
    slot: str
    provider: str
    key_slot: str
    masked: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime
    last_tested_at: datetime | None
    last_test_result: str | None

    def to_dict(self) -> dict:
        return {"secret_id": self.secret_id, "slot": self.slot, "provider": self.provider, "key_slot": self.key_slot, "masked": self.masked, "status": self.status,
                "version": self.version, "created_at": self.created_at.isoformat(), "updated_at": self.updated_at.isoformat(),
                "last_tested_at": self.last_tested_at.isoformat() if self.last_tested_at else None, "last_test_result": self.last_test_result}


class LLMSecretProvider(Protocol):
    """The interface the future LLM Router depends on (instead of reading .env)."""
    async def get_secret_for_runtime(self, slot: Slot) -> "SecretValue | None": ...
