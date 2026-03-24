"""MessageBus — routes and delivers messages to LLM-object mailboxes."""
from __future__ import annotations

from dataclasses import dataclass, field

from .object import LLMObject
from .types import (
    Message,
    MessageLog,
    MessageType,
    ProcessingResult,
)


@dataclass
class BusMetrics:
    """Aggregate metrics for the message bus."""
    messages_routed: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


class MessageBus:
    """Routes messages to LLM-object mailboxes. Does not process — that's the Runtime's job."""

    def __init__(
        self,
        strict_peers: bool = True,
    ) -> None:
        self._objects: dict[str, LLMObject] = {}
        self._subscriptions: dict[str, set[str]] = {}  # topic -> set of object_ids
        self._strict_peers = strict_peers
        self._log: list[MessageLog] = []
        self._metrics = BusMetrics()

    # --- Registration ---

    def register(self, obj: LLMObject) -> None:
        """Register an LLM-object with the bus."""
        self._objects[obj.object_id] = obj
        # Auto-subscribe from definition
        for topic in obj.subscriptions:
            self.subscribe(obj.object_id, topic)

    def unregister(self, object_id: str) -> None:
        """Remove an LLM-object from the bus."""
        self._objects.pop(object_id, None)
        # Remove from all subscription sets
        for subscribers in self._subscriptions.values():
            subscribers.discard(object_id)

    def subscribe(self, object_id: str, topic: str) -> None:
        """Subscribe an object to a topic."""
        self._subscriptions.setdefault(topic, set()).add(object_id)

    # --- Delivery ---

    def deliver(self, message: Message) -> list[LLMObject]:
        """Route a message to recipient mailbox(es). Returns recipients."""
        # Validate peers for domain messages
        if (
            self._strict_peers
            and message.type == MessageType.DOMAIN
            and message.sender not in ("__user__", "__system__", "__external__")
            and message.recipient != "__broadcast__"
            and message.topic is None
        ):
            sender_obj = self._objects.get(message.sender)
            if sender_obj and message.recipient not in sender_obj.peer_ids:
                self._log.append(MessageLog(
                    message=message,
                    delivered=False,
                    error=f"Peer validation failed: '{message.recipient}' is not a peer of '{message.sender}'",
                ))
                return []

        recipients = self._resolve_recipients(message)

        for obj in recipients:
            obj.deliver(message)
            self._log.append(MessageLog(message=message, delivered=True))

        return recipients

    def _resolve_recipients(self, message: Message) -> list[LLMObject]:
        """Resolve message recipients to LLMObject instances."""
        # Broadcast
        if message.recipient == "__broadcast__":
            return [
                obj for oid, obj in self._objects.items()
                if oid != message.sender
            ]

        # Topic subscription
        if message.topic is not None:
            subscriber_ids = self._subscriptions.get(message.topic, set())
            return [
                self._objects[oid]
                for oid in subscriber_ids
                if oid in self._objects and oid != message.sender
            ]

        # Direct peer-to-peer
        obj = self._objects.get(message.recipient)
        return [obj] if obj else []

    # --- Metrics ---

    def record_processing(self, result: ProcessingResult) -> None:
        """Record metrics from a processed message (called by Runtime)."""
        self._metrics.messages_routed += 1
        if result.metrics:
            self._metrics.total_input_tokens += result.metrics.input_tokens
            self._metrics.total_output_tokens += result.metrics.output_tokens

    # --- Querying ---

    def topology(self) -> dict[str, list[str]]:
        """Return the communication graph from peer declarations."""
        graph: dict[str, list[str]] = {}
        for oid, obj in self._objects.items():
            graph[oid] = obj.peer_ids
        return graph

    @property
    def log(self) -> list[MessageLog]:
        return list(self._log)

    @property
    def metrics(self) -> BusMetrics:
        return self._metrics

    @property
    def objects(self) -> dict[str, LLMObject]:
        return dict(self._objects)
