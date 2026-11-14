"""Secret service. Three deliberately separate read/write paths:
  set_secret(...)                -> metadata only (the value goes in, never comes back)
  get_secret_metadata(...)       -> SecretMetadata (masked, never a value)
  get_secret_for_runtime(...)    -> SecretValue | None   (the ONLY path that decrypts; for the future LLM router, never for the UI)
Errors are SecretError with a safe code. Nothing here logs, audits, raises or returns a key; with no master key, writes AND reads are disabled (no plaintext fallback)."""
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.exc import DBAPIError, IntegrityError

from copilot_secrets import audit, repository as repo
from copilot_secrets.contracts import Operation, SecretError, SecretMetadata, SecretValue, Slot, Status, TestResult
from copilot_secrets.credential_test import test_provider_credentials
from copilot_secrets.crypto import SecretBox

logger = logging.getLogger("copilot_secrets")
KEY_RE = re.compile(r"^[\x21-\x7E]{20,512}$")           # printable ASCII, no whitespace / control characters
PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
MASK_DOTS = "••••••••••••"
MIN_LEN_FOR_SUFFIX = 24                                  # a visible 4-character suffix of a long, high-entropy key is not a practical risk; shorter keys show none


def mask(value: str) -> str:
    return MASK_DOTS + value[-4:] if len(value) >= MIN_LEN_FOR_SUFFIX else MASK_DOTS


def validate_key(value) -> str:
    if not isinstance(value, str):
        raise SecretError("invalid_key_format")
    v = value.strip()
    if not KEY_RE.match(v):
        raise SecretError("invalid_key_format")
    return v


def validate_provider(value) -> str:
    v = value.strip().lower() if isinstance(value, str) else ""
    if not PROVIDER_RE.match(v):
        raise SecretError("invalid_provider")
    return v


def parse_slot(value) -> Slot:
    try:
        return Slot(value)
    except ValueError:
        raise SecretError("invalid_slot")


