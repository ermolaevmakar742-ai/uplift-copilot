"""The single outbound call: GET https://openrouter.ai/api/v1/key. URL is a backend constant (no argument can change it), HTTPS, timeout mandatory,
no redirects, no environment proxies. Failures become `FetchError` with a safe code; the response body is parsed, never returned or logged."""
import logging
import math
from typing import Any

import httpx

from copilot_openrouter.contracts import FetchError, KeyState

logger = logging.getLogger("copilot_openrouter")
KEY_URL = "https://openrouter.ai/api/v1/key"
CREDITS_URL = "https://openrouter.ai/api/v1/credits"      # official account balance endpoint (total_credits, total_usage); management keys only
TIMEOUT_SECONDS = 8.0
MAX_BODY = 64 * 1024
_NUM_FIELDS = ("limit", "limit_remaining", "usage", "usage_daily", "usage_weekly", "usage_monthly")


def _num(v: Any) -> float | None:
    """Only a real finite non-bool number counts; anything else is unknown (None), never 0."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def parse_key_payload(body: Any) -> dict:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise FetchError(KeyState.ERROR, "malformed_response")
    return {k: _num(data.get(k)) for k in _NUM_FIELDS}


async def _get(url: str, secret_value: str, transport, timeout: float):
    """One GET to a backend-constant URL -> parsed JSON body. Raises FetchError only."""
    headers = {"Authorization": "Bearer " + secret_value, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=False, trust_env=False, transport=transport) as c:
            r = await c.get(url, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError):
        logger.warning("openrouter key status: network failure")
        raise FetchError(KeyState.UNAVAILABLE, "network_error")
    except Exception:
        logger.warning("openrouter key status: unexpected client failure")
        raise FetchError(KeyState.UNAVAILABLE, "client_error")
    sc = r.status_code
    if sc in (401, 403):
        raise FetchError(KeyState.UNAUTHORIZED, "forbidden" if sc == 403 else "unauthorized")
    if sc == 429:
        raise FetchError(KeyState.RATE_LIMITED, "rate_limited")
    if sc != 200:
        raise FetchError(KeyState.ERROR, f"http_{sc}")
    if len(r.content) > MAX_BODY:
        raise FetchError(KeyState.ERROR, "malformed_response")
    try:
        return r.json()
    except Exception:
        raise FetchError(KeyState.ERROR, "malformed_response")


async def fetch_key_info(secret_value: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = TIMEOUT_SECONDS) -> dict:
    """GET /api/v1/key -> per-key limit / usage. Raises FetchError only."""
    return parse_key_payload(await _get(KEY_URL, secret_value, transport, timeout))


def parse_credits_payload(body: Any) -> dict:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise FetchError(KeyState.ERROR, "malformed_response")
    total, used = _num(data.get("total_credits")), _num(data.get("total_usage"))
    if total is None or used is None:
        raise FetchError(KeyState.ERROR, "malformed_response")
    return {"total_credits": total, "total_usage": used, "balance": round(total - used, 6)}


async def fetch_credits(secret_value: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = TIMEOUT_SECONDS) -> dict:
    """GET /api/v1/credits -> account balance. Raises FetchError only."""
    return parse_credits_payload(await _get(CREDITS_URL, secret_value, transport, timeout))


async def fetch_all(secret_value: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = TIMEOUT_SECONDS) -> dict:
    """/key is authoritative for the key's health (its failure fails the status). /credits only adds the account balance: if it fails the key stays
    HEALTHY and `balance_error_code` says why the balance is unknown (403 = not a management key)."""
    info = await fetch_key_info(secret_value, transport=transport, timeout=timeout)
    try:
        info.update(await fetch_credits(secret_value, transport=transport, timeout=timeout))
        info["balance_source"] = "credits"
    except FetchError as e:
        info["balance_error_code"] = "credits_forbidden" if e.code == "forbidden" else "credits_" + e.code
    return info
