"""OpenRouter key status service: secret (runtime accessor only) -> client -> normalised status. In-memory TTL cache holds ONLY normalised metrics."""
import logging
import time
from datetime import datetime, timezone
from typing import Callable

from copilot_openrouter import client as _client
from copilot_openrouter.contracts import FetchError, KeyState, OpenRouterKeyStatus
from copilot_secrets.contracts import Slot

logger = logging.getLogger("copilot_openrouter")
CACHE_TTL = 45.0
SAFE_MESSAGES = {
    KeyState.NOT_CONFIGURED: "No API key configured",
    KeyState.UNAUTHORIZED: "OpenRouter rejected the key",
    KeyState.RATE_LIMITED: "Rate limited, try again later",
    KeyState.UNAVAILABLE: "Could not retrieve balance",
    KeyState.ERROR: "Could not retrieve balance",
}


def classify(info: dict) -> KeyState:
    exhausted = [v for v in (info.get("limit_remaining"), info.get("balance")) if v is not None]
    return KeyState.LIMITED if any(v <= 0 for v in exhausted) else KeyState.HEALTHY


class OpenRouterBalanceService:
    def __init__(self, secret_provider, *, fetch: Callable | None = None, clock: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] = time.monotonic, ttl: float = CACHE_TTL):
        self._secrets, self._fetch, self._ttl, self._mono = secret_provider, fetch or _client.fetch_all, ttl, monotonic
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[str, tuple[float, OpenRouterKeyStatus]] = {}

    async def status(self, slot: Slot, *, force: bool = False) -> OpenRouterKeyStatus:
        """Never raises. force=True bypasses the cache (manual refresh)."""
        now = self._mono()
        hit = self._cache.get(slot.value)
        if hit and not force and now - hit[0] < self._ttl:
            return hit[1]
        res = await self._compute(slot)
        if res.configured and res.status in (KeyState.HEALTHY.value, KeyState.LIMITED.value):
            self._cache[slot.value] = (now, res)
        else:
            self._cache.pop(slot.value, None)
        return res

    async def all(self, *, force: bool = False) -> list[OpenRouterKeyStatus]:
        return [await self.status(s, force=force) for s in Slot]

    async def _compute(self, slot: Slot) -> OpenRouterKeyStatus:
        at = self._clock()
        secret = await self._secrets.get_secret_for_runtime(slot)
        if secret is None:
            return OpenRouterKeyStatus(slot.value, False, KeyState.NOT_CONFIGURED.value, retrieved_at=at, error_code="not_configured",
                                       error_message_safe=SAFE_MESSAGES[KeyState.NOT_CONFIGURED])
        try:
            info = await self._fetch(secret.reveal())
        except FetchError as e:
            return self._failed(slot, at, e.state, e.code)
        except Exception:
            logger.warning("openrouter status failed slot=%s", slot.value)           # no exc_info: nothing that could carry the key
            return self._failed(slot, at, KeyState.UNAVAILABLE, "client_error")
        finally:
            del secret
        st = classify(info)
        return OpenRouterKeyStatus(slot.value, True, st.value, balance=info.get("balance"), limit=info.get("limit"),
                                   limit_remaining=info.get("limit_remaining"), total_credits=info.get("total_credits"), total_usage=info.get("total_usage"), usage_daily=info.get("usage_daily"),
                                   usage_weekly=info.get("usage_weekly"), usage_monthly=info.get("usage_monthly"), retrieved_at=at,
                                   balance_source=info.get("balance_source"), balance_error_code=info.get("balance_error_code"))

    @staticmethod
    def _failed(slot: Slot, at: datetime, state: KeyState, code: str) -> OpenRouterKeyStatus:
        return OpenRouterKeyStatus(slot.value, True, state.value, retrieved_at=at, error_code=code, error_message_safe=SAFE_MESSAGES[state])