class SecretService:
    def __init__(self, session_factory, box: SecretBox, *, clock: Callable[[], datetime] | None = None, audit_log: Callable | None = None, dry_run_tests: bool = True):
        self._sf = session_factory
        self.box = box
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._audit_log = audit_log
        self._dry = dry_run_tests

    @property
    def writes_enabled(self) -> bool:
        return self.box.available

    async def _audit(self, s, actor, slot, provider, op, ok, reason, version):
        await audit.record(s, self._sf, now=self._clock(), actor=actor, slot=slot.value, provider=provider, op=op, success=ok, reason=reason, version=version,
                           audit_log=self._audit_log)

    # ---- read: metadata only
    async def get_secret_metadata(self, slot: Slot) -> SecretMetadata | None:
        try:
            async with self._sf() as s:
                return await repo.current(s, slot.value)
        except DBAPIError:
            raise SecretError("storage_not_migrated")

    async def list_metadata(self) -> dict[str, SecretMetadata | None]:
        return {sl.value: await self.get_secret_metadata(sl) for sl in Slot}

    # ---- read: runtime (the only decrypting path)
    async def get_secret_for_runtime(self, slot: Slot) -> SecretValue | None:
        """None when: no master key, storage missing, no ACTIVE version (disabled / revoked / deleted), or the ciphertext does not verify. Never raises."""
        try:
            if not self.box.available:
                return None
            async with self._sf() as s:
                row = await repo.ciphertext(s, slot.value)
            if row is None:
                return None
            plain = self.box.decrypt(slot.value, row[0], row[1])
            return SecretValue(plain) if plain else None
        except Exception:
            logger.error("runtime secret read failed slot=%s", slot.value)
            return None

    # ---- write
    async def set_secret(self, slot: Slot, api_key, provider, actor: str) -> SecretMetadata:
        """Create or rotate. The new value is encrypted, the previous ACTIVE/DISABLED version is revoked and its ciphertext erased, all in one transaction."""
        prov = None
        try:
            if not self.box.available:
                raise SecretError("master_key_unavailable")
            prov = validate_provider(provider)
            value = validate_key(api_key)
            now = self._clock()
            async with self._sf() as s:
                prev = await repo.current(s, slot.value)
                version = await repo.next_version(s, slot.value)
                token = self.box.encrypt(slot.value, version, value)
                if prev:
                    await repo.revoke_and_erase(s, slot.value, Status.REVOKED, now)
                sid = "sec_" + hashlib.sha256(f"{slot.value}|{version}|{now.isoformat()}".encode()).hexdigest()[:20]
                await repo.insert_active(s, secret_id=sid, slot=slot.value, version=version, provider=prov, key_slot=slot.value.lower(), encrypted=token,
                                         masked=mask(value), now=now)
                await self._audit(s, actor, slot, prov, Operation.KEY_UPDATED if prev else Operation.KEY_CREATED, True, "rotated" if prev else "created", version)
                await self._audit(s, actor, slot, prov, Operation.KEY_ACTIVATED, True, "new_version_active", version)
                await s.commit()
            return await self.get_secret_metadata(slot)
        except SecretError as e:
            await self._audit(None, actor, slot, prov, Operation.KEY_UPDATED, False, e.code, None)
            raise
        except IntegrityError:
            await self._audit(None, actor, slot, prov, Operation.KEY_UPDATED, False, "concurrent_update", None)
            raise SecretError("concurrent_update")
        except DBAPIError:
            raise SecretError("storage_not_migrated")

    async def delete_secret(self, slot: Slot, actor: str) -> None:
        try:
            async with self._sf() as s:
                cur = await repo.current(s, slot.value)
                if cur is None:
                    raise SecretError("not_configured")
                await repo.revoke_and_erase(s, slot.value, Status.DELETED, self._clock())
                await self._audit(s, actor, slot, cur.provider, Operation.KEY_DELETED, True, "deleted_and_erased", cur.version)
                await s.commit()
        except SecretError as e:
            await self._audit(None, actor, slot, None, Operation.KEY_DELETED, False, e.code, None)
            raise
        except DBAPIError:
            raise SecretError("storage_not_migrated")

    async def disable_secret(self, slot: Slot, actor: str) -> SecretMetadata:
        return await self._toggle(slot, actor, Status.ACTIVE, Status.DISABLED, Operation.KEY_DISABLED, "already_disabled_or_missing")

    async def activate_secret(self, slot: Slot, actor: str) -> SecretMetadata:
        if not self.box.available:
            await self._audit(None, actor, slot, None, Operation.KEY_ACTIVATED, False, "master_key_unavailable", None)
            raise SecretError("master_key_unavailable")
        return await self._toggle(slot, actor, Status.DISABLED, Status.ACTIVE, Operation.KEY_ACTIVATED, "not_disabled")

    async def _toggle(self, slot, actor, frm: Status, to: Status, op: Operation, bad: str) -> SecretMetadata:
        try:
            async with self._sf() as s:
                cur = await repo.current(s, slot.value)
                if cur is None or cur.status != frm.value:
                    raise SecretError(bad)
                await repo.set_status(s, cur.secret_id, to, self._clock())
                await self._audit(s, actor, slot, cur.provider, op, True, to.value.lower(), cur.version)
                await s.commit()
            return await self.get_secret_metadata(slot)
        except SecretError as e:
            await self._audit(None, actor, slot, None, op, False, e.code, None)
            raise
        except DBAPIError:
            raise SecretError("storage_not_migrated")

    # ---- connection test (dry-run: no network)
    async def test_secret(self, slot: Slot, actor: str) -> dict:
        try:
            meta = await self.get_secret_metadata(slot)
        except SecretError:
            raise
        secret = await self.get_secret_for_runtime(slot) if meta and meta.status == Status.ACTIVE.value else None
        if meta and meta.status == Status.ACTIVE.value and secret is None:
            result, detail = TestResult.UNAVAILABLE, "master_key_unavailable" if not self.box.available else "ciphertext_unreadable"
        else:
            result, detail = test_provider_credentials(slot.value, meta.provider if meta else None, secret, dry_run=self._dry)
        del secret
        if meta:
            try:
                async with self._sf() as s:
                    await repo.record_test(s, meta.secret_id, result.value, self._clock())
                    await s.commit()
            except Exception:
                logger.error("recording test result failed slot=%s", slot.value)
        await self._audit(None, actor, slot, meta.provider if meta else None, Operation.KEY_TESTED, result in (TestResult.VALID, TestResult.DRY_RUN), f"{result.value}:{detail}",
                          meta.version if meta else None)
        return {"result": result.value, "detail": detail}
