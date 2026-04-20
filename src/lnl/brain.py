"""LLM provider abstraction — Brain interface and implementations."""
from __future__ import annotations

import datetime
import json
import logging
import os
import time

logger = logging.getLogger(__name__)
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import yaml

from .types import (
    InferenceMetrics,
    LLMResponse,
    Message,
    MessageType,
    ObjectDefinition,
    OutgoingMessage,
    ReactFinish,
    ReactStep,
    StateDelta,
    ToolCall,
    ToolResult,
)

# JSON schema for the LLM response format (no tools)
LLM_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "updated_state": {
            "type": "string",
            "description": "Your complete updated state serialized as a JSON string, e.g. '{\"key\": \"value\"}'. Use '{}' if no state to store.",
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
        "state_update": {
            "type": "object",
            "description": "Optional. Emit ONLY when a value genuinely changed. Omit entirely if nothing changed — do not invent updates.",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": ["set", "delete", "append"],
                    "description": "set: add/update a key. delete: remove a key. append: add to a list.",
                },
                "key": {"type": "string", "description": "The state key to modify."},
                "value": {"description": "New value (set/append). Omit for delete."},
            },
            "required": ["op", "key"],
            "additionalProperties": False,
        },
        "finish": {
            "type": "object",
            "description": "Present only when action=finish.",
            "properties": {
                "reply": {"type": "string", "description": "Reply to the message sender."},
                "outgoing_messages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "recipient": {"type": "string"},
                            "content": {"type": "string"},
                            "expects_reply": {
                                "type": "boolean",
                                "description": "Set true for Ask messages — when you need information back before you can continue. Leave false (default) for Tell messages: notifications, writes, and one-way forwards.",
                            },
                            "reference": {
                                "type": "string",
                                "description": "Optional short tag for correlating a reply to this Ask message. Include when expects_reply is true so you can match the reply via [in-reply-to: <reference>].",
                            },
                        },
                        "required": ["recipient", "content"],
                        "additionalProperties": False,
                    },
                },
                "updated_definition": {
                    "type": "object",
                    "description": "Optional. Only include when responding to an Admin message that changes your behavior. Include only the fields that change: role, behavior, peers.",
                    "properties": {
                        "role": {"type": "string"},
                        "behavior": {"type": "string"},
                        "peers": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "object_id": {"type": "string"},
                                    "relationship": {"type": "string"},
                                },
                                "required": ["object_id", "relationship"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["reply"],
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
    if msg.type == MessageType.HEARTBEAT:
        return "Heartbeat"
    if msg.type == MessageType.EVENT:
        if msg.sender in ("__system__", "__external__"):
            return "External event"
        return f"Event from {msg.sender}"
    if msg.type == MessageType.ADMIN:
        return "Admin"
    if msg.sender == "__user__":
        return "User instruction"
    if msg.expects_reply:
        return f"Ask from peer: {msg.sender}"   # sender expects a reply
    return f"Tell from peer: {msg.sender}"       # fire-and-forget, no reply expected


def _peer_interaction_loop(pending_timeout_seconds: float, heartbeat_interval_seconds: float) -> str:
    return f"""
  ## Peer Sends: Ask vs. Tell

  Every outgoing message is either an **Ask** or a **Tell**. Set `expects_reply`
  accordingly on each entry in `outgoing_messages`.

  **Tell (NOTIFY / WRITE / FORWARD) — `expects_reply: false` (the DEFAULT):**
  You are informing, storing, or passing an event along — no reply expected.
  The recipient is told this is a Tell so it knows not to reply. Send the message
  and immediately proceed to your next action or finish. Do NOT set a PENDING flag.
  Examples:
  - Forwarding an event to the next object in the chain
  - Writing a record to a store
  - Posting a Slack notification
  - Triggering a downstream action

  **Ask (QUERY) — `expects_reply: true`:**
  You need information back from the peer before you can continue. The recipient
  is told this is an Ask so it knows to reply. Send your question with
  `expects_reply: true`, set a `_pending` block in state, and STOP. When their
  REPLY arrives, read your state to recall your intent, complete the goal, then
  clear `_pending`.
  Use this ONLY when you cannot proceed without the peer's answer. Examples:
  - Looking up a manager's name/email before addressing a notification
  - Checking policy rules before routing a ticket
  - Querying an org directory before assigning work

  **Saving a continuation when going PENDING:** Your `_pending` block must contain
  everything needed to resume without re-reading the original trigger.
  Every message is prefixed with "[system time: <timestamp>] [msg-id: <id>]" — use
  the timestamp as `started_at` so you know when you went pending.
  When an Ask reply arrives, it carries "[in-reply-to: <id>]" where <id> matches
  the `reference` you set on the outgoing message — use it to match multiple
  concurrent pending requests:
    _pending: {{
      waiting_for: "<peer-id>",
      reference: "<short tag you set on the outgoing message>",
      question: "<exact question sent>",
      intent: "<what you will do once you have the answer>",
      started_at: "<timestamp from the [system time: ...] prefix of the current message>",
      context: {{ <all data from the original message needed to complete the action> }}
    }}

  **Rule:** If you already have all the data you need to complete your action, send
  Tell messages to all relevant peers and finish in one step. Never set PENDING
  after a Tell send.

  **Heartbeat recovery:** A Heartbeat arrives every {heartbeat_interval_seconds:.0f}s.
  Like all messages, it is prefixed with "[system time: <timestamp>]" — this is the
  authoritative current time. On each Heartbeat, check your state for a `_pending`
  block. If present:
  - Read the current time from the "[system time: ...]" prefix of the Heartbeat.
  - Compute elapsed seconds = current_time - `_pending.started_at`.
  - If elapsed < {pending_timeout_seconds:.0f}s: re-send the original question to
    `_pending.waiting_for` (with `expects_reply: true`) and remain PENDING (do not
    update `started_at`).
  - If elapsed >= {pending_timeout_seconds:.0f}s: the peer is unresponsive. Use
    `_pending.intent` and `_pending.context` to take the best available fallback
    action (proceed with a default, escalate, or log a failure), then clear
    `_pending` from state."""


def build_system_prompt(
    definition: ObjectDefinition,
    current_state,  # str (from LLM) or dict (from mock scripts)
    tools: str = "",
    react_cross_objects: bool = True,
    pending_timeout_seconds: float = 90.0,
    heartbeat_interval_seconds: float = 30.0,
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
        "behavior": definition.behavior or "(none)",
        "skills": skills_str or "(none)",
        "peers": peers or "(none)",
        "event_sources": event_sources or "(none)",
        "current_state": (json.dumps(current_state, indent=2) if isinstance(current_state, dict) else current_state.strip()) if current_state else "(empty)",
        "tools": tools or "(none)",
        "peer_interaction_loop": _peer_interaction_loop(pending_timeout_seconds, heartbeat_interval_seconds) if react_cross_objects else "",
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
    ts = message.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    id_tag = f" [msg-id: {message.id}]" if message.id else ""
    reply_tag = f" [in-reply-to: {message.in_reply_to}]" if message.in_reply_to else ""
    msgs.append({"role": "user", "content": f"[system time: {ts}]{id_tag}{reply_tag} [{_message_label(message)}]: {message.content}"})
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

        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                f"OpenAI response truncated (finish_reason=length) for object {object_id}. "
                "The output exceeded the model's max_tokens limit."
            )
        raw = _safe_json_loads(choice.message.content or "{}")
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
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                f"OpenAI response truncated (finish_reason=length) for object {object_id}. "
                "The output exceeded the model's max_tokens limit."
            )
        raw = _safe_json_loads(choice.message.content or "{}")
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
        self._client = _anthropic.Anthropic(
            api_key=api_key or os.environ["ANTHROPIC_API_KEY"],
            timeout=600.0,  # 10 min HTTP timeout — prevents httpx.ReadTimeout on slow responses
        )

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
            max_tokens=16000,
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

        if resp.stop_reason == "max_tokens":
            raise RuntimeError(
                f"Anthropic response truncated (stop_reason=max_tokens) for object {object_id}. "
                "The output exceeded the max_tokens limit."
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
        # Patch AFTER enforce_strict: give `state_update.value` an explicit wildcard schema
        # (empty schema = any value in JSON Schema) so Anthropic's validator accepts it.
        # Done after enforce_strict so the wildcard isn't overridden.
        try:
            strict_schema["properties"]["state_update"]["properties"]["value"] = {
                "description": "New value (set/append). Omit for delete.",
            }
        except (KeyError, TypeError):
            pass

        t0 = time.time()
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=16000,
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
        except Exception as e:
            # output_config may be unsupported for this model/version — fall back to
            # unstructured output and rely on _safe_json_loads to parse the response.
            if "output_config" in str(e) or "json_schema" in str(e) or "400" in str(e):
                logger.debug(
                    "AnthropicBrain: output_config rejected (%s), falling back to unstructured call.",
                    e,
                )
                resp = self._client.messages.create(
                    model=self.model,
                    max_tokens=16000,
                    temperature=self._temperature,
                    system=sys_prompt,
                    messages=user_messages,
                )
            else:
                raise

        latency_ms = (time.time() - t0) * 1000

        metrics = InferenceMetrics(
            input_tokens=getattr(resp.usage, "input_tokens", 0) if resp.usage else 0,
            output_tokens=getattr(resp.usage, "output_tokens", 0) if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        if resp.stop_reason == "max_tokens":
            raise RuntimeError(
                f"Anthropic response truncated (stop_reason=max_tokens) for object {object_id}. "
                "The output exceeded the max_tokens limit."
            )
        content_str = "".join(block.text for block in resp.content if hasattr(block, "text"))
        try:
            raw = _safe_json_loads(content_str or "{}")
        except json.JSONDecodeError:
            logger.warning(
                "AnthropicBrain: JSON parse failed for object %s. "
                "Response preview: %r",
                object_id,
                (content_str or "")[:200],
            )
            raw = {}
        return _parse_react_step(raw), metrics


class GeminiBrain(LLMBrain):
    """Brain backed by the Google Gemini API."""

    def __init__(
        self,
        model: str = "gemini-2.5-pro",
        api_key: Optional[str] = None,
        temperature: float = 0.0,
    ) -> None:
        try:
            from google import genai
            from google.genai import types as genai_types
        except ImportError:
            raise ImportError("google-genai package required. Install with: pip install google-genai")

        self.model = model
        self._temperature = temperature
        resolved_key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Google API key required. Set GOOGLE_API_KEY in your environment or .env file, "
                "or pass api_key to GeminiBrain."
            )
        self._client = genai.Client(api_key=resolved_key)
        self._types = genai_types

    def _to_gemini_contents(self, messages: list[dict]) -> tuple[str, list]:
        """Split system prompt and convert messages to Gemini contents format."""
        system_parts = []
        contents = []
        for m in messages:
            if m["role"] == "system":
                system_parts.append(m["content"])
            else:
                role = "model" if m["role"] == "assistant" else m["role"]
                contents.append(
                    self._types.Content(
                        role=role,
                        parts=[self._types.Part(text=m["content"])],
                    )
                )
        return "\n".join(system_parts), contents

    def _generate_json(self, messages: list[dict], schema: dict) -> tuple[str, Any]:
        system_instruction, contents = self._to_gemini_contents(messages)
        t0 = time.time()
        config = self._types.GenerateContentConfig(
            temperature=self._temperature,
            max_output_tokens=8192,
            response_mime_type="application/json",
            response_schema=schema,
        )
        if system_instruction:
            config.system_instruction = system_instruction
        resp = self._client.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )
        latency_ms = (time.time() - t0) * 1000
        metrics = InferenceMetrics(
            input_tokens=getattr(getattr(resp, "usage_metadata", None), "prompt_token_count", 0) or 0,
            output_tokens=getattr(getattr(resp, "usage_metadata", None), "candidates_token_count", 0) or 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        return resp.text or "{}", metrics

    def call(
        self,
        messages: list[dict],
        schema: dict,
        *,
        object_id: str | None = None,
    ) -> tuple[LLMResponse, InferenceMetrics]:
        text, metrics = self._generate_json(messages, schema)
        raw = _safe_json_loads(text)
        return _parse_llm_result(raw), metrics

    def react_call(
        self,
        messages: list[dict],
        *,
        object_id: str | None = None,
    ) -> tuple[ReactStep, InferenceMetrics]:
        text, metrics = self._generate_json(messages, LLM_REACT_SCHEMA)
        raw = _safe_json_loads(text)
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

    def script_react(
        self,
        step: ReactStep,
        metrics: Optional[InferenceMetrics] = None,
    ) -> None:
        """Enqueue a pre-built ReactStep directly (bypasses LLMResponse conversion).

        Useful for testing state_update deltas and other ReAct-specific fields
        without polluting LLMResponse.
        """
        self._react_queue.append((step, metrics or InferenceMetrics(model="mock")))

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
                updated_state="",
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
            )
            step = ReactStep(
                thought=response.reasoning or "Done.",
                action="finish",
                finish=finish,
            )
            self._react_queue.append((step, metrics))

        return self._react_queue.pop(0)


def _sanitize_json_control_chars(text: str) -> str:
    """Escape literal control characters (newlines, tabs, carriage returns) inside JSON
    string values.  The LLM sometimes emits unescaped newlines in long 'thought' or
    'reply' fields, which are valid in prose but illegal in JSON strings.

    Uses a simple character-level state machine that tracks whether we are inside a
    JSON string so we can escape only the characters that need it.
    """
    out: list[str] = []
    in_string = False
    skip_next = False
    for ch in text:
        if skip_next:
            skip_next = False
            out.append(ch)
            continue
        if ch == "\\" and in_string:
            skip_next = True   # next char is an escape sequence — pass through as-is
            out.append(ch)
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string:
            if ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def _safe_json_loads(text: str) -> dict:
    """Parse JSON from LLM output, tolerating markdown fences, preamble text,
    and literal control characters inside string values."""
    text = text.strip()
    if not text:
        return {}
    # Strip optional markdown code fences (```json ... ``` or ``` ... ```)
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
        text = text.strip()
    if not text:
        return {}

    def _try_parse(s: str) -> dict:
        """Try json.loads, then Extra-data fallback, then brace-search fallback."""
        try:
            return json.loads(s)
        except json.JSONDecodeError as e:
            if "Extra data" in str(e):
                decoder = json.JSONDecoder()
                result, _ = decoder.raw_decode(s)
                return result
            # Fallback: find first '{' and try to parse from there
            # (handles preamble text like "Here is the response:\n{...}")
            brace = s.find("{")
            if brace > 0:
                try:
                    decoder = json.JSONDecoder()
                    result, _ = decoder.raw_decode(s, brace)
                    return result
                except json.JSONDecodeError:
                    pass
            raise

    try:
        return _try_parse(text)
    except json.JSONDecodeError:
        # Last resort: escape literal control characters inside strings and retry
        sanitized = _sanitize_json_control_chars(text)
        return _try_parse(sanitized)


def _ensure_str(value: Any) -> str:
    """Coerce a value to string — handles cases where the LLM returns a dict instead of a string."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value)


def _parse_state(raw_state: Any) -> str:
    """Normalize updated_state to a plain string."""
    if isinstance(raw_state, str):
        return raw_state.strip()
    if isinstance(raw_state, dict):
        # Fallback: model returned an object despite string schema — serialize it.
        return json.dumps(raw_state)
    return ""


def _parse_state_delta(raw: dict) -> Optional[StateDelta]:
    """Parse an optional state_update dict into a StateDelta, or None if absent/invalid."""
    if not isinstance(raw, dict):
        return None
    op = raw.get("op")
    key = raw.get("key")
    if not op or not key:
        return None
    return StateDelta(op=op, key=key, value=raw.get("value"))


def _parse_react_step(raw: dict) -> ReactStep:
    """Parse a raw LLM dict into a ReactStep."""
    thought = raw.get("thought", "")
    action = raw.get("action", "finish")

    # state_update is optional at any step
    state_update = _parse_state_delta(raw.get("state_update") or {})

    if action == "tool_call":
        tc_data = raw.get("tool_call") or {}
        tc = ToolCall(
            id=tc_data.get("id", ""),
            tool=tc_data.get("tool", ""),
            arguments=tc_data.get("arguments", {}),
        )
        return ReactStep(thought=thought, action="tool_call", state_update=state_update, tool_call=tc)

    # action == "finish"
    f_data = raw.get("finish") or {}
    updated_state = _parse_state(f_data.get("updated_state"))

    raw_msgs = f_data.get("outgoing_messages", []) or []
    outgoing = [
        OutgoingMessage(
            recipient=m["recipient"],
            content=m["content"],
            reference=m.get("reference"),
            expects_reply=bool(m.get("expects_reply", False)),
        )
        for m in raw_msgs
        if isinstance(m, dict)
    ]
    updated_def = f_data.get("updated_definition") or None
    if updated_def == {}:
        updated_def = None
    finish = ReactFinish(
        reply=f_data.get("reply", ""),
        updated_state=updated_state,
        outgoing_messages=outgoing,
        updated_definition=updated_def,
    )
    return ReactStep(thought=thought, action="finish", state_update=state_update, finish=finish)


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
            outgoing.append(OutgoingMessage(recipient=m["recipient"], content=m["content"], reference=m.get("reference")))
        elif isinstance(m, OutgoingMessage):
            outgoing.append(m)

    tool_calls = []
    for tc in data.get("tool_calls", []):
        if isinstance(tc, dict):
            tool_calls.append(ToolCall(id=tc["id"], tool=tc["tool"], arguments=tc["arguments"]))
        elif isinstance(tc, ToolCall):
            tool_calls.append(tc)

    return LLMResponse(
        updated_state=_parse_state(data.get("updated_state")),
        reply=_ensure_str(data.get("reply", "")),
        outgoing_messages=outgoing,
        reasoning=data.get("reasoning", ""),
        tool_calls=tool_calls,
    )
