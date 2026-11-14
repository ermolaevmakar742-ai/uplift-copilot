"""LLM access for Creative Intelligence. Uses the SAME endpoint, key and model catalogue as the bot (LLM_BASE_URL / LLM_API_KEY, OpenRouter
chat/completions) — it is a thin multimodal-capable transport because `LLMClient.generate` in main.py only accepts plain-text messages.
A model is used for images ONLY if the provider catalogue lists `image` in its input modalities; otherwise `VisionUnavailable` is raised
(no pretending, no sending images as text). `post` is injectable for tests."""
import json
import logging
import os
import time
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger("copilot_creative.llm")
# Seeded from the audit (OpenRouter catalogue, 2026-10): used only when the catalogue endpoint itself is unreachable.
KNOWN_IMAGE_MODELS = {"anthropic/claude-opus-5", "openai/gpt-5.6-luna"}
CATALOG_TTL = 3600


class LLMError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class VisionUnavailable(LLMError):
    pass


Post = Callable[[dict], Awaitable[dict]]


class LLM:
    def __init__(self, env=None, post: Post | None = None, catalog_loader: Callable[[], Awaitable[dict]] | None = None):
        self.env = os.environ if env is None else env
        self._post = post
        self._catalog_loader = catalog_loader
        self._catalog: dict | None = None
        self._catalog_at = 0.0

    @property
    def base(self) -> str:
        return str(self.env.get("LLM_BASE_URL", "")).strip().rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.base and str(self.env.get("LLM_API_KEY", "")).strip())

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.env.get('LLM_API_KEY', '')}", "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost", "X-Title": "COPILOT Creative Intelligence"}

    async def _load_catalog(self) -> dict:
        if self._catalog_loader:
            return await self._catalog_loader()
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{self.base}/models", headers=self._headers())
            r.raise_for_status()
            return {m["id"]: list((m.get("architecture") or {}).get("input_modalities") or []) for m in r.json().get("data", [])}

    async def modalities(self, model: str) -> tuple[list[str] | None, str]:
        """(input modalities or None if unknown, how it was established)."""
        if self._catalog is None or time.monotonic() - self._catalog_at > CATALOG_TTL:
            try:
                self._catalog, self._catalog_at = await self._load_catalog(), time.monotonic()
            except Exception as e:
                logger.warning("model catalogue unavailable: %s", type(e).__name__)
                self._catalog = None
        if self._catalog is not None:
            return (self._catalog.get(model), "provider_catalogue")
        return (["text", "image"] if model in KNOWN_IMAGE_MODELS else None, "audit_seed")

    async def supports_image(self, model: str) -> bool:
        mods, _ = await self.modalities(model)
        return bool(mods and "image" in mods)

    async def require_vision(self, model: str) -> None:
        if not self.configured:
            raise LLMError("llm_not_configured", "LLM_BASE_URL / LLM_API_KEY are not configured")
        if not await self.supports_image(model):
            raise VisionUnavailable("model_no_vision", f"Model '{model}' does not accept images (per provider catalogue). Set COPILOT_CREATIVE_VISION_MODEL to an image-capable model.")

    async def _http_post(self, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15)) as c:
            r = await c.post(f"{self.base}/chat/completions", headers=self._headers(), json=payload)
        if r.status_code == 402:
            raise LLMError("http_402", "LLM provider refused: insufficient credits on the OpenRouter account (top up the balance and retry)")
        if r.status_code >= 400:
            raise LLMError(f"http_{r.status_code}", f"LLM provider returned HTTP {r.status_code}")
        return r.json()

    async def chat(self, model: str, messages: list[dict], *, max_tokens: int = 4000, temperature: float = 0.4) -> tuple[str, dict]:
        if not self.configured and self._post is None:
            raise LLMError("llm_not_configured", "LLM_BASE_URL / LLM_API_KEY are not configured")
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature, "usage": {"include": True}}
        last: Exception | None = None
        for attempt in range(2):
            try:
                data = await (self._post or self._http_post)(payload)
                choice = (data.get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content")
                if isinstance(text, list):
                    text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
                if not text or not str(text).strip():
                    raise LLMError("empty_response", "The model returned an empty response")
                u = data.get("usage") or {}
                return str(text), {k: u.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cost") if k in u}
            except LLMError as e:
                last = e
                if e.code not in ("http_429", "http_500", "http_502", "http_503", "http_504", "empty_response") or attempt:
                    raise
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = LLMError("llm_unreachable", f"LLM provider unreachable ({type(e).__name__})")
                if attempt:
                    raise last
            await __import__("asyncio").sleep(2)
        raise last or LLMError("llm_failed")


def extract_json(text: str) -> dict:
    """First top-level JSON object in a model reply (tolerates ``` fences and chatter)."""
    s = str(text).strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```", 1)[0]
    i = s.find("{")
    if i < 0:
        raise LLMError("invalid_json", "The model did not return a JSON object")
    depth, in_str, esc = 0, False, False
    for j in range(i, len(s)):
        ch = s[j]
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_str = False
            if ch != "\\":
                esc = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[i:j + 1])
                except ValueError:
                    raise LLMError("invalid_json", "The model returned malformed JSON")
                if not isinstance(obj, dict):
                    raise LLMError("invalid_json", "Expected a JSON object")
                return obj
    raise LLMError("invalid_json", "The model returned incomplete JSON (output truncated?)")
