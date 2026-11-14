"""Dispatcher boundary between COPILOT (decides) and n8n (orchestrates). Only the mock exists at this stage."""
import hashlib
import hmac
import json
import logging
from typing import Protocol

logger = logging.getLogger("copilot_voice")

SIGNATURE_HEADER = "X-COPILOT-Signature"      # same scheme as Traffic OS -> n8n: sha256=HMAC(secret, raw body)


def sign_body(secret: str, raw_body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def canonical_body(job: dict) -> bytes:
    return json.dumps(job, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class VoiceJobDispatcher(Protocol):
    def dispatch(self, job: dict) -> dict: ...


class MockVoiceJobDispatcher:
    """Logs only. No network, no audio, no Telegram."""

    def dispatch(self, job: dict) -> dict:
        logger.info("VOICE_JOB_CREATED %s", json.dumps({
            "decision_id": job.get("decision_id"), "chat": job.get("chat_id"),
            "correlation_id": job.get("correlation_id"), "reason": job.get("reason"),
            "score": job.get("score"), "text_length": len(job.get("text") or ""), "dry_run": True,
        }, ensure_ascii=False))
        return {"dispatched": False, "mode": "mock"}
