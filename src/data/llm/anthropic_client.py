"""Anthropic LLM client for data generation."""
from __future__ import annotations

import json
import os
from typing import List, Optional, Sequence, Type

from pydantic import BaseModel

from .base import AbstractLLM, ChatMessage

try:
    import anthropic
except ImportError:
    anthropic = None


class AnthropicChatLLM(AbstractLLM):
    """Anthropic Messages API client with structured output support."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        api_key: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 16384,
    ) -> None:
        """Initialize Anthropic client.

        Args:
            model: Model name (e.g., "claude-sonnet-4-20250514", "claude-3-5-sonnet-latest").
            api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens in response.
        """
        if anthropic is None:
            raise ImportError(
                "anthropic package not installed. Run: pip install anthropic"
            )

        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

        api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "Anthropic API key required. Set ANTHROPIC_API_KEY environment variable "
                "or pass api_key parameter."
            )
        self.client = anthropic.Anthropic(api_key=api_key)

    def _to_anthropic_messages(
        self, messages: Sequence[ChatMessage]
    ) -> tuple[Optional[str], List[dict]]:
        """Convert ChatMessage sequence to Anthropic format.

        Returns:
            Tuple of (system_content, non_system_messages).
        """
        system_parts = []
        non_system = []

        for msg in messages:
            if msg.role == "system":
                system_parts.append(msg.content)
            else:
                non_system.append({"role": msg.role, "content": msg.content})

        system_content = "\n".join(system_parts) if system_parts else None
        return system_content, non_system

    @staticmethod
    def _enforce_strict_schema(schema: dict) -> None:
        """Recursively set additionalProperties: false on all object types.

        Anthropic's JSON schema mode requires this for every object in the schema,
        including nested $defs. Free-form dicts (additionalProperties: true) are
        left unchanged since they must allow arbitrary keys.
        """
        if schema.get("type") == "object":
            schema["additionalProperties"] = False
        for key in ("properties", "$defs"):
            if key in schema:
                for sub in schema[key].values():
                    if isinstance(sub, dict):
                        AnthropicChatLLM._enforce_strict_schema(sub)
        for key in ("items", "anyOf", "oneOf", "allOf"):
            if key in schema:
                target = schema[key]
                if isinstance(target, dict):
                    AnthropicChatLLM._enforce_strict_schema(target)
                elif isinstance(target, list):
                    for item in target:
                        if isinstance(item, dict):
                            AnthropicChatLLM._enforce_strict_schema(item)

    def generate_structured(
        self,
        messages: Sequence[ChatMessage],
        response_model: Type[BaseModel],
    ) -> BaseModel:
        """Generate a structured response using Anthropic's JSON mode.

        Args:
            messages: Chat history as a sequence of messages.
            response_model: Pydantic model class to parse the response into.

        Returns:
            An instance of response_model populated from the LLM response.
        """
        schema = response_model.model_json_schema()
        self._enforce_strict_schema(schema)
        system_content, non_system_messages = self._to_anthropic_messages(messages)

        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": non_system_messages,
        }

        if system_content:
            kwargs["system"] = system_content

        # Add JSON schema output config (Anthropic SDK uses output_config, not response_format)
        kwargs["output_config"] = {
            "format": {
                "type": "json_schema",
                "schema": schema,
            },
        }

        response = self.client.messages.create(**kwargs)

        # Extract text content from response
        content = ""
        for block in response.content:
            if hasattr(block, "text"):
                content += block.text

        parsed = json.loads(content)
        return response_model(**parsed)

    def generate_text(self, messages: Sequence[ChatMessage]) -> str:
        """Generate a plain text response without schema constraints."""
        system_content, non_system_messages = self._to_anthropic_messages(messages)
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": non_system_messages,
        }
        if system_content:
            kwargs["system"] = system_content
        response = self.client.messages.create(**kwargs)
        content = ""
        for block in response.content:
            if hasattr(block, "text"):
                content += block.text
        return content
