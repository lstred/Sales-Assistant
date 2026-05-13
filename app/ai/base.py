"""AI provider interface.

Concrete providers (OpenAI / Anthropic / Azure OpenAI) implement this
contract. The rest of the app only depends on ``AIProvider``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(slots=True)
class CompletionResult:
    text: str
    model: str
    usage: dict = field(default_factory=dict)


class AIProvider(Protocol):
    """Minimum surface area each AI backend must implement."""

    name: str

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_output_tokens: int,
        temperature: float,
        timeout_seconds: int,
    ) -> CompletionResult: ...

    def ping(self) -> tuple[bool, str]:
        """Cheap liveness check."""
        ...
