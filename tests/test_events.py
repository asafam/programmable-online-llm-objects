"""Tests for event source providers and registry."""
import pytest

from src.lnl import (
    LLMResponse,
    MockBrain,
    ObjectDefinition,
)
from src.lnl.events import (
    EventEnvelope,
    EventSourceRegistry,
    InjectableEventSource,
)
from src.lnl.runtime import Runtime


class TestInjectableEventSource:
    def test_push_poll_cycle(self):
        src = InjectableEventSource("slack-webhook")
        src.push(EventEnvelope(source_descriptor="slack-webhook", content="hello"))
        src.push(EventEnvelope(source_descriptor="slack-webhook", content="world"))

        events = src.poll()
        assert len(events) == 2
        assert events[0].content == "hello"
        assert events[1].content == "world"

    def test_poll_clears_queue(self):
        src = InjectableEventSource("slack-webhook")
        src.push(EventEnvelope(source_descriptor="slack-webhook", content="hello"))

        src.poll()
        assert src.poll() == []

    def test_empty_poll(self):
        src = InjectableEventSource("slack-webhook")
        assert src.poll() == []

    def test_descriptor_property(self):
        src = InjectableEventSource("email-inbox")
        assert src.descriptor == "email-inbox"


class TestEventSourceRegistry:
    def test_bind_object_creates_injectable_fallback(self):
        reg = EventSourceRegistry()
        reg.bind_object("slack", ["Slack webhook: messages"])

        # Inject and poll
        reg.inject("slack", "hello")
        events = reg.poll_all()
        assert len(events) == 1
        assert events[0] == ("slack", EventEnvelope(
            source_descriptor="Slack webhook: messages",
            content="hello",
            source_id="__external__",
        ))

    def test_factory_match_takes_priority(self):
        """Registered factory providers are used instead of injectable fallback."""
        custom_source = InjectableEventSource("custom")
        reg = EventSourceRegistry()
        reg.register_factory(lambda desc: custom_source if "slack" in desc.lower() else None)
        reg.bind_object("slack", ["Slack webhook: messages"])

        # Push directly to the custom provider
        custom_source.push(EventEnvelope(
            source_descriptor="Slack webhook: messages",
            content="from custom",
        ))

        events = reg.poll_all()
        assert len(events) == 1
        assert events[0][1].content == "from custom"

    def test_inject_to_object_without_event_sources(self):
        """inject() creates ad-hoc binding for objects without declared event_sources."""
        reg = EventSourceRegistry()
        reg.inject("unknown-object", "hello")

        events = reg.poll_all()
        assert len(events) == 1
        assert events[0][0] == "unknown-object"
        assert events[0][1].content == "hello"
        assert events[0][1].source_descriptor == "__injected__"

    def test_inject_custom_source_id(self):
        reg = EventSourceRegistry()
        reg.bind_object("slack", ["Slack webhook: messages"])
        reg.inject("slack", "hello", source="slack-api")

        events = reg.poll_all()
        assert events[0][1].source_id == "slack-api"

    def test_poll_all_multiple_objects(self):
        reg = EventSourceRegistry()
        reg.bind_object("slack", ["Slack webhook: messages"])
        reg.bind_object("email", ["Email inbox: new mail"])

        reg.inject("slack", "slack msg")
        reg.inject("email", "email msg")

        events = reg.poll_all()
        assert len(events) == 2
        ids = {oid for oid, _env in events}
        assert ids == {"slack", "email"}

    def test_poll_all_clears_events(self):
        reg = EventSourceRegistry()
        reg.bind_object("slack", ["Slack webhook: messages"])
        reg.inject("slack", "hello")

        reg.poll_all()
        assert reg.poll_all() == []

    def test_bindings_summary(self):
        reg = EventSourceRegistry()
        reg.bind_object("slack", ["Slack webhook: messages", "Slack webhook: reactions"])
        reg.bind_object("email", ["Email inbox: new mail"])

        summary = reg.bindings_summary()
        assert summary == {
            "slack": ["Slack webhook: messages", "Slack webhook: reactions"],
            "email": ["Email inbox: new mail"],
        }

    def test_multiple_event_sources_per_object(self):
        reg = EventSourceRegistry()
        reg.bind_object("slack", ["Slack webhook: messages", "Slack webhook: reactions"])

        # inject() pushes to first provider
        reg.inject("slack", "hello")

        events = reg.poll_all()
        assert len(events) == 1
        assert events[0][1].source_descriptor == "Slack webhook: messages"


