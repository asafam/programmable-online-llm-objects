"""LLMObject — the single runtime entity in the LNL system."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict

from .brain import LLMBrain
from .tools import ToolRegistry
from .types import (
    InferenceMetrics,
    Message,
    ObjectDefinition,
    OutgoingMessage,
    ProcessingResult,
)


class LLMObject:
    """An LLM-object: definition + brain + mutable NL state."""

    MAX_TOOL_ROUNDS = 5

    def __init__(
        self,
        definition: ObjectDefinition,
        brain: LLMBrain,
        tool_registry: ToolRegistry | None = None,
        tool_context_factory: object = None,
    ) -> None:
        self._definition = definition
        self._brain = brain
        self._state: dict = {}
        self._history: list[Message] = []
        self._mailbox: deque[Message] = deque()
        self._tool_registry = tool_registry
        self._tool_context_factory = tool_context_factory

    # --- Properties ---

    @property
    def object_id(self) -> str:
        return self._definition.object_id

    @property
    def state(self) -> dict:
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

    # --- Mailbox ---

    @property
    def has_pending(self) -> bool:
        """True if the mailbox has messages waiting to be processed."""
        return bool(self._mailbox)

    @property
    def mailbox(self) -> deque[Message]:
        return self._mailbox

    def deliver(self, message: Message) -> None:
        """Put a message in this object's mailbox."""
        self._mailbox.append(message)

    def process_next(self) -> ProcessingResult | None:
        """Process the next message from the mailbox."""
        if not self._mailbox:
            return None
        message = self._mailbox.popleft()
        return self.process_message(message)

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

        # Tool execution loop
        total_metrics = metrics
        prior_exchanges = []
        rounds = 0

        while response.tool_calls and self._tool_registry and rounds < self.MAX_TOOL_ROUNDS:
            rounds += 1
            context = self._tool_context_factory(self) if self._tool_context_factory else {}
            tool_results = [
                self._tool_registry.execute(tc, context)
                for tc in response.tool_calls
            ]
            prior_exchanges.append((response, tool_results))

            response, cont_metrics = self._brain.process_continuation(
                definition=self._definition,
                current_state=self._state,
                message=message,
                history=self._history,
                prior_exchanges=prior_exchanges,
            )
            total_metrics = _accumulate_metrics(total_metrics, cont_metrics)

        self._state = response.updated_state
        self._history.append(message)

        return ProcessingResult(
            object_id=self.object_id,
            reply=response.reply,
            outgoing_messages=response.outgoing_messages,
            state_before=state_before,
            state_after=self._state,
            metrics=total_metrics,
        )


    # --- Live Modification ---

    def modify_definition(self, **updates: object) -> None:
        """Change definition fields WITHOUT resetting state."""
        for key, value in updates.items():
            if not hasattr(self._definition, key):
                raise AttributeError(f"ObjectDefinition has no field '{key}'")
            setattr(self._definition, key, value)

    # --- Testing / Debugging ---

    def set_state(self, state: dict) -> None:
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


def _accumulate_metrics(base: InferenceMetrics, add: InferenceMetrics) -> InferenceMetrics:
    """Combine metrics from multiple LLM calls."""
    return InferenceMetrics(
        input_tokens=base.input_tokens + add.input_tokens,
        output_tokens=base.output_tokens + add.output_tokens,
        latency_ms=base.latency_ms + add.latency_ms,
        model=base.model or add.model,
    )
