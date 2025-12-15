from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence, Union

from pydantic import BaseModel
from src.llm.base import AbstractLLM, ChatMessage, StructuredResponse, ToolSpec

try:
    import anthropic
except ImportError:  # pragma: no cover - informative error if dependency missing
    anthropic = None


class AnthropicChatLLM(AbstractLLM):
    """Simple Anthropic Messages API wrapper with optional tool definitions."""

    def __init__(
        self,
        model: str = "claude-3-5-sonnet-latest",
        api_key: Optional[str] = None,
        temperature: float = 0.2,
        tools: Optional[List[ToolSpec]] = None,
    ) -> None:
        if anthropic is None:
            raise ImportError("anthropic package not installed. Install `anthropic` to use AnthropicChatLLM.")
        self.model = model
        self.temperature = temperature
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for AnthropicChatLLM.")
        self.tools = tools or []
        self.client = anthropic.Anthropic(api_key=self.api_key)

    def _tool_payloads(self) -> List[Dict[str, Any]]:
        payloads: List[Dict[str, Any]] = []
        for tool in self.tools:
            payloads.append(
                {
                    "name": tool.name if isinstance(tool, ToolSpec) else tool.get("name"),
                    "description": tool.description if isinstance(tool, ToolSpec) else tool.get("description"),
                    "input_schema": tool.parameters if isinstance(tool, ToolSpec) else tool.get("parameters"),
                }
            )
        return payloads

    def _to_anthropic_messages(self, messages: Sequence[ChatMessage]) -> List[Dict[str, Any]]:
        formatted: List[Dict[str, Any]] = []
        for msg in messages:
            formatted.append({"role": msg.role, "content": msg.content})
        return formatted

    def generate(self, messages: Sequence[ChatMessage]) -> ChatMessage:
        tools_payload = self._tool_payloads()

        # Extract system messages
        system_parts = []
        non_system_messages = []
        for msg in messages:
            if msg.role == "system":
                system_parts.append(msg.content)
            else:
                non_system_messages.append(msg)

        system_content = "\n".join(system_parts) if system_parts else None

        resp = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            temperature=self.temperature,
            system=system_content,
            messages=self._to_anthropic_messages(non_system_messages),
            tools=tools_payload if tools_payload else None,
        )

        content_str = ""
        tool_calls = None
        for block in resp.content:
            if block["type"] == "text":
                content_str += block.get("text", "")
            if block["type"] == "tool_use":
                tool_calls = [
                    {
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {}) or {}),
                        }
                    }
                ]
                break

        return ChatMessage(role="assistant", content=content_str, tool_calls=tool_calls)

    def generate_structured(self, messages: Sequence[ChatMessage], schema_or_model: Union[Dict[str, Any], BaseModel]) -> Any:
        """Generate structured response using Anthropic's structured outputs."""
        if isinstance(schema_or_model, BaseModel):
            schema = schema_or_model.model_json_schema()
            response_model = schema_or_model
        else:
            schema = schema_or_model
            response_model = None

        # Extract system messages
        system_parts = []
        non_system_messages = []
        for msg in messages:
            if msg.role == "system":
                system_parts.append(msg.content)
            else:
                non_system_messages.append(msg)

        system_content = "\n".join(system_parts) if system_parts else None

        resp = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            temperature=self.temperature,
            system=system_content,
            messages=self._to_anthropic_messages(non_system_messages),
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "schema": schema,
                    "strict": False  # Allow more flexibility
                }
            }
        )

        content_str = ""
        for block in resp.content:
            if block["type"] == "text":
                content_str += block.get("text", "")

        try:
            parsed = json.loads(content_str)
            if response_model:
                return response_model(**parsed)
            else:
                return StructuredResponse(
                    response=parsed.get("response", ""),
                    state=parsed.get("state"),
                    messages=parsed.get("messages")
                )
        except json.JSONDecodeError:
            # Fallback
            if response_model:
                return response_model()
            else:
                return StructuredResponse(response="", state=None, messages=None)
