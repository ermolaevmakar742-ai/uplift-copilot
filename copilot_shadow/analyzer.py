"""Builds a PII-free state snapshot from data production already computed.

Pure and synchronous; reads only. Never stores message text, names,
usernames, phone/email, OSINT raw data or memory contents.
"""
from typing import Any

ANALYZER_VERSION = "snapshot-1"

_TRUE_RISK = ("self_exclusion", "minor")


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def build_snapshot(user: Any, client_state: dict | None, history: list | None,
                   regime: str | None) -> dict:
    cs = client_state if isinstance(client_state, dict) else {}
    conv = cs.get("conversation") or {}
    risk = cs.get("risk") or {}
    ident = cs.get("identity") or {}
    region = ident.get("region")
    return {
        "stage": (getattr(user, "stage", None) or cs.get("stage") or "new"),
        "production_regime": regime,
        "intent": conv.get("intent", "unknown"),
        "sentiment": _num(conv.get("sentiment")),
        "complexity": _num(conv.get("complexity")),
        "messages_count": len(history) if history is not None else conv.get("messages_count"),
        "region": region if isinstance(region, str) and len(region) <= 3 else None,
        "memory_keys": len(cs.get("memory") or {}),
        "risk": {k: bool(risk.get(k, False)) for k in _TRUE_RISK},
        "manual_mode": bool(getattr(user, "manual_mode", False)),
        "blocked": bool(getattr(user, "blocked", False)),
        "client_state_available": bool(cs),
    }
