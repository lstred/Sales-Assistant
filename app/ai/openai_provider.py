"""OpenAI Chat Completions provider (works with the standard public API)."""

from __future__ import annotations

import logging
import random
import time

import httpx

from app.ai.base import ChatMessage, CompletionResult

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"

# Transient errors we retry on (rate-limits + transient server-side failures).
_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 4
_BASE_BACKOFF_SECONDS = 2.0  # 2s, 4s, 8s, 16s — plus jitter.


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, base_url: str = "") -> None:
        # Strip stray whitespace/newlines: a trailing \n inside a header value
        # blows up at the httpx layer with LocalProtocolError.
        clean = (api_key or "").strip()
        if not clean:
            raise ValueError("OpenAI API key is required")
        self._api_key = clean
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_output_tokens: int,
        temperature: float,
        timeout_seconds: int,
    ) -> CompletionResult:
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_output_tokens,
            "temperature": temperature,
        }
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                with httpx.Client(timeout=timeout_seconds) as client:
                    resp = client.post(
                        f"{self._base_url}/chat/completions",
                        headers=self._headers(),
                        json=payload,
                    )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                # Network blips are also worth retrying.
                last_exc = exc
                if attempt >= _MAX_RETRIES:
                    raise
                sleep_for = _BASE_BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0, 1.0)
                log.warning("OpenAI transport error %s; retrying in %.1fs (attempt %d/%d)",
                            type(exc).__name__, sleep_for, attempt + 1, _MAX_RETRIES)
                time.sleep(sleep_for)
                continue

            if resp.status_code in _RETRY_STATUS_CODES and attempt < _MAX_RETRIES:
                # Honor Retry-After (seconds or HTTP-date) when the server provides it,
                # otherwise exponential backoff with jitter.
                retry_after_header = resp.headers.get("Retry-After", "").strip()
                sleep_for: float
                try:
                    sleep_for = float(retry_after_header) if retry_after_header else 0.0
                except ValueError:
                    sleep_for = 0.0
                if sleep_for <= 0:
                    sleep_for = _BASE_BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0, 1.0)
                # Cap any single wait at 60s so we never block the UI thread excessively.
                sleep_for = min(sleep_for, 60.0)
                log.warning(
                    "OpenAI HTTP %d (%s); retrying in %.1fs (attempt %d/%d)",
                    resp.status_code,
                    "rate-limited" if resp.status_code == 429 else "server error",
                    sleep_for,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(sleep_for)
                continue

            # Either success or a non-retriable error -> let raise_for_status handle it.
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]["message"]["content"]
            return CompletionResult(
                text=choice, model=data.get("model", model), usage=data.get("usage", {})
            )

        # Should not be reachable: loop either returns or raises. Guard anyway.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("OpenAI request failed after retries with no recorded exception")

    def ping(self) -> tuple[bool, str]:
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(f"{self._base_url}/models", headers=self._headers())
            if resp.status_code == 200:
                return True, "OpenAI API reachable."
            return False, f"OpenAI API returned HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
