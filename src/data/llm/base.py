"""Abstract LLM interface for data generation."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Type, Union

from pydantic import BaseModel


@dataclass
class ChatMessage:
    """Represents a chat message with role and content."""
    role: str
    content: str


class AbstractLLM(ABC):
    """Abstract interface for LLM backends."""

    @abstractmethod
    def generate_structured(
        self,
        messages: Sequence[ChatMessage],
        response_model: Type[BaseModel],
    ) -> BaseModel:
        """Generate a structured response matching the given Pydantic model.

        Args:
            messages: Chat history as a sequence of messages.
            response_model: Pydantic model class to parse the response into.

        Returns:
            An instance of response_model populated from the LLM response.
        """
        raise NotImplementedError

    def generate_text(
        self,
        messages: Sequence[ChatMessage],
    ) -> str:
        """Generate a plain text response.

        Default implementation raises NotImplementedError. Subclasses should
        override this to support free-form text generation without schema constraints.
        """
        raise NotImplementedError


def system_message(content: str) -> ChatMessage:
    """Create a system message."""
    return ChatMessage(role="system", content=content)


def user_message(content: str) -> ChatMessage:
    """Create a user message."""
    return ChatMessage(role="user", content=content)


def assistant_message(content: str) -> ChatMessage:
    """Create an assistant message."""
    return ChatMessage(role="assistant", content=content)
