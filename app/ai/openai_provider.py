"""OpenAI Chat Completions provider (works with the standard public API)."""

from __future__ import annotations

import logging

import httpx

from app.ai.base import ChatMessage, CompletionResult

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"


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
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]["message"]["content"]
        return CompletionResult(text=choice, model=data.get("model", model), usage=data.get("usage", {}))

    def ping(self) -> tuple[bool, str]:
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(f"{self._base_url}/models", headers=self._headers())
            if resp.status_code == 200:
                return True, "OpenAI API reachable."
            return False, f"OpenAI API returned HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
