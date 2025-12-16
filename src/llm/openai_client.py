from __future__ import annotations

import os
import yaml
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
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: Optional[float] = None,
        seed: Optional[int] = None,
        tools: Optional[List[ToolSpec]] = None,
    ) -> None:
        if OpenAI is None:
            raise ImportError("openai package not installed. Install `openai` to use OpenAIChatLLM.")

        # Load config from system.yml
        config_path = os.path.join(os.path.dirname(__file__), '..', '..', 'config', 'system.yml')
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        llm_config = config.get('llm', {})

        self.model = model or llm_config.get('model', 'gpt-4o-mini')
        self.temperature = temperature if temperature is not None else llm_config.get('temperature', 0.0)
        self.seed = seed if seed is not None else llm_config.get('seed')
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
        kwargs = {
            "model": self.model,
            "messages": self._to_dict_messages(messages),
            "temperature": self.temperature,
        }
        if tool_payloads:
            kwargs["tools"] = tool_payloads
            kwargs["tool_choice"] = "auto"
        if self.seed is not None:
            kwargs["seed"] = self.seed

        response = self.client.chat.completions.create(**kwargs)

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

    def generate_structured(self, messages: Sequence[ChatMessage], schema_or_model: Union[Dict[str, Any], BaseModel, type]) -> Any:
        """Generate structured response using OpenAI's structured outputs.

        Args:
            schema_or_model: Either a Pydantic model (uses strict=False) or a raw JSON schema dict (uses strict=True)
        """
        # Determine if this is a Pydantic model or a raw schema
        is_pydantic = isinstance(schema_or_model, type) and issubclass(schema_or_model, BaseModel)

        if is_pydantic:
            schema = schema_or_model.schema()
            response_model = schema_or_model
            # Pydantic schemas may not have additionalProperties: false, so use strict=False
            strict_mode = False
        else:
            schema = schema_or_model
            response_model = None
            # Raw schemas should be designed for strict mode (with additionalProperties: false)
            strict_mode = True

        kwargs = {
            "model": self.model,
            "messages": self._to_dict_messages(messages),
            "temperature": self.temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "schema": schema,
                    "strict": strict_mode
                }
            }
        }
        if self.seed is not None:
            kwargs["seed"] = self.seed

        response = self.client.chat.completions.create(**kwargs)

        msg = response.choices[0].message
        content = msg.content or "{}"

        try:
            import json
            parsed = json.loads(content)

            if response_model:
                # Pydantic model - instantiate it
                return response_model(**parsed)
            elif is_pydantic:
                # Was a Pydantic type but we're not using it - return StructuredResponse
                return StructuredResponse(
                    response=parsed.get("response") or "",
                    state=parsed.get("state"),
                    messages=parsed.get("messages")
                )
            else:
                # Raw schema - return the parsed dict directly
                return parsed

        except json.JSONDecodeError as e:
            # Fallback - log error for debugging
            print(f"[ERROR] Failed to parse structured JSON response: {e}")
            print(f"[ERROR] Raw content: {content[:500]}")  # First 500 chars

            if response_model:
                return response_model()
            elif is_pydantic:
                return StructuredResponse(response="", state=None, messages=None)
            else:
                # Raw schema - return empty dict
                return {}
