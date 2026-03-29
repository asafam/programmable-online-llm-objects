"""LLM provider abstraction — Brain interface and implementations."""
from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import yaml

from .types import (
    ExternalAction,
    InferenceMetrics,
    LLMResponse,
    Message,
    MessageType,
    ObjectDefinition,
    OutgoingMessage,
    ReactFinish,
    ReactStep,
    ToolCall,
    ToolResult,
)

# JSON schema for the LLM response format (no tools)
LLM_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "updated_state": {
            "type": "object",
            "additionalProperties": True,
            "description": "The complete updated state after processing the message, as a JSON object.",
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
        "external_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "system": {
                        "type": "string",
                        "description": "External system name, e.g. 'slack', 'email', 'jira'.",
                    },
                    "action": {
                        "type": "string",
                        "description": "Action to perform, e.g. 'send_message', 'send', 'create_issue'.",
                    },
                    "content": {
                        "type": "string",
                        "description": "NL content: the message body, email text, ticket description, etc.",
                    },
                    "params": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": "Structured parameters: channel, to, subject, project, etc.",
                    },
                },
                "required": ["system", "action", "content"],
                "additionalProperties": False,
            },
            "description": "Actions directed at external systems (Slack, Email, Jira, etc.). Use instead of outgoing_messages for external integrations.",
        },
    },
    "required": ["updated_state", "reply", "outgoing_messages", "reasoning"],
    "additionalProperties": False,
}

# Schema extended with tool_calls — used when tools are registered.
# The tool_calls items schema is intentionally open (additionalProperties: true on arguments)
# so any tool can be called. The system prompt describes the available tools and their arguments.
LLM_RESPONSE_SCHEMA_WITH_TOOLS: dict[str, Any] = {
    **LLM_RESPONSE_SCHEMA,
    "properties": {
        **LLM_RESPONSE_SCHEMA["properties"],
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Unique ID for this tool call."},
                    "tool": {"type": "string", "description": "Tool name."},
                    "arguments": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": "Arguments for the tool, as described in the Tools section.",
                    },
                },
                "required": ["id", "tool", "arguments"],
                "additionalProperties": False,
            },
            "description": "Tool calls to execute. When present, the LLM will be called again with results before producing a final response.",
        },
    },
}


# ReAct step schema — one thought + one action per LLM call.
# action="tool_call": execute a tool and observe the result, then call again.
# action="finish": commit reply, state, and any outgoing messages/actions.
LLM_REACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {
            "type": "string",
            "description": "Your explicit reasoning about what to do next.",
        },
        "action": {
            "type": "string",
            "enum": ["tool_call", "finish"],
            "description": "The single action to take this step.",
        },
        "tool_call": {
            "type": "object",
            "description": "Present only when action=tool_call.",
            "properties": {
                "id": {"type": "string", "description": "Unique ID for this call."},
                "tool": {"type": "string", "description": "Tool name."},
                "arguments": {"type": "object", "additionalProperties": True},
            },
            "required": ["id", "tool", "arguments"],
            "additionalProperties": False,
        },
        "finish": {
            "type": "object",
            "description": "Present only when action=finish.",
            "properties": {
                "reply": {"type": "string", "description": "Reply to the message sender."},
                "updated_state": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": "Your complete updated state.",
                },
                "outgoing_messages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "recipient": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["recipient", "content"],
                        "additionalProperties": False,
                    },
                },
                "external_actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "system": {"type": "string"},
                            "action": {"type": "string"},
                            "content": {"type": "string"},
                            "params": {"type": "object", "additionalProperties": True},
                        },
                        "required": ["system", "action", "content"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["reply", "updated_state"],
            "additionalProperties": False,
        },
    },
    "required": ["thought", "action"],
    "additionalProperties": False,
}


_PROMPT_CONFIG: Optional[dict] = None

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "config" / "prompts" / "lnl"


def _load_prompt_config() -> dict:
    """Load the prompt config from config/prompts/lnl/object.yaml."""
    global _PROMPT_CONFIG
    if _PROMPT_CONFIG is None:
        with open(_PROMPTS_DIR / "object.yaml") as f:
            _PROMPT_CONFIG = yaml.safe_load(f)
    return _PROMPT_CONFIG


