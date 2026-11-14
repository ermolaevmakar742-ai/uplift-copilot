"""Audit trail for key operations: an append-only table plus (optionally) the panel's existing audit_log. An event carries timestamp, actor, slot, provider,
operation, success and a SAFE reason code; it can never carry a key. Writing an audit event never raises into the caller."""
import logging
import secrets as _secrets
from datetime import datetime
from typing import Callable

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from copilot_secrets.contracts import Operation
from copilot_secrets.store import llm_secret_audit

logger = logging.getLogger("copilot_secrets")
REASON_MAX = 120


def new_event_id() -> str:
    return "sa_" + _secrets.token_hex(10)


async def record(session: AsyncSession | None, session_factory, *, now: datetime, actor: str, slot: str, provider: str | None, op: Operation,
                 success: bool, reason: str | None, version: int | None, audit_log: Callable | None = None) -> None:
    """Uses the caller's session when given (same transaction as the change); otherwise its own."""
    row = dict(event_id=new_event_id(), created_at=now, actor=actor[:64], key_slot=slot, provider=provider, operation=op.value, success=bool(success),
               reason=(reason or None) and reason[:REASON_MAX], version=version)
    try:
        if session is not None:
            await session.execute(insert(llm_secret_audit).values(**row))
        else:
            async with session_factory() as s:
                await s.execute(insert(llm_secret_audit).values(**row))
                await s.commit()
    except Exception as e:
        logger.error("secret audit write failed slot=%s op=%s: %s", slot, op.value, type(e).__name__)
    logger.info("LLM_KEY op=%s slot=%s success=%s reason=%s", op.value, slot, success, row["reason"])
    if audit_log is not None:
        try:
            await audit_log("llm_key_" + op.value.lower(), ip=None, details=f"slot={slot} success={success} reason={row['reason']} version={version}")
        except Exception:
            logger.warning("panel audit_log failed op=%s", op.value)
