from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence, Union

from pydantic import BaseModel
from src.llm.base import AbstractLLM, ChatMessage, StructuredResponse, ToolSpec

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - informative error if dependency missing
    OpenAI = None


class OpenAIChatLLM(AbstractLLM):
    """Simple OpenAI chat completion wrapper with optional tool definitions."""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.2,
        tools: Optional[List[ToolSpec]] = None,
    ) -> None:
        if OpenAI is None:
            raise ImportError("openai package not installed. Install `openai` to use OpenAIChatLLM.")
        self.model = model
        self.temperature = temperature
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required for OpenAIChatLLM.")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")
        self.tools = tools or []
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def _to_dict_messages(self, messages: Sequence[ChatMessage]) -> List[Dict[str, str]]:
        return [{"role": m.role, "content": m.content} for m in messages]

    def _tool_payloads(self) -> List[Dict[str, Any]]:
        payloads: List[Dict[str, Any]] = []
        for tool in self.tools:
            if isinstance(tool, ToolSpec):
                name = tool.name
                description = tool.description
                parameters = tool.parameters
            else:
                name = tool.get("name")
                description = tool.get("description")
                parameters = tool.get("parameters")

            payloads.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": parameters,
                    },
                }
            )
        return payloads

    def generate(self, messages: Sequence[ChatMessage]) -> ChatMessage:
        tool_payloads = self._tool_payloads()
        response = self.client.chat.completions.create(
            model=self.model,
            messages=self._to_dict_messages(messages),
            temperature=self.temperature,
            tools=tool_payloads if tool_payloads else None,
            tool_choice="auto" if tool_payloads else None,
        )

        msg = response.choices[0].message

        # Normalize message content to string
        content = ""
        if isinstance(msg.content, str):
            content = msg.content
        elif isinstance(msg.content, list):
            content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in msg.content)

        # Normalize tool_calls into simple dicts for downstream code
        normalized_tool_calls: List[Dict[str, Any]] = []
        raw_tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in raw_tool_calls:
            func = getattr(tc, "function", None) or {}
            name = getattr(func, "name", None) if not isinstance(func, dict) else func.get("name")
            arguments = getattr(func, "arguments", None) if not isinstance(func, dict) else func.get("arguments")
            normalized_tool_calls.append({"function": {"name": name, "arguments": arguments}})

        return ChatMessage(
            role=getattr(msg, "role", "assistant"),
            content=content or "",
            tool_calls=normalized_tool_calls if normalized_tool_calls else None,
        )

    def generate_structured(self, messages: Sequence[ChatMessage], schema_or_model: Union[Dict[str, Any], BaseModel]) -> Any:
        """Generate structured response using OpenAI's structured outputs."""
        if isinstance(schema_or_model, BaseModel):
            schema = schema_or_model.model_json_schema()
            response_model = schema_or_model
        else:
            schema = schema_or_model
            response_model = None

        response = self.client.chat.completions.create(
            model=self.model,
            messages=self._to_dict_messages(messages),
            temperature=self.temperature,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "schema": schema,
                    "strict": False  # Allow more flexibility
                }
            }
        )

        msg = response.choices[0].message
        content = msg.content or "{}"

        try:
            import json
            parsed = json.loads(content)
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