def _message_label(msg: Message) -> str:
    """Return a human-readable label for a message, so the LLM knows its type."""
    if msg.type == MessageType.EVENT:
        if msg.sender in ("__system__", "__external__"):
            return "External event"
        return f"Event from {msg.sender}"
    if msg.type == MessageType.ADMIN:
        return "System"
    if msg.sender == "__user__":
        return "User instruction"
    return f"Message from peer: {msg.sender}"


def build_system_prompt(
    definition: ObjectDefinition,
    current_state: dict,
    tools: str = "",
) -> str:
    """Build the system prompt from the YAML template and an ObjectDefinition."""
    config = _load_prompt_config()
    template = config["system_prompt"]

    peers = ""
    if definition.peers:
        peers = "\n".join(f"- {p.object_id}: {p.relationship}" for p in definition.peers)

    skills_str = ""
    if definition.skills:
        skills_str = "\n".join(f"- {s}" for s in definition.skills)

    event_sources = ""
    if definition.event_sources:
        event_sources = "\n".join(f"- {s}" for s in definition.event_sources)

    substitutions = {
        "object_id": definition.object_id,
        "role": definition.role,
        "state_description": definition.state_description or "(none)",
        "behavior": definition.behavior or "(none)",
        "skills": skills_str or "(none)",
        "peers": peers or "(none)",
        "event_sources": event_sources or "(none)",
        "seed_data": json.dumps(definition.seed_data, indent=2) if definition.seed_data else "(none)",
        "current_state": json.dumps(current_state, indent=2) if current_state else "(empty)",
        "tools": tools or "(none)",
    }
    result = template
    for key, value in substitutions.items():
        result = result.replace("{" + key + "}", value)
    return result


def _build_chat_messages(
    sys_prompt: str,
    history: Sequence[Message],
    message: Message,
) -> list[dict[str, str]]:
    """Build the initial chat message list with labeled history and new message.

    Returns a list starting with {"role": "system", ...}. Anthropic implementations
    should strip this entry and pass it separately.
    """
    msgs: list[dict[str, str]] = [{"role": "system", "content": sys_prompt}]
    if history:
        history_lines = [f"  [{_message_label(msg)}]: {msg.content}" for msg in history]
        msgs.append({"role": "user", "content": "[Past messages — already reflected in your state]\n" + "\n".join(history_lines)})
        msgs.append({"role": "assistant", "content": "Understood. What is the new message?"})
    msgs.append({"role": "user", "content": f"[{_message_label(message)}]: {message.content}"})
    return msgs


class LLMBrain(ABC):
    """Abstract interface for LLM processing backends."""

    @abstractmethod
    def call(
        self,
        messages: list[dict],
        schema: dict,
        *,
        object_id: str | None = None,
    ) -> tuple[LLMResponse, InferenceMetrics]:
        """Single LLM call. messages is the fully-assembled conversation (system + user turns).
        schema is the JSON schema for structured output.
        object_id is optional context used by MockBrain for script lookup.
        """
        ...

    @abstractmethod
    def react_call(
        self,
        messages: list[dict],
        *,
        object_id: str | None = None,
    ) -> tuple[ReactStep, InferenceMetrics]:
        """One ReAct step: returns a single thought + action and its metrics.

        The caller appends the step and its observation to `messages` and calls
        again until action == "finish".
        """
        ...


