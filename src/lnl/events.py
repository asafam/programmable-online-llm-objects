"""Event source providers — active event listening for LLM-objects."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Protocol


@dataclass
class EventEnvelope:
    """An event produced by an EventSourceProvider."""
    source_descriptor: str  # matches the event_source string from definition
    content: str
    source_id: str = "__external__"


class EventSourceProvider(Protocol):
    """Provides events for a particular source type."""

    def poll(self) -> list[EventEnvelope]:
        """Return pending events and clear them. Non-blocking."""
        ...

    def push(self, event: EventEnvelope) -> None:
        """Push an event into this provider."""
        ...


class InjectableEventSource:
    """Default provider — accepts programmatically injected events."""

    def __init__(self, source_descriptor: str) -> None:
        self._descriptor = source_descriptor
        self._queue: deque[EventEnvelope] = deque()

    @property
    def descriptor(self) -> str:
        return self._descriptor

    def poll(self) -> list[EventEnvelope]:
        events = list(self._queue)
        self._queue.clear()
        return events

    def push(self, event: EventEnvelope) -> None:
        self._queue.append(event)

    def fire(self, content: str, source: str = "__external__") -> None:
        """Convenience for test harnesses to push an event."""
        self.push(EventEnvelope(
            source_descriptor=self._descriptor,
            content=content,
            source_id=source,
        ))


class EventSourceRegistry:
    """Manages event source bindings for the runtime.

    Each object's declared event_sources are bound to concrete providers.
    Factories are tried first; unmatched sources get an InjectableEventSource.
    """

    def __init__(self) -> None:
        # object_id → [(source_descriptor, provider)]
        self._bindings: dict[str, list[tuple[str, EventSourceProvider]]] = {}
        # Registered provider factories: fn(descriptor) → provider | None
        self._factories: list[Callable[[str], EventSourceProvider | None]] = []

    def register_factory(self, factory: Callable[[str], EventSourceProvider | None]) -> None:
        """Register a factory that creates providers for matching descriptors."""
        self._factories.append(factory)

    def bind_object(self, object_id: str, event_sources: list[str]) -> None:
        """Create bindings for an object's declared event sources."""
        bindings: list[tuple[str, EventSourceProvider]] = []
        for descriptor in event_sources:
            provider = self._find_or_create_provider(descriptor)
            bindings.append((descriptor, provider))
        self._bindings[object_id] = bindings

    def _find_or_create_provider(self, descriptor: str) -> EventSourceProvider:
        """Try factories first; fall back to InjectableEventSource."""
        for factory in self._factories:
            provider = factory(descriptor)
            if provider is not None:
                return provider
        return InjectableEventSource(descriptor)

    def inject(self, recipient: str, content: str, source: str = "__external__") -> None:
        """Inject an event to a specific object.

        If the object has bindings, pushes to its first provider.
        If not, creates an ad-hoc injectable binding.
        """
        bindings = self._bindings.get(recipient)
        if bindings:
            descriptor = bindings[0][0]
            bindings[0][1].push(EventEnvelope(
                source_descriptor=descriptor,
                content=content,
                source_id=source,
            ))
        else:
            # Object has no event_sources — create ad-hoc injectable
            injectable = InjectableEventSource("__injected__")
            injectable.push(EventEnvelope(
                source_descriptor="__injected__",
                content=content,
                source_id=source,
            ))
            self._bindings[recipient] = [("__injected__", injectable)]

    def poll_all(self) -> list[tuple[str, EventEnvelope]]:
        """Poll all bindings, return (object_id, envelope) pairs."""
        results: list[tuple[str, EventEnvelope]] = []
        for object_id, bindings in self._bindings.items():
            for _descriptor, provider in bindings:
                for envelope in provider.poll():
                    results.append((object_id, envelope))
        return results

    def get_source(self, object_id: str, descriptor: str) -> EventSourceProvider | None:
        """Get the provider for a specific object + descriptor binding."""
        bindings = self._bindings.get(object_id)
        if bindings is None:
            return None
        for desc, provider in bindings:
            if desc == descriptor:
                return provider
        return None

    def unbind_object(self, object_id: str) -> None:
        """Remove all event source bindings for an object."""
        self._bindings.pop(object_id, None)

    def bindings_summary(self) -> dict[str, list[str]]:
        """Return object_id → list of bound source descriptors (for introspection)."""
        return {
            oid: [desc for desc, _prov in bindings]
            for oid, bindings in self._bindings.items()
        }
