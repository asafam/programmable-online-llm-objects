"""MessageBus — routes and delivers messages to LLM-object mailboxes."""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from typing import Callable, Optional

from .object import LLMObject
from .types import (
    Message,
    MessageLog,
    MessageType,
    ProcessingResult,
)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass
class BusMetrics:
    """Aggregate metrics for the message bus."""
    messages_routed: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


class MessageBus:
    """Routes messages to LLM-object mailboxes. Does not process — that's the Runtime's job."""

    def __init__(self) -> None:
        self._objects: dict[str, LLMObject] = {}
        self._subscriptions: dict[str, set[str]] = {}  # topic -> set of object_ids
        self._log: list[MessageLog] = []
        self._log_by_msg_id: dict[str, MessageLog] = {}  # msg.id -> log entry, for trace timing updates
        self._metrics = BusMetrics()
        self.on_message: Optional[Callable[[Message], None]] = None
        self._schedule_callback: Optional[Callable] = None
        self._max_chain_depth: int = 10  # set by Runtime; used to compute hop_depth at deliver time

    def set_max_chain_depth(self, depth: int) -> None:
        """Configure the runtime's max chain depth so the bus can compute hop_depth."""
        self._max_chain_depth = depth

    def set_schedule_callback(self, callback: Callable) -> None:
        """Set callback invoked when an object needs to be scheduled on the pool."""
        self._schedule_callback = callback

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
        recipients = self._resolve_recipients(message)

        for obj in recipients:
            obj.deliver(message, self._schedule_callback)
            hop_depth = max(0, self._max_chain_depth - message.depth_remaining)
            log_entry = MessageLog(
                message=message,
                delivered=True,
                received_at=_utcnow(),
                hop_depth=hop_depth,
            )
            self._log.append(log_entry)
            if message.id:
                self._log_by_msg_id[message.id] = log_entry
            if self.on_message:
                self.on_message(message)

        return recipients

    def log_synthetic(self, message: Message) -> None:
        """Record a synthetic message in the bus log WITHOUT delivering it.

        Used to surface non-delivery events (planner output, runtime decisions,
        debug markers) in the eval evidence stream so humans / judges / debug
        tools can see them inline with real bus traffic. The synthetic message
        is appended to `self._log` like any delivered message, but no object
        receives it. `on_message` is invoked so listeners (--debug-messages)
        can render it.
        """
        log_entry = MessageLog(
            message=message,
            delivered=False,
            received_at=_utcnow(),
            hop_depth=0,
        )
        self._log.append(log_entry)
        if message.id:
            self._log_by_msg_id[message.id] = log_entry
        if self.on_message:
            self.on_message(message)

    def update_log_timing(
        self,
        msg_id: str,
        *,
        started_at: Optional[datetime.datetime] = None,
        completed_at: Optional[datetime.datetime] = None,
        metrics=None,
    ) -> None:
        """Update processing timestamps and metrics on an existing log entry (by msg.id)."""
        entry = self._log_by_msg_id.get(msg_id)
        if entry is None:
            return
        if started_at is not None:
            entry.processing_started_at = started_at
        if completed_at is not None:
            entry.processing_completed_at = completed_at
        if metrics is not None:
            entry.metrics = metrics

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
