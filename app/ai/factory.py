"""AI provider factory."""

from __future__ import annotations

from app.ai.base import AIProvider
from app.ai.openai_provider import OpenAIProvider
from app.config.models import AIConfig
from app.config.store import get_secret


def build_provider(cfg: AIConfig) -> AIProvider:
    api_key = (get_secret("AI", f"{cfg.provider}:{cfg.api_username}") or "").strip()
    if cfg.provider == "openai":
        return OpenAIProvider(api_key=api_key, base_url=cfg.base_url)
    raise NotImplementedError(f"AI provider {cfg.provider!r} not implemented yet")
