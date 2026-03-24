"""Tests for EventGateway — event dispatch abstraction."""
from src.lnl import LLMResponse, MockBrain, ObjectDefinition, OutgoingMessage
from src.lnl.gateway import EventGateway
from src.lnl.runtime import Runtime


class TestEventGateway:
    def test_dispatch_with_event_source(self):
        """Object with event_sources: dispatch routes through the source."""
        brain = MockBrain()
        brain.script("slack", LLMResponse(
            updated_state={"status": "got message"}, reply="Received",
        ))
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(
            object_id="slack",
            role="Slack monitor",
            event_sources=["Slack webhook: messages"],
        ))

        gw = EventGateway(rt)
        results = gw.dispatch("slack", "New message in #general")

        assert len(results) == 1
        assert results[0].object_id == "slack"
        assert rt.state("slack") == {"status": "got message"}

    def test_dispatch_without_event_source(self):
        """Object without event_sources: dispatch falls back to inject_event."""
        brain = MockBrain()
        brain.script("worker", LLMResponse(
            updated_state={"status": "processed"}, reply="ok",
        ))
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(object_id="worker", role="Worker"))

        gw = EventGateway(rt)
        results = gw.dispatch("worker", "do work")

        assert len(results) == 1
        assert results[0].object_id == "worker"
        assert rt.state("worker") == {"status": "processed"}

    def test_dispatch_with_explicit_descriptor(self):
        """Explicit descriptor targets the specific event source."""
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(
            object_id="monitor",
            role="Multi-source monitor",
            event_sources=["Slack webhook: messages", "HubSpot: new deals"],
        ))

        gw = EventGateway(rt)
        results = gw.dispatch("monitor", "New deal", descriptor="HubSpot: new deals")

        assert len(results) == 1
        assert results[0].object_id == "monitor"

    def test_dispatch_chains_to_peers(self):
        """Dispatched event triggers chain: object sends to peer."""
        brain = MockBrain()
        brain.script("slack", LLMResponse(
            updated_state={"status": "forwarded"}, reply="forwarding",
            outgoing_messages=[OutgoingMessage(recipient="triage", content="urgent")],
        ))
        brain.script("triage", LLMResponse(
            updated_state={"status": "triaged"}, reply="handled",
        ))
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(
            object_id="slack",
            role="Slack monitor",
            event_sources=["Slack webhook: messages"],
        ))
        rt.create_object(ObjectDefinition(object_id="triage", role="Triage"))

        gw = EventGateway(rt)
        results = gw.dispatch("slack", "urgent message")

        assert len(results) == 2
        assert results[0].object_id == "slack"
        assert results[1].object_id == "triage"

    def test_dispatch_with_source_id(self):
        """Source parameter is passed through to the event."""
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(
            object_id="slack",
            role="Slack monitor",
            event_sources=["Slack webhook: messages"],
        ))

        gw = EventGateway(rt)
        results = gw.dispatch("slack", "hello", source="user-123")

        assert len(results) == 1
