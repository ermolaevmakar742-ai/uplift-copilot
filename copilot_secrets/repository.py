"""Database access for llm_secrets. Values are only ever ciphertext here; this layer never sees a plaintext key."""
from datetime import datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from copilot_secrets.contracts import SecretMetadata, Status
from copilot_secrets.store import llm_secrets as t

_META_COLS = (t.c.secret_id, t.c.slot, t.c.provider, t.c.key_slot, t.c.masked, t.c.status, t.c.version, t.c.created_at, t.c.updated_at,
              t.c.last_tested_at, t.c.last_test_result)       # NOTE: encrypted_value is deliberately not selectable through metadata


def _meta(r) -> SecretMetadata:
    return SecretMetadata(r["secret_id"], r["slot"], r["provider"], r["key_slot"], r["masked"], r["status"], r["version"], r["created_at"], r["updated_at"],
                          r["last_tested_at"], r["last_test_result"])


async def current(s: AsyncSession, slot: str) -> SecretMetadata | None:
    """The live row of a slot: ACTIVE, else DISABLED (the newest non-terminal version)."""
    r = (await s.execute(select(*_META_COLS).where(t.c.slot == slot, t.c.status.in_((Status.ACTIVE.value, Status.DISABLED.value)))
                         .order_by(t.c.version.desc()).limit(1))).mappings().first()
    return _meta(r) if r else None


async def latest(s: AsyncSession, slot: str) -> SecretMetadata | None:
    r = (await s.execute(select(*_META_COLS).where(t.c.slot == slot).order_by(t.c.version.desc()).limit(1))).mappings().first()
    return _meta(r) if r else None


async def next_version(s: AsyncSession, slot: str) -> int:
    return ((await s.execute(select(func.max(t.c.version)).where(t.c.slot == slot))).scalar() or 0) + 1


async def revoke_and_erase(s: AsyncSession, slot: str, new_status: Status, now: datetime) -> None:
    """Terminal state + crypto-erase for every non-terminal version of the slot."""
    await s.execute(update(t).where(t.c.slot == slot, t.c.status.in_((Status.ACTIVE.value, Status.DISABLED.value)))
                    .values(status=new_status.value, encrypted_value=None, updated_at=now))


async def insert_active(s: AsyncSession, *, secret_id, slot, version, provider, key_slot, encrypted, masked, now) -> None:
    await s.execute(insert(t).values(secret_id=secret_id, slot=slot, version=version, provider=provider, key_slot=key_slot, encrypted_value=encrypted, masked=masked,
                                     status=Status.ACTIVE.value, created_at=now, updated_at=now))


async def set_status(s: AsyncSession, secret_id: str, status: Status, now: datetime) -> None:
    await s.execute(update(t).where(t.c.secret_id == secret_id).values(status=status.value, updated_at=now))


async def record_test(s: AsyncSession, secret_id: str, result: str, now: datetime) -> None:
    await s.execute(update(t).where(t.c.secret_id == secret_id).values(last_tested_at=now, last_test_result=result))


async def ciphertext(s: AsyncSession, slot: str) -> tuple[int, str] | None:
    """(version, token) of the ACTIVE row only; the single place a ciphertext is read."""
    r = (await s.execute(select(t.c.version, t.c.encrypted_value).where(t.c.slot == slot, t.c.status == Status.ACTIVE.value))).first()
    return (r[0], r[1]) if r and r[1] else None
