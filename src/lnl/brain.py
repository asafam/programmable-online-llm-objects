"""LLM provider abstraction — Brain interface and implementations."""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from .types import (
    InferenceMetrics,
    LLMResponse,
    Message,
    ObjectDefinition,
    OutgoingMessage,
)

# JSON schema for the LLM response format
LLM_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "updated_state": {
            "type": "string",
            "description": "The complete updated state after processing the message.",
        },
        "reply": {
            "type": "string",
            "description": "Your reply to the sender of the message.",
        },
        "outgoing_messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "recipient": {
                        "type": "string",
                        "description": "The object_id of the recipient.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content of the message.",
                    },
                },
                "required": ["recipient", "content"],
                "additionalProperties": False,
            },
            "description": "Messages to send to other objects.",
        },
        "reasoning": {
            "type": "string",
            "description": "Brief internal reasoning about what you did and why.",
        },
    },
    "required": ["updated_state", "reply", "outgoing_messages", "reasoning"],
    "additionalProperties": False,
}


def build_system_prompt(definition: ObjectDefinition, current_state: str) -> str:
    """Build the system prompt from an ObjectDefinition and current state."""
    parts = [f"You are '{definition.object_id}'."]

    parts.append(f"\n## Role\n{definition.role}")

    if definition.behavior:
        parts.append(f"\n## Behavior\n{definition.behavior}")

    if definition.peers:
        peer_lines = [f"- {p.object_id}: {p.relationship}" for p in definition.peers]
        parts.append(f"\n## Peers\n" + "\n".join(peer_lines))

    if definition.skills:
        parts.append(f"\n## Skills\n" + "\n".join(f"- {s}" for s in definition.skills))

    if definition.state_description:
        parts.append(f"\n## State Description\n{definition.state_description}")

    parts.append(f"\n## Current State\n{current_state or '(empty)'}")

    parts.append(
        "\n## Instructions\n"
        "Respond with a JSON object containing:\n"
        "- updated_state: Your complete updated state as a natural language string.\n"
        "- reply: Your reply to the sender.\n"
        "- outgoing_messages: A list of messages to send to peers (each with recipient and content).\n"
        "- reasoning: Brief internal reasoning about your decision.\n"
        "Do NOT include anything outside the JSON object."
    )

    return "\n".join(parts)


class LLMBrain(ABC):
    """Abstract interface for LLM processing backends."""

    @abstractmethod
    def process(
        self,
        definition: ObjectDefinition,
        current_state: str,
        message: Message,
        history: Sequence[Message],
    ) -> tuple[LLMResponse, InferenceMetrics]:
        """Process a message and return the LLM response with metrics."""
        ...


class OpenAIBrain(LLMBrain):
    """Brain backed by OpenAI via the existing OpenAIChatLLM client."""

    def __init__(self, model: str = "gpt-4o-mini", **kwargs: Any) -> None:
        from src.system.llm.openai_client import OpenAIChatLLM
        from src.system.llm.base import system_message, user_message

        self._llm = OpenAIChatLLM(model=model, **kwargs)
        self._system_message = system_message
        self._user_message = user_message

    def process(
        self,
        definition: ObjectDefinition,
        current_state: str,
        message: Message,
        history: Sequence[Message],
    ) -> tuple[LLMResponse, InferenceMetrics]:
        sys_prompt = build_system_prompt(definition, current_state)
        chat = [self._system_message(sys_prompt)]

        for msg in history:
            chat.append(self._user_message(f"[{msg.sender}]: {msg.content}"))

        chat.append(self._user_message(f"[{message.sender}]: {message.content}"))

        t0 = time.time()
        result = self._llm.generate_structured(chat, LLM_RESPONSE_SCHEMA)
        latency_ms = (time.time() - t0) * 1000

        usage = self._llm.last_usage
        metrics = InferenceMetrics(
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            latency_ms=latency_ms,
            model=self._llm.model,
        )

        return _parse_llm_result(result), metrics


