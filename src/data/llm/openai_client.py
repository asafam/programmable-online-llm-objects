"""OpenAI LLM client for data generation."""
from __future__ import annotations

import json
import os
from typing import List, Optional, Sequence, Type

from pydantic import BaseModel

from .base import AbstractLLM, ChatMessage

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


class OpenAIChatLLM(AbstractLLM):
    """OpenAI chat completion client with structured output support."""

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: Optional[str] = None,
        temperature: float = 0.7,
        seed: Optional[int] = None,
    ) -> None:
        """Initialize OpenAI client.

        Args:
            model: Model name (e.g., "gpt-4o", "gpt-4o-mini").
            api_key: OpenAI API key. Falls back to OPENAI_API_KEY env var.
            temperature: Sampling temperature.
            seed: Random seed for reproducibility.
        """
        if OpenAI is None:
            raise ImportError(
                "openai package not installed. Run: pip install openai"
            )

        self.model = model
        self.temperature = temperature
        self.seed = seed

        api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OpenAI API key required. Set OPENAI_API_KEY environment variable "
                "or pass api_key parameter."
            )
        self.client = OpenAI(api_key=api_key)

    def _to_dict_messages(self, messages: Sequence[ChatMessage]) -> List[dict]:
        """Convert ChatMessage sequence to OpenAI message format."""
        return [{"role": m.role, "content": m.content} for m in messages]

    def generate_structured(
        self,
        messages: Sequence[ChatMessage],
        response_model: Type[BaseModel],
    ) -> BaseModel:
        """Generate a structured response using OpenAI's JSON mode.

        Args:
            messages: Chat history as a sequence of messages.
            response_model: Pydantic model class to parse the response into.

        Returns:
            An instance of response_model populated from the LLM response.
        """
        schema = response_model.model_json_schema()

        kwargs = {
            "model": self.model,
            "messages": self._to_dict_messages(messages),
            "temperature": self.temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "schema": schema,
                    "strict": False,  # Pydantic schemas may not be strict-compatible
                },
            },
        }

        if self.seed is not None:
            kwargs["seed"] = self.seed

        response = self.client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or "{}"

        parsed = json.loads(content)
        return response_model(**parsed)

    def generate_text(self, messages: Sequence[ChatMessage]) -> str:
        """Generate a plain text response without schema constraints."""
        kwargs = {
            "model": self.model,
            "messages": self._to_dict_messages(messages),
            "temperature": self.temperature,
        }
        if self.seed is not None:
            kwargs["seed"] = self.seed
        response = self.client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""
