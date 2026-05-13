"""AI provider abstraction + concrete implementations."""

from app.ai.base import AIProvider, ChatMessage, CompletionResult
from app.ai.factory import build_provider

__all__ = ["AIProvider", "ChatMessage", "CompletionResult", "build_provider"]