class AnthropicBrain(LLMBrain):
    """Brain backed by Anthropic via the existing AnthropicChatLLM client."""

    def __init__(self, model: str = "claude-3-5-sonnet-latest", **kwargs: Any) -> None:
        from src.system.llm.anthropic_client import AnthropicChatLLM
        from src.system.llm.base import system_message, user_message

        self._llm = AnthropicChatLLM(model=model, **kwargs)
        self._system_message = system_message
        self._user_message = user_message

    def process(
        self,
        definition: ObjectDefinition,
        current_state: str,
        message: Message,
        history: Sequence[Message],
    ) -> tuple[LLMResponse, InferenceMetrics]:
        sys_prompt = build_system_prompt(definition, current_state)
        chat = [self._system_message(sys_prompt)]

        for msg in history:
            chat.append(self._user_message(f"[{msg.sender}]: {msg.content}"))

        chat.append(self._user_message(f"[{message.sender}]: {message.content}"))

        t0 = time.time()
        result = self._llm.generate_structured(chat, LLM_RESPONSE_SCHEMA)
        latency_ms = (time.time() - t0) * 1000

        usage = self._llm.last_usage
        metrics = InferenceMetrics(
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            latency_ms=latency_ms,
            model=self._llm.model,
        )

        return _parse_llm_result(result), metrics


@dataclass
class _ScriptEntry:
    response: LLMResponse
    metrics: InferenceMetrics = field(
        default_factory=lambda: InferenceMetrics(model="mock")
    )


@dataclass
class CallRecord:
    """Record of a call made to MockBrain."""
    object_id: str
    definition: ObjectDefinition
    current_state: str
    message: Message


class MockBrain(LLMBrain):
    """Deterministic scripted brain for testing."""

    def __init__(self) -> None:
        self._scripts: dict[str, list[_ScriptEntry]] = {}
        self._default_response: Optional[LLMResponse] = None
        self.call_log: list[CallRecord] = []

    def script(
        self,
        object_id: str,
        response: LLMResponse,
        metrics: Optional[InferenceMetrics] = None,
    ) -> None:
        """Add a scripted response for an object. Responses are consumed in order."""
        entry = _ScriptEntry(
            response=response,
            metrics=metrics or InferenceMetrics(model="mock"),
        )
        self._scripts.setdefault(object_id, []).append(entry)

    def set_default(self, response: LLMResponse) -> None:
        """Set a default response for any unscripted calls."""
        self._default_response = response

    def process(
        self,
        definition: ObjectDefinition,
        current_state: str,
        message: Message,
        history: Sequence[Message],
    ) -> tuple[LLMResponse, InferenceMetrics]:
        self.call_log.append(
            CallRecord(
                object_id=definition.object_id,
                definition=definition,
                current_state=current_state,
                message=message,
            )
        )

        entries = self._scripts.get(definition.object_id, [])
        if entries:
            entry = entries.pop(0)
            return entry.response, entry.metrics

        if self._default_response is not None:
            return self._default_response, InferenceMetrics(model="mock")

        # Fallback: echo back with no state change
        return (
            LLMResponse(
                updated_state=current_state,
                reply=f"Echo: {message.content}",
                outgoing_messages=[],
                reasoning="No script configured",
            ),
            InferenceMetrics(model="mock"),
        )


def _parse_llm_result(result: Any) -> LLMResponse:
    """Parse the raw LLM result (dict or StructuredResponse) into LLMResponse."""
    if isinstance(result, dict):
        data = result
    else:
        # StructuredResponse from Anthropic — has .response, .state, .messages
        data = {
            "updated_state": getattr(result, "state", "") or "",
            "reply": getattr(result, "response", "") or "",
            "outgoing_messages": getattr(result, "messages", []) or [],
            "reasoning": "",
        }

    outgoing = []
    for m in data.get("outgoing_messages", []):
        if isinstance(m, dict):
            outgoing.append(OutgoingMessage(recipient=m["recipient"], content=m["content"]))
        elif isinstance(m, OutgoingMessage):
            outgoing.append(m)

    return LLMResponse(
        updated_state=data.get("updated_state", ""),
        reply=data.get("reply", ""),
        outgoing_messages=outgoing,
        reasoning=data.get("reasoning", ""),
    )
