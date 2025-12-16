from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from src.llm.base import AbstractLLM, ChatMessage, system_message, user_message


class Message(BaseModel):
    to: str
    message: str
    message_type: str = "default"


class StructuredResponse(BaseModel):
    response: str = ""
    state_updates: Optional[Dict[str, Any]] = None
    messages: List[Message] = []
    purpose_updates: List[str] = []  # New purposes to accumulate


class ReflectionResponse(BaseModel):
    should_act: bool = False  # Whether additional action is needed
    action_message: Optional[str] = None  # Message to send to self if action needed
    reasoning: Optional[str] = None  # Why action is/isn't needed (for debugging)


# Hand-crafted JSON schema for actor responses (strict mode compatible)
# Uses array of key-value pairs where values are JSON strings
ACTOR_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "response": {
            "type": "string",
            "description": "Natural language response to the message sender"
        },
        "state_updates": {
            "type": "array",
            "description": "Array of state field updates, each with 'key' (string) and 'value_json' (JSON-encoded string)",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value_json": {
                        "type": "string",
                        "description": "JSON-encoded value for the state field (e.g., '50', '\"hello\"', '[1,2,3]')"
                    }
                },
                "required": ["key", "value_json"],
                "additionalProperties": False
            }
        },
        "messages": {
            "type": "array",
            "description": "Messages to send to other actors",
            "items": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "message": {"type": "string"},
                    "message_type": {"type": "string"}
                },
                "required": ["to", "message", "message_type"],
                "additionalProperties": False
            }
        },
        "purpose_updates": {
            "type": "array",
            "description": "New purposes to accumulate",
            "items": {"type": "string"}
        }
    },
    "required": ["response", "state_updates", "messages", "purpose_updates"],
    "additionalProperties": False
}


class BaseActor:
    """Marker base class for all actors."""

    def __init__(self, name: str) -> None:
        self.name = name

    def receive(self, message: str, from_actor: str) -> str:  # pragma: no cover - interface hook
        raise NotImplementedError