class OpenAIBrain(LLMBrain):
    """Brain backed by the OpenAI API."""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: Optional[str] = None,
        temperature: float = 0.0,
        seed: Optional[int] = 42,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai package required. Install with: pip install openai")

        self.model = model
        self._temperature = temperature
        self._seed = seed
        self._client = OpenAI(api_key=api_key or os.environ["OPENAI_API_KEY"])

    def call(
        self,
        messages: list[dict],
        schema: dict,
        *,
        object_id: str | None = None,
    ) -> tuple[LLMResponse, InferenceMetrics]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self._temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "llm_response",
                    "schema": schema,
                },
            },
        }
        if self._seed is not None:
            kwargs["seed"] = self._seed

        t0 = time.time()
        resp = self._client.chat.completions.create(**kwargs)
        latency_ms = (time.time() - t0) * 1000

        metrics = InferenceMetrics(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )

        raw = _safe_json_loads(resp.choices[0].message.content or "{}")
        return _parse_llm_result(raw), metrics

    def react_call(
        self,
        messages: list[dict],
        *,
        object_id: str | None = None,
    ) -> tuple[ReactStep, InferenceMetrics]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self._temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "react_step", "schema": LLM_REACT_SCHEMA},
            },
        }
        if self._seed is not None:
            kwargs["seed"] = self._seed

        t0 = time.time()
        resp = self._client.chat.completions.create(**kwargs)
        latency_ms = (time.time() - t0) * 1000

        metrics = InferenceMetrics(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        raw = _safe_json_loads(resp.choices[0].message.content or "{}")
        return _parse_react_step(raw), metrics


class AnthropicBrain(LLMBrain):
    """Brain backed by the Anthropic API."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        api_key: Optional[str] = None,
        temperature: float = 0.0,
    ) -> None:
        try:
            import anthropic as _anthropic
        except ImportError:
            raise ImportError("anthropic package required. Install with: pip install anthropic")

        self.model = model
        self._temperature = temperature
        self._client = _anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])

    @staticmethod
    def _enforce_strict_schema(schema: dict) -> None:
        """Recursively set additionalProperties: false on all object types."""
        if schema.get("type") == "object":
            schema["additionalProperties"] = False
        for key in ("properties", "$defs"):
            if key in schema:
                for sub in schema[key].values():
                    if isinstance(sub, dict):
                        AnthropicBrain._enforce_strict_schema(sub)
        for key in ("items", "anyOf", "oneOf", "allOf"):
            if key in schema:
                target = schema[key]
                if isinstance(target, dict):
                    AnthropicBrain._enforce_strict_schema(target)
                elif isinstance(target, list):
                    for item in target:
                        if isinstance(item, dict):
                            AnthropicBrain._enforce_strict_schema(item)

    def call(
        self,
        messages: list[dict],
        schema: dict,
        *,
        object_id: str | None = None,
    ) -> tuple[LLMResponse, InferenceMetrics]:
        # Anthropic requires system prompt as a separate parameter
        sys_prompt = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        user_messages = [m for m in messages if m["role"] != "system"]

        strict_schema = json.loads(json.dumps(schema))
        self._enforce_strict_schema(strict_schema)

        t0 = time.time()
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=4096,
            temperature=self._temperature,
            system=sys_prompt,
            messages=user_messages,
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": strict_schema,
                },
            },
        )
        latency_ms = (time.time() - t0) * 1000

        metrics = InferenceMetrics(
            input_tokens=getattr(resp.usage, "input_tokens", 0) if resp.usage else 0,
            output_tokens=getattr(resp.usage, "output_tokens", 0) if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )

        content_str = ""
        for block in resp.content:
            if hasattr(block, "text"):
                content_str += block.text

        raw = _safe_json_loads(content_str or "{}")
        return _parse_llm_result(raw), metrics

    def react_call(
        self,
        messages: list[dict],
        *,
        object_id: str | None = None,
    ) -> tuple[ReactStep, InferenceMetrics]:
        sys_prompt = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        user_messages = [m for m in messages if m["role"] != "system"]

        strict_schema = json.loads(json.dumps(LLM_REACT_SCHEMA))
        self._enforce_strict_schema(strict_schema)

        t0 = time.time()
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=4096,
            temperature=self._temperature,
            system=sys_prompt,
            messages=user_messages,
            output_config={"format": {"type": "json_schema", "schema": strict_schema}},
        )
        latency_ms = (time.time() - t0) * 1000

        metrics = InferenceMetrics(
            input_tokens=getattr(resp.usage, "input_tokens", 0) if resp.usage else 0,
            output_tokens=getattr(resp.usage, "output_tokens", 0) if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        content_str = "".join(block.text for block in resp.content if hasattr(block, "text"))
        raw = _safe_json_loads(content_str or "{}")
        return _parse_react_step(raw), metrics


@dataclass
class _ScriptEntry:
    response: LLMResponse
    metrics: InferenceMetrics = field(
        default_factory=lambda: InferenceMetrics(model="mock")
    )


@dataclass
class CallRecord:
    """Record of a call made to MockBrain."""
    object_id: str | None
    messages: list[dict]


class MockBrain(LLMBrain):
    """Deterministic scripted brain for testing."""

    def __init__(self) -> None:
        self._scripts: dict[str, list[_ScriptEntry]] = {}
        self._default_response: Optional[LLMResponse] = None
        self.call_log: list[CallRecord] = []
        self._react_queue: list[tuple[ReactStep, InferenceMetrics]] = []

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

    def call(
        self,
        messages: list[dict],
        schema: dict,
        *,
        object_id: str | None = None,
    ) -> tuple[LLMResponse, InferenceMetrics]:
        self.call_log.append(CallRecord(object_id=object_id, messages=messages))

        if object_id is not None:
            entries = self._scripts.get(object_id, [])
            if entries:
                entry = entries.pop(0)
                return entry.response, entry.metrics

        if self._default_response is not None:
            return self._default_response, InferenceMetrics(model="mock")

        # Fallback: echo the last user message with no state change
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        return (
            LLMResponse(
                updated_state={},
                reply=f"Echo: {last_user}",
                outgoing_messages=[],
                reasoning="No script configured",
            ),
            InferenceMetrics(model="mock"),
        )

    def react_call(
        self,
        messages: list[dict],
        *,
        object_id: str | None = None,
    ) -> tuple[ReactStep, InferenceMetrics]:
        # Return pre-converted steps before fetching a new scripted response.
        if self._react_queue:
            return self._react_queue.pop(0)

        # Fetch the next scripted LLMResponse and convert to ReactStep(s).
        response, metrics = self.call(messages, {}, object_id=object_id)

        if response.tool_calls:
            # One ReactStep per tool call — no finish yet (comes from next script).
            for tc in response.tool_calls:
                step = ReactStep(
                    thought=response.reasoning or "Calling tool.",
                    action="tool_call",
                    tool_call=tc,
                )
                self._react_queue.append((step, metrics))
        else:
            finish = ReactFinish(
                reply=response.reply,
                updated_state=response.updated_state,
                outgoing_messages=response.outgoing_messages,
                external_actions=response.external_actions,
            )
            step = ReactStep(
                thought=response.reasoning or "Done.",
                action="finish",
                finish=finish,
            )
            self._react_queue.append((step, metrics))

        return self._react_queue.pop(0)


def _safe_json_loads(text: str) -> dict:
    """Parse JSON from LLM output, tolerating trailing extra data."""
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        if "Extra data" in str(e):
            decoder = json.JSONDecoder()
            result, _ = decoder.raw_decode(text)
            return result
        raise


def _ensure_str(value: Any) -> str:
    """Coerce a value to string — handles cases where the LLM returns a dict instead of a string."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value)


def _parse_react_step(raw: dict) -> ReactStep:
    """Parse a raw LLM dict into a ReactStep."""
    thought = raw.get("thought", "")
    action = raw.get("action", "finish")

    if action == "tool_call":
        tc_data = raw.get("tool_call") or {}
        tc = ToolCall(
            id=tc_data.get("id", ""),
            tool=tc_data.get("tool", ""),
            arguments=tc_data.get("arguments", {}),
        )
        return ReactStep(thought=thought, action="tool_call", tool_call=tc)

    # action == "finish"
    f_data = raw.get("finish") or {}
    outgoing = [
        OutgoingMessage(recipient=m["recipient"], content=m["content"])
        for m in f_data.get("outgoing_messages", [])
        if isinstance(m, dict)
    ]
    external = [
        ExternalAction(
            system=ea["system"],
            action=ea["action"],
            content=ea["content"],
            params=ea.get("params", {}),
        )
        for ea in f_data.get("external_actions", [])
        if isinstance(ea, dict)
    ]
    finish = ReactFinish(
        reply=f_data.get("reply", ""),
        updated_state=f_data.get("updated_state") or {},
        outgoing_messages=outgoing,
        external_actions=external,
    )
    return ReactStep(thought=thought, action="finish", finish=finish)


def _parse_llm_result(result: Any) -> LLMResponse:
    """Parse the raw LLM result dict into LLMResponse."""
    if isinstance(result, dict):
        data = result
    else:
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

    tool_calls = []
    for tc in data.get("tool_calls", []):
        if isinstance(tc, dict):
            tool_calls.append(ToolCall(id=tc["id"], tool=tc["tool"], arguments=tc["arguments"]))
        elif isinstance(tc, ToolCall):
            tool_calls.append(tc)

    external_actions = []
    for ea in data.get("external_actions", []):
        if isinstance(ea, dict):
            external_actions.append(ExternalAction(
                system=ea["system"],
                action=ea["action"],
                content=ea["content"],
                params=ea.get("params", {}),
            ))
        elif isinstance(ea, ExternalAction):
            external_actions.append(ea)

    return LLMResponse(
        updated_state=data.get("updated_state") or {},
        reply=_ensure_str(data.get("reply", "")),
        outgoing_messages=outgoing,
        reasoning=data.get("reasoning", ""),
        tool_calls=tool_calls,
        external_actions=external_actions,
    )
