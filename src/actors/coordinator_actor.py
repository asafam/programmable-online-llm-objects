from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional

import yaml
from pydantic import BaseModel

from src.llm.base import AbstractLLM, ChatMessage
from src.message_bus import MessageBus

from .base import Actor, Message


class CreateActorSpec(BaseModel):
    name: str
    purpose: str
    system_prompt: str
    initial_state: Optional[Dict] = None


class CoordinatorResponse(BaseModel):
    response: Optional[str] = None
    messages: List[Message] = []
    create_actors: List[CreateActorSpec] = []

# Hand-crafted JSON schema compatible with OpenAI's strict mode
# Removed 'state' field - coordinator doesn't need to update its own state
COORDINATOR_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "response": {
            "anyOf": [
                {"type": "string"},
                {"type": "null"}
            ],
            "description": "Natural language response to the user"
        },
        "messages": {
            "type": "array",
            "description": "Messages to send to actors",
            "items": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "message": {
                        "type": "string",
                        "description": "Natural language message content to the other actor"
                    }
                },
                "required": ["to", "message"],
                "additionalProperties": False
            }
        },
        "create_actors": {
            "type": "array",
            "description": "Actor specifications to create",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "purpose": {"type": "string"},
                    "system_prompt": {"type": "string"},
                    "initial_state": {
                        "type": "object",
                        "properties": {},  # Empty object - LLM will generate {} for initial_state
                        "additionalProperties": False
                    }
                },
                "required": ["name", "purpose", "system_prompt", "initial_state"],
                "additionalProperties": False
            }
        }
    },
    "required": ["response", "messages", "create_actors"],
    "additionalProperties": False
}