class Actor(BaseActor):
    """Concrete actor with messaging and state management."""

    def __init__(
        self,
        name: str,
        llm: AbstractLLM,
        system_prompt: str,
        initial_state: Optional[Dict] = None,
        reflection_prompt: Optional[str] = None,
        enable_self_reflection: bool = False,
    ) -> None:
        super().__init__(name=name)
        self.llm = llm
        self.base_system_prompt = system_prompt  # Store the base prompt
        self.state: Dict = initial_state or {}
        self.message_bus = None  # set by MessageBus upon registration
        self.message_history: List[tuple[str, str]] = []  # List of (from_actor, message)
        self.proactivity_enabled: bool = False
        self.self_reflection_enabled: bool = enable_self_reflection
        self.reflection_prompt = reflection_prompt
        self.purposes: List[str] = []  # Accumulated purpose updates
        # No tools - state updates and messaging are handled via structured response parsing

    @property
    def system_prompt(self) -> str:
        """Build system prompt including base prompt and accumulated purposes."""
        prompt = self.base_system_prompt
        purpose = self.state.get('purpose', '')
        if self.purposes:
            purposes_text = "\n".join(f"- {p}" for p in self.purposes)
            full_purpose = f"{purpose}\n\nAdditional purposes:\n{purposes_text}"
        else:
            full_purpose = purpose
        return prompt.replace("{purpose}", full_purpose)

    @system_prompt.setter
    def system_prompt(self, value: str) -> None:
        """Allow direct setting of system prompt (for backward compatibility)."""
        self.base_system_prompt = value

    def set_message_bus(self, bus) -> None:
        self.message_bus = bus

    def set_tool_executor(self, executor) -> None:
        self.tool_executor = executor

    def update_system_prompt(self, new_prompt: str) -> None:
        """Update the actor's system prompt"""
        self.system_prompt = new_prompt

    # ---- Generic state helpers -------------------------------------------------
    def get_state(self) -> Dict:
        return self.state

    def set_state_field(self, key: str, value) -> None:
        print(f"[State] {self.name}: set {key} = {value}")
        self.state[key] = value

    def update_state(self, updates: Dict) -> None:
        print(f"[State] {self.name}: update {updates}")
        self.state.update(updates)

    def reflect_on_change(self, old_state: Dict, new_state: Dict) -> None:
        """Reflect on state changes and take action if needed by sending message to self."""
        if not self.reflection_prompt:
            return

        # Compute state diff
        changed_keys = set(old_state.keys()) | set(new_state.keys())
        diff_lines = []
        for key in sorted(changed_keys):
            old_val = old_state.get(key, 'not present')
            new_val = new_state.get(key, 'not present')
            if old_val != new_val:
                diff_lines.append(f"{key}: {old_val} -> {new_val}")
        state_diff = "\n".join(diff_lines) if diff_lines else "No changes detected"

        prompt = self.reflection_prompt.format(
            old_state=json.dumps(old_state, indent=2),
            new_state=json.dumps(new_state, indent=2),
            state_diff=state_diff
        )

        chat: List[ChatMessage] = [system_message(self.system_prompt), user_message(prompt)]
        reflection_response = self.llm.generate_structured(chat, ReflectionResponse)

        if reflection_response.should_act and reflection_response.action_message:
            print(f"[Reflection] {self.name}: {reflection_response.reasoning or 'Taking action based on state change'}")
            # Send message to self to trigger follow-up action
            if self.message_bus:
                self.message_bus.send(from_actor=self.name, to_actor=self.name, message=reflection_response.action_message)

    def speak(self, content: str) -> str:
        """LLM-backed natural language response with actor persona."""
        state_for_prompt = {k: v for k, v in self.state.items() if k != "known_actors"}
        full_prompt = self.system_prompt + f"\n\nCurrent state: {json.dumps(state_for_prompt)}. Use this current state to inform your response."
        messages: List[ChatMessage] = [system_message(full_prompt), user_message(content)]
        return self.llm.generate_text(messages)

    def _format_known_actors(self) -> Optional[str]:
        known = self.state.get("known_actors", {})
        if not known:
            return None
        lines = ["Known actors:"]
        for name, meta in known.items():
            desc = meta.get("purpose") or meta.get("description") or ""
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)

    def receive(self, message: str, from_actor: str) -> str:
        """Handle incoming natural-language message and parse structured response from LLM."""
        # Append to message history
        self.message_history.append((from_actor, message))

        state_for_prompt = {k: v for k, v in self.state.items() if k != "known_actors"}
        chat: List[ChatMessage] = [system_message(self.system_prompt + f"\n\nCurrent state: {json.dumps(state_for_prompt)}")]

        known_ctx = self._format_known_actors()
        if known_ctx:
            chat.append(system_message(known_ctx))

        chat.append(user_message(f"From {from_actor}: {message}"))

        # Get structured response from LLM using strict schema
        raw_response = self.llm.generate_structured(chat, ACTOR_RESPONSE_SCHEMA)

        # Convert state_updates array to dict, parsing JSON strings
        state_updates_dict = None
        if raw_response.get("state_updates"):
            state_updates_dict = {}
            for item in raw_response["state_updates"]:
                key = item["key"]
                value_json = item["value_json"]
                try:
                    value = json.loads(value_json)
                except json.JSONDecodeError:
                    # If JSON parsing fails, use the string as-is
                    value = value_json
                state_updates_dict[key] = value

        # Parse into StructuredResponse model for backward compatibility
        structured_response = StructuredResponse(
            response=raw_response.get("response") or "",
            state_updates=state_updates_dict,
            messages=[Message(**m) for m in raw_response.get("messages", [])],
            purpose_updates=raw_response.get("purpose_updates", [])
        )

        # Capture old state before updates
        old_state = self.state.copy()

        # Apply state changes if any
        if structured_response.state_updates:
            self.update_state(structured_response.state_updates)

        # Apply purpose updates if any
        if structured_response.purpose_updates:
            for purpose in structured_response.purpose_updates:
                print(f"[Purpose] {self.name}: Added purpose: {purpose}")
            self.purposes.extend(structured_response.purpose_updates)

        # Send messages if any
        if structured_response.messages and self.message_bus:
            for msg in structured_response.messages:
                self.message_bus.send(from_actor=self.name, to_actor=msg.to, message=msg.message)

        # Self-reflection if enabled and state changed
        if self.self_reflection_enabled and old_state != self.state:
            self.reflect_on_change(old_state, self.state)

        return structured_response.response

