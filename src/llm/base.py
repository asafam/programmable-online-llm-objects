from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Union

from pydantic import BaseModel


@dataclass
class ChatMessage:
    role: str
    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None


@dataclass
class StructuredResponse:
    """Structured response from LLM with separate components."""
    response: str  # Natural language response text
    state: Optional[Dict[str, Any]] = None  # State updates as JSON
    messages: Optional[List[Dict[str, Any]]] = None  # Messages to send


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]


class AbstractLLM(ABC):
    """Abstract interface for LLM backends."""

    @abstractmethod
    def generate(self, messages: Sequence[ChatMessage]) -> ChatMessage:
        """Return a single assistant message given a chat history."""
        raise NotImplementedError

    @abstractmethod
    def generate_structured(self, messages: Sequence[ChatMessage], schema_or_model: Union[Dict[str, Any], BaseModel]) -> Any:
        """Return a structured response given a chat history and JSON schema or Pydantic model."""
        raise NotImplementedError

    def generate_text(self, messages: Sequence[ChatMessage]) -> str:
        """Helper to return only the content string."""
        return self.generate(messages).content


def system_message(content: str) -> ChatMessage:
    return ChatMessage(role="system", content=content)


def user_message(content: str) -> ChatMessage:
    return ChatMessage(role="user", content=content)


def assistant_message(content: str) -> ChatMessage:
    return ChatMessage(role="assistant", content=content)