class CoordinatorActor(Actor):
    def __init__(self, llm: AbstractLLM, llm_factory: Callable[[str], AbstractLLM]) -> None:
        super().__init__(
            name="Coordinator",
            llm=llm,
            system_prompt="",  # Prompt loaded dynamically from YAML
            initial_state={"actors": {}},
        )
        self.llm_factory = llm_factory
        self.config = yaml.safe_load(open('config/system.yml'))
        actors = self.state.setdefault("actors", {})
        actors[self.name] = {
            "purpose": "Orchestrates actors and routing",
            "system_prompt": "",  # Loaded from YAML
            "initial_state": {},
        }

    def _render_system_prompt(self) -> str:
        registry = self._format_known_actors()
        if not registry:
            registry = "(none)"
        if not hasattr(self, '_loaded_prompt'):
            base_config_dir = os.path.join(os.path.dirname(__file__), '..', '..')
            prompt_path = os.path.join(base_config_dir, self.config['prompts']['coordinator'])
            with open(prompt_path, 'r') as f:
                self._loaded_prompt = yaml.safe_load(f)['system_prompt']
        return self._loaded_prompt.replace('[[registry]]', registry)

    def _register_actor_meta(self, args: Dict) -> None:
        name = args.get("name")
        if not name:
            return
        actors: Dict = self.state.setdefault("actors", {})
        if name in actors:
            return
        actors[name] = {
            "purpose": args.get("purpose"),
            "system_prompt": args.get("system_prompt"),
            "initial_state": args.get("initial_state", {}),
        }

    def _create_actor(
        self, name: str, purpose: str, system_prompt: str = "", initial_state: Optional[Dict] = None
    ) -> str:
        if name in self.message_bus.actors:
            return f"Actor '{name}' already exists. No new actor created."
        init_state = initial_state or {}
        init_state.setdefault("purpose", purpose)

        # Load base prompt from actor.yml
        base_config_dir = os.path.join(os.path.dirname(__file__), '..', '..')
        base_prompt_path = os.path.join(base_config_dir, self.config['prompts']['actor'])
        with open(base_prompt_path, 'r') as f:
            base_config = yaml.safe_load(f)

        base_prompt = base_config['system_prompt']
        reflection_prompt = base_config.get('reflection_prompt', '')

        state_schema = ", ".join(f"{k} ({type(v).__name__})" for k, v in init_state.items()) if init_state else ""
        related_actors = self._format_known_actors() or "None"
        full_system_prompt = base_prompt.replace('{purpose}', purpose).replace('{state_schema}', state_schema).replace('{related_actors}', related_actors)

        # Conditionally include heartbeat
        if self.config.get('heartbeat', {}).get('enabled', False):
            heartbeat_text = base_config.get('system_prompt_heartbeat', {}).get('instructions', '')
            full_system_prompt = full_system_prompt.replace('{system_prompt_heartbeat}', heartbeat_text)
        else:
            full_system_prompt = full_system_prompt.replace('{system_prompt_heartbeat}', '')

        # Conditionally include proactivity
        if self.config.get('features', {}).get('proactivity', False):
            proactivity_instructions = base_config.get('system_prompt_proactivity', {}).get('instructions', '')
            proactivity_format = base_config.get('system_prompt_proactivity', {}).get('format', '')
            full_system_prompt = full_system_prompt.replace('{proactivity::instructions}', proactivity_instructions).replace('{proactivity::format}', proactivity_format)
        else:
            full_system_prompt = full_system_prompt.replace('{proactivity::instructions}', '').replace('{proactivity::format}', '')

        # Append any additional system prompt
        if system_prompt:
            full_system_prompt += "\n\n" + system_prompt

        actor = Actor(
            name=name,
            llm=self.llm_factory(name),
            system_prompt=full_system_prompt,
            initial_state=init_state,
            reflection_prompt=reflection_prompt,
            enable_self_reflection=bool(reflection_prompt),
        )
        self.message_bus.register(actor)
        print(f"[Coordinator] Created actor '{name}' with purpose '{purpose}'")
        return f"Created actor '{name}' with purpose: {purpose}"

    def _format_known_actors(self) -> Optional[str]:
        actors = self.state.get("actors", {})
        if not actors:
            return None
        lines = ["Known actors:"]
        for name, meta in actors.items():
            purpose = meta.get("purpose", "")
            # Include current state if actor exists in message bus
            if name in self.message_bus.actors:
                current_state = self.message_bus.actors[name].state
                state_summary = ", ".join(f"{k}: {v}" for k, v in current_state.items() if k != "purpose")
                lines.append(f"- {name}: {purpose} (current state: {state_summary})")
            else:
                initial_state = meta.get("initial_state", {})
                state_keys = list(initial_state.keys()) if initial_state else []
                state_info = f" (state keys: {', '.join(state_keys)})" if state_keys else ""
                lines.append(f"- {name}: {purpose}{state_info}")
        return "\n".join(lines)

    def receive(self, message: str, from_actor: str) -> str:
        if from_actor != "User":
            return message  # Return actor responses directly to break loops

        # Append to message history
        self.message_history.append((from_actor, message))

        # Build chat with tools for the coordinator LLM
        rendered_prompt = self._render_system_prompt()
        chat: List[ChatMessage] = [ChatMessage(role="system", content=rendered_prompt)]
        chat.append(ChatMessage(role="user", content=message))

        # Get structured response from LLM using raw JSON schema (strict mode for determinism)
        raw_response = self.llm.generate_structured(chat, COORDINATOR_RESPONSE_SCHEMA)

        # Parse the raw response into CoordinatorResponse model
        structured_response = CoordinatorResponse(
            response=raw_response.get("response"),
            messages=[Message(**m) for m in raw_response.get("messages", [])],
            create_actors=[CreateActorSpec(**a) for a in raw_response.get("create_actors", [])]
        )

        # Create actors if any
        if structured_response.create_actors:
            for actor_spec in structured_response.create_actors:
                requested_name = actor_spec.name
                if requested_name and requested_name in self.state.get("actors", {}):
                    # Actor already exists
                    continue
                self._create_actor(
                    name=actor_spec.name,
                    purpose=actor_spec.purpose,
                    system_prompt=actor_spec.system_prompt,
                    initial_state=actor_spec.initial_state
                )
                self._register_actor_meta({
                    "name": actor_spec.name,
                    "purpose": actor_spec.purpose,
                    "system_prompt": actor_spec.system_prompt,
                    "initial_state": actor_spec.initial_state
                })

        # Send messages if any and collect responses
        actor_responses = []
        if structured_response.messages and self.message_bus:
            for msg in structured_response.messages:
                to_actor = msg.to
                if to_actor not in self.message_bus.actors:
                    # Create a default actor for unknown recipients
                    self._create_actor(
                        name=to_actor,
                        purpose=f"Handle {to_actor.lower()} related tasks",
                        initial_state={}
                    )
                response = self.message_bus.send(from_actor=self.name, to_actor=to_actor, message=msg.message)
                if response:
                    actor_responses.append(response)

        # If we have actor responses, return those instead of coordinator's initial response
        # The actor responses contain the actual information the user requested
        if actor_responses:
            return "\n".join(actor_responses)

        # Otherwise return the coordinator's own response
        return structured_response.response or ""
