"""LLMObject — the single runtime entity in the LNL system."""
from __future__ import annotations

from dataclasses import asdict

from .brain import LLMBrain
from .types import (
    Message,
    ObjectDefinition,
    OutgoingMessage,
    ProcessingResult,
)


class LLMObject:
    """An LLM-object: definition + brain + mutable NL state."""

    def __init__(self, definition: ObjectDefinition, brain: LLMBrain) -> None:
        self._definition = definition
        self._brain = brain
        self._state: str = ""
        self._history: list[Message] = []

    # --- Properties ---

    @property
    def object_id(self) -> str:
        return self._definition.object_id

    @property
    def state(self) -> str:
        return self._state

    @property
    def definition(self) -> ObjectDefinition:
        return self._definition

    @property
    def peer_ids(self) -> list[str]:
        return [p.object_id for p in self._definition.peers]

    @property
    def subscriptions(self) -> list[str]:
        return list(self._definition.subscriptions)

    @property
    def history(self) -> list[Message]:
        return list(self._history)

    # --- Core Processing ---

    def process_message(self, message: Message) -> ProcessingResult:
        """Process an incoming message through the brain and update state."""
        state_before = self._state

        response, metrics = self._brain.process(
            definition=self._definition,
            current_state=self._state,
            message=message,
            history=self._history,
        )

        self._state = response.updated_state
        self._history.append(message)

        return ProcessingResult(
            object_id=self.object_id,
            reply=response.reply,
            outgoing_messages=response.outgoing_messages,
            state_before=state_before,
            state_after=self._state,
            metrics=metrics,
        )

    # --- Live Modification ---

    def modify_definition(self, **updates: object) -> None:
        """Change definition fields WITHOUT resetting state."""
        for key, value in updates.items():
            if not hasattr(self._definition, key):
                raise AttributeError(f"ObjectDefinition has no field '{key}'")
            setattr(self._definition, key, value)

    # --- Testing / Debugging ---

    def set_state(self, state: str) -> None:
        """Set state directly (for testing)."""
        self._state = state

    def snapshot(self) -> dict:
        """Return a debug snapshot of the object."""
        return {
            "object_id": self.object_id,
            "state": self._state,
            "definition": asdict(self._definition),
            "history_length": len(self._history),
        }