class TestRuntimeEventProviderIntegration:
    def test_object_receives_events_from_provider(self):
        """Object with event_sources receives events pushed through a custom provider."""
        custom_source = InjectableEventSource("custom")
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={"status": "event handled"}, reply="ok"))

        rt = Runtime(brain, strict_peers=False)
        rt._event_sources.register_factory(
            lambda desc: custom_source if "slack" in desc.lower() else None
        )
        rt.create_object(ObjectDefinition(
            object_id="slack",
            role="Slack service",
            event_sources=["Slack webhook: incoming messages"],
        ))

        # Push event through the provider, then trigger processing
        custom_source.push(EventEnvelope(
            source_descriptor="Slack webhook: incoming messages",
            content="New message in #general",
        ))
        # inject_event polls providers and dispatches; blocks until transaction commits
        # But we need to trigger processing — use send to an unrelated object
        # or inject to same object to trigger the loop
        rt.create_object(ObjectDefinition(object_id="dummy", role="dummy"))
        results = rt.send("dummy", "trigger")

        # The provider event should have been delivered to slack during the loop
        assert rt.state("slack") == {"status": "event handled"}

    def test_inject_event_backward_compat_with_event_sources(self):
        """inject_event() works for objects that have declared event_sources."""
        brain = MockBrain()
        brain.script("slack", LLMResponse(updated_state={"status": "injected"}, reply="ok"))

        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(
            object_id="slack",
            role="Slack service",
            event_sources=["Slack webhook: incoming messages"],
        ))

        results = rt.inject_event("slack", "hello from inject_event")
        assert len(results) == 1
        assert results[0].object_id == "slack"
        assert rt.state("slack") == {"status": "injected"}

    def test_inject_event_backward_compat_without_event_sources(self):
        """inject_event() works for objects without event_sources (ad-hoc injectable)."""
        brain = MockBrain()
        brain.script("worker", LLMResponse(updated_state={"status": "got event"}, reply="ok"))

        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(
            object_id="worker",
            role="Worker",
        ))

        results = rt.inject_event("worker", "hello")
        assert len(results) == 1
        assert rt.state("worker") == {"status": "got event"}

    def test_event_registry_reflects_bindings(self):
        brain = MockBrain()
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(
            object_id="slack",
            role="Slack",
            event_sources=["Slack webhook: messages"],
        ))

        assert rt.event_registry == {"slack": ["Slack webhook: messages"]}

    def test_register_provider_before_object_creation(self):
        """Provider factory registered before objects are created binds correctly."""
        events_received = []

        class TrackingProvider:
            def __init__(self):
                self._queue = []
            def poll(self):
                result = list(self._queue)
                self._queue.clear()
                return result
            def push(self, event):
                self._queue.append(event)
                events_received.append(event)

        tracker = TrackingProvider()
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={"status": "tracked"}, reply="ok"))

        rt = Runtime(brain, strict_peers=False)
        rt._event_sources.register_factory(lambda desc: tracker if "hubspot" in desc.lower() else None)

        rt.create_object(ObjectDefinition(
            object_id="hubspot",
            role="HubSpot CRM",
            event_sources=["HubSpot: new quote created"],
        ))

        # Push through provider
        tracker.push(EventEnvelope(
            source_descriptor="HubSpot: new quote created",
            content="Quote Q-1234",
        ))

        # Trigger processing via inject_event to a dummy
        rt.create_object(ObjectDefinition(object_id="dummy", role="D"))
        rt.send("dummy", "go")

        assert rt.state("hubspot") == {"status": "tracked"}
        assert len(events_received) == 1
