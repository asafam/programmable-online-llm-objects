"""MessageBus — routes messages between LLM-objects with peer validation and chaining."""
from __future__ import annotations

from dataclasses import dataclass, field

from .object import LLMObject
from .types import (
    InferenceMetrics,
    Message,
    MessageLog,
    MessageType,
    OutgoingMessage,
    ProcessingResult,
)


@dataclass
class BusMetrics:
    """Aggregate metrics for the message bus."""
    messages_routed: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


class ChainDepthExceeded(Exception):
    """Raised when message chain depth exceeds the limit."""
    pass


class MessageBus:
    """Routes messages between LLM-objects with validation and chaining."""

    def __init__(
        self,
        max_chain_depth: int = 10,
        strict_peers: bool = True,
    ) -> None:
        self._objects: dict[str, LLMObject] = {}
        self._subscriptions: dict[str, set[str]] = {}  # topic -> set of object_ids
        self._max_chain_depth = max_chain_depth
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

    # --- Sending ---

    def send(self, message: Message) -> list[ProcessingResult]:
        """Route a message and recursively process outgoing messages.

        Returns all ProcessingResults from the entire chain.
        """
        return self._send_recursive(message, depth=0)

    def _send_recursive(
        self, message: Message, depth: int
    ) -> list[ProcessingResult]:
        if depth >= self._max_chain_depth:
            self._log.append(MessageLog(
                message=message,
                delivered=False,
                error=f"Chain depth limit ({self._max_chain_depth}) exceeded",
            ))
            raise ChainDepthExceeded(
                f"Chain depth limit ({self._max_chain_depth}) exceeded"
            )

        results: list[ProcessingResult] = []

        # Determine recipients
        recipients = self._resolve_recipients(message)

        # Validate peers for domain messages
        if (
            self._strict_peers
            and message.type == MessageType.DOMAIN
            and message.sender not in ("__user__", "__system__")
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
                return results

        for obj in recipients:
            result = obj.process_message(message)
            self._metrics.messages_routed += 1
            if result.metrics:
                self._metrics.total_input_tokens += result.metrics.input_tokens
                self._metrics.total_output_tokens += result.metrics.output_tokens

            self._log.append(MessageLog(
                message=message,
                delivered=True,
                metrics=result.metrics,
            ))
            results.append(result)

            # Recursively process outgoing messages
            for out_msg in result.outgoing_messages:
                chained = Message(
                    sender=obj.object_id,
                    recipient=out_msg.recipient,
                    type=MessageType.DOMAIN,
                    content=out_msg.content,
                )
                chain_results = self._send_recursive(chained, depth + 1)
                results.extend(chain_results)

        return results

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
