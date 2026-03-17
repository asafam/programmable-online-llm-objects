"""Mock services for simulating external systems in benchmarks."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolCall:
    """A recorded tool call made by an LLM-object."""
    service: str
    method: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScheduledEvent:
    """An event scheduled to fire at a given step."""
    step: int
    target: str
    content: str


@dataclass
class MockService:
    """Simulates an external service with scripted responses and recording.

    Three roles:
    - Pull: respond to tool calls with scripted state
    - Push: emit events at specific steps
    - Recording: capture outgoing calls for assertions
    """
    name: str
    _state: dict[str, Any] = field(default_factory=dict)
    _responses: dict[str, list[Any]] = field(default_factory=dict)
    _recordings: list[ToolCall] = field(default_factory=list)

    def set_state(self, key: str, value: Any) -> None:
        """Set a state value for the service."""
        self._state[key] = value

    def get_state(self, key: str, default: Any = None) -> Any:
        """Get a state value."""
        return self._state.get(key, default)

    def script_response(self, method: str, response: Any) -> None:
        """Add a scripted response for a method (consumed in order)."""
        self._responses.setdefault(method, []).append(response)

    def handle_call(self, method: str, args: dict[str, Any] | None = None) -> Any:
        """Handle a tool call — record it and return scripted response."""
        args = args or {}
        self._recordings.append(ToolCall(
            service=self.name,
            method=method,
            args=args,
        ))

        responses = self._responses.get(method, [])
        if responses:
            return responses.pop(0)

        # Default: return current state
        return dict(self._state)

    @property
    def recordings(self) -> list[ToolCall]:
        """All recorded tool calls."""
        return list(self._recordings)

    def clear_recordings(self) -> None:
        """Clear recorded tool calls."""
        self._recordings.clear()


class MockRegistry:
    """Registry of mock services with scheduled event queue."""

    def __init__(self) -> None:
        self._services: dict[str, MockService] = {}
        self._events: list[ScheduledEvent] = []
        self._step: int = 0

    def add_service(self, name: str) -> MockService:
        """Create and register a mock service."""
        svc = MockService(name=name)
        self._services[name] = svc
        return svc

    def get_service(self, name: str) -> Optional[MockService]:
        """Get a registered service by name."""
        return self._services.get(name)

    def schedule_event(self, step: int, target: str, content: str) -> None:
        """Schedule an event to fire at a given step."""
        self._events.append(ScheduledEvent(step=step, target=target, content=content))

    def advance(self) -> list[ScheduledEvent]:
        """Advance to the next step and return events due."""
        self._step += 1
        due = [e for e in self._events if e.step == self._step]
        return due

    @property
    def step(self) -> int:
        return self._step

    @property
    def services(self) -> dict[str, MockService]:
        return dict(self._services)

    def handle_call(self, service: str, method: str, args: dict[str, Any] | None = None) -> Any:
        """Route a tool call to the appropriate service."""
        svc = self._services.get(service)
        if svc is None:
            raise KeyError(f"Unknown service: {service}")
        return svc.handle_call(method, args)

    def all_recordings(self) -> dict[str, list[ToolCall]]:
        """All recordings grouped by service."""
        return {name: svc.recordings for name, svc in self._services.items()}
