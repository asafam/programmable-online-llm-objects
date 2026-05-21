"""LLM utilities for data generation."""
from typing import Optional

from .base import AbstractLLM, ChatMessage, user_message, system_message, assistant_message
from .openai_client import OpenAIChatLLM
from .azure_client import AzureChatLLM
from .anthropic_client import AnthropicChatLLM
from .gemini_client import GeminiChatLLM


def create_llm(
    provider: str,
    model: str,
    temperature: float = 0.7,
    seed: Optional[int] = None,
) -> AbstractLLM:
    """Create an LLM client for the specified provider.

    Args:
        provider: LLM provider - "openai", "anthropic", or "google".
        model: Model name (e.g., "gpt-4o", "claude-sonnet-4-20250514", "gemini-2.5-pro").
        temperature: Sampling temperature.
        seed: Random seed for reproducibility (OpenAI only).

    Returns:
        Configured LLM client.

    Raises:
        ValueError: If provider is not recognized.
    """
    if provider == "openai":
        return OpenAIChatLLM(
            model=model,
            temperature=temperature,
            seed=seed,
        )
    elif provider == "azure":
        return AzureChatLLM(
            model=model,
            temperature=temperature,
            seed=seed,
        )
    elif provider == "anthropic":
        return AnthropicChatLLM(
            model=model,
            temperature=temperature,
        )
    elif provider == "google":
        return GeminiChatLLM(
            model=model,
            temperature=temperature,
        )
    else:
        raise ValueError(
            f"Unknown provider: {provider}. Use 'openai', 'azure', 'anthropic', or 'google'."
        )


__all__ = [
    "AbstractLLM",
    "ChatMessage",
    "OpenAIChatLLM",
    "AnthropicChatLLM",
    "GeminiChatLLM",
    "create_llm",
    "user_message",
    "system_message",
    "assistant_message",
]
