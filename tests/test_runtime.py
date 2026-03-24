"""Tests for Runtime — processing loop, chaining, and API."""
import pytest
from pathlib import Path

from src.lnl import (
    LLMResponse,
    MockBrain,
    ObjectDefinition,
    OutgoingMessage,
    PeerDeclaration,
)
from src.lnl.runtime import Runtime
from src.lnl.tools import CodeExecutor, MockToolExecutor, ToolRegistry
from src.lnl.types import MessageType, ToolCall


@pytest.fixture
def brain():
    b = MockBrain()
    b.set_default(LLMResponse(updated_state={"status": "processed"}, reply="ok"))
    return b


@pytest.fixture
def rt(brain):
    return Runtime(brain, strict_peers=False)


class TestLoadDirectory:
    def test_loads_all_md_files(self, rt, tmp_path):
        (tmp_path / "a.md").write_text("# Alpha\n\n## Role\n\nDoes alpha work.\n")
        (tmp_path / "b.md").write_text("# Beta\n\n## Role\n\nDoes beta work.\n")
        (tmp_path / "not-md.txt").write_text("ignored")

        objects = rt.load_directory(tmp_path)

        assert len(objects) == 2
        ids = {o.object_id for o in objects}
        assert ids == {"alpha", "beta"}

    def test_load_file(self, rt, tmp_path):
        (tmp_path / "obj.md").write_text("# My Object\n\n## Role\n\nTest role.\n")

        obj = rt.load_file(tmp_path / "obj.md")

        assert obj.object_id == "my-object"


class TestSendRoutes:
    def test_send_routes_through_bus(self, rt):
        rt.create_object(ObjectDefinition(
            object_id="worker",
            role="Worker role.",
        ))

        results = rt.send("worker", "do work")

        assert len(results) == 1
        assert results[0].object_id == "worker"
        assert results[0].reply == "ok"

    def test_broadcast(self, rt):
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.create_object(ObjectDefinition(object_id="b", role="B"))

        results = rt.broadcast("hello all")

        assert len(results) == 2

    def test_publish_to_topic(self, rt):
        rt.create_object(ObjectDefinition(
            object_id="sub",
            role="Subscriber",
            subscriptions=["news"],
        ))
        rt.create_object(ObjectDefinition(object_id="other", role="Other"))

        results = rt.publish("news", "breaking")

        assert len(results) == 1
        assert results[0].object_id == "sub"


class TestChainProcessing:
    """Chaining tests — moved from test_bus.py since Runtime now owns processing."""

    def test_simple_chain_a_b_c(self):
        """A sends to B, B produces message to C. All results returned."""
        brain = MockBrain()
        brain.script("b", LLMResponse(
            updated_state={"status": "b got it"},
            reply="B reply",
            outgoing_messages=[OutgoingMessage(recipient="c", content="from B")],
        ))
        brain.script("c", LLMResponse(
            updated_state={"status": "c got it"},
            reply="C reply",
        ))

        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.create_object(ObjectDefinition(object_id="b", role="B"))
        rt.create_object(ObjectDefinition(object_id="c", role="C"))

        results = rt.send("b", "start chain", sender="a")

        assert len(results) == 2
        assert results[0].object_id == "b"
        assert results[1].object_id == "c"
        assert results[0].state_after == {"status": "b got it"}
        assert results[1].state_after == {"status": "c got it"}

    def test_chain_depth_limit(self):
        """Chain exceeding max depth stops processing (no exception — just stops)."""
        brain = MockBrain()
        # Each call produces a message back to self, creating a loop
        brain.set_default(LLMResponse(
            updated_state={},
            reply="ok",
            outgoing_messages=[OutgoingMessage(recipient="a", content="loop")],
        ))

        rt = Runtime(brain, max_chain_depth=3, strict_peers=False)
        rt.create_object(ObjectDefinition(object_id="a", role="A"))

        results = rt.send("a", "start loop")

        # Should process exactly max_chain_depth messages then stop
        assert len(results) == 3

    def test_bfs_ordering(self):
        """Mailbox model produces BFS: A→B and A→C, then B and C process before their children."""
        brain = MockBrain()
        brain.script("a", LLMResponse(
            updated_state={"status": "a done"},
            reply="A reply",
            outgoing_messages=[
                OutgoingMessage(recipient="b", content="from A"),
                OutgoingMessage(recipient="c", content="from A"),
            ],
        ))
        brain.script("b", LLMResponse(
            updated_state={"status": "b done"},
            reply="B reply",
        ))
        brain.script("c", LLMResponse(
            updated_state={"status": "c done"},
            reply="C reply",
        ))

        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.create_object(ObjectDefinition(object_id="b", role="B"))
        rt.create_object(ObjectDefinition(object_id="c", role="C"))

        results = rt.send("a", "start")

        assert len(results) == 3
        assert results[0].object_id == "a"
        # BFS: b and c process in registration order
        assert results[1].object_id == "b"
        assert results[2].object_id == "c"


class TestInjectEvent:
    def test_inject_event_delivers_and_processes(self):
        brain = MockBrain()
        brain.script("slack", LLMResponse(
            updated_state={"status": "event received"},
            reply="got it",
        ))

        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(object_id="slack", role="Slack service"))

        results = rt.inject_event("slack", "New message in #support")

        assert len(results) == 1
        assert results[0].object_id == "slack"
        assert rt.state("slack") == {"status": "event received"}

    def test_inject_event_chains_to_peers(self):
        brain = MockBrain()
        brain.script("slack", LLMResponse(
            updated_state={"status": "forwarded"},
            reply="forwarding",
            outgoing_messages=[OutgoingMessage(recipient="triage", content="urgent ticket")],
        ))
        brain.script("triage", LLMResponse(
            updated_state={"status": "triaged"},
            reply="handled",
        ))

        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(object_id="slack", role="Slack"))
        rt.create_object(ObjectDefinition(object_id="triage", role="Triage"))

        results = rt.inject_event("slack", "urgent message")

        assert len(results) == 2
        assert results[0].object_id == "slack"
        assert results[1].object_id == "triage"


class TestEventRegistry:
    def test_event_sources_registered(self):
        brain = MockBrain()
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(
            object_id="slack",
            role="Slack service",
            event_sources=["Slack webhook: incoming messages", "Slack webhook: reactions"],
        ))

        assert rt.event_registry == {
            "slack": ["Slack webhook: incoming messages", "Slack webhook: reactions"],
        }


class TestModify:
    def test_modify_preserves_state(self, brain):
        rt = Runtime(brain, strict_peers=False)
        brain.script("worker", LLMResponse(
            updated_state={"status": "has state"},
            reply="ok",
        ))
        rt.create_object(ObjectDefinition(object_id="worker", role="Original role."))

        rt.send("worker", "init")
        assert rt.state("worker") == {"status": "has state"}

        rt.modify("worker", role="New role.")

        assert rt.state("worker") == {"status": "has state"}
        assert rt.has_unsaved_modifications("worker")

    def test_add_remove_peer(self, rt):
        rt.create_object(ObjectDefinition(object_id="a", role="A"))

        rt.add_peer("a", "b", "helper")
        topo = rt.topology()
        assert "b" in topo["a"]

        rt.remove_peer("a", "b")
        topo = rt.topology()
        assert "b" not in topo["a"]


class TestTopology:
    def test_reflects_structure(self, rt):
        rt.create_object(ObjectDefinition(
            object_id="a",
            role="A",
            peers=[PeerDeclaration("b", "peer")],
        ))
        rt.create_object(ObjectDefinition(object_id="b", role="B"))

        topo = rt.topology()
        assert topo == {"a": ["b"], "b": []}


class TestPersistence:
    def test_save_reload_roundtrip(self, brain, tmp_path):
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(
            object_id="worker",
            role="Worker role.",
            state_description="Track tasks.",
        ))

        path = rt.save_object("worker", tmp_path / "worker.md")
        assert path.exists()

        # Reload in a new runtime
        rt2 = Runtime(brain, strict_peers=False)
        obj = rt2.load_file(path)
        assert obj.object_id == "worker"
        assert obj.definition.role == "Worker role."

    def test_save_clears_modified(self, rt, tmp_path):
        rt.create_object(ObjectDefinition(object_id="x", role="X"))
        rt.modify("x", role="Updated")
        assert rt.has_unsaved_modifications("x")

        rt.save_object("x", tmp_path / "x.md")
        assert not rt.has_unsaved_modifications("x")


class TestMetrics:
    def test_metrics_after_send(self, rt):
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.send("a", "hello")

        assert rt.metrics.messages_routed == 1

    def test_message_log(self, rt):
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.send("a", "hello")

        assert len(rt.message_log) == 1
        assert rt.message_log[0].delivered is True


class TestCreateFromText:
    def test_create_from_markdown(self, rt):
        obj = rt.create_object_from_text("# Worker\n\n## Role\n\nDoes work.\n")
        assert obj.object_id == "worker"
        results = rt.send("worker", "hi")
        assert len(results) == 1


class TestEventSources:
    """Runtime manages event sources — objects declare interests, Runtime handles plumbing."""

    def test_event_source_accessible(self):
        """Object with event_sources gets a provider accessible via get_event_source."""
        brain = MockBrain()
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(
            object_id="slack",
            role="Slack monitor",
            event_sources=["Slack webhook: messages"],
        ))

        source = rt.get_event_source("slack", "Slack webhook: messages")
        assert source is not None

    def test_no_event_source_without_declaration(self):
        """Object without event_sources has no source."""
        brain = MockBrain()
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(object_id="worker", role="Worker"))

        source = rt.get_event_source("worker", "anything")
        assert source is None

    def test_fire_delivers_event(self):
        """fire() on event source → process_pending → object receives event."""
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

        source = rt.get_event_source("slack", "Slack webhook: messages")
        source.fire("New message in #general: hello")
        results = rt.process_pending()

        assert len(results) == 1
        assert results[0].object_id == "slack"
        assert rt.state("slack") == {"status": "got message"}

    def test_fire_chains_to_peers(self):
        """Event fires → object processes → sends to peer → peer processes."""
        brain = MockBrain()
        brain.script("slack", LLMResponse(
            updated_state={"status": "forwarded"},
            reply="forwarding",
            outgoing_messages=[OutgoingMessage(recipient="triage", content="urgent ticket")],
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

        source = rt.get_event_source("slack", "Slack webhook: messages")
        source.fire("urgent message")
        results = rt.process_pending()

        assert len(results) == 2
        assert results[0].object_id == "slack"
        assert results[1].object_id == "triage"

    def test_multiple_event_sources(self):
        """Object with multiple event_sources gets separate providers."""
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(
            object_id="monitor",
            role="Multi-source monitor",
            event_sources=["Slack webhook: messages", "HubSpot: new deals"],
        ))

        slack = rt.get_event_source("monitor", "Slack webhook: messages")
        hubspot = rt.get_event_source("monitor", "HubSpot: new deals")
        assert slack is not None
        assert hubspot is not None
        assert slack is not hubspot


class TestLiveMode:
    def test_run_and_stop(self):
        """start() launches the loop, stop() shuts it down."""
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))
        rt = Runtime(brain, strict_peers=False)

        rt.start(poll_interval=0.01)
        assert rt.is_running

        rt.stop()
        assert not rt.is_running

    def test_submit_returns_results(self):
        """submit() enqueues work; results available after done is set."""
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={"status": "processed"}, reply="hello"))
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(object_id="worker", role="Worker"))

        rt.start(poll_interval=0.01)
        try:
            item = rt.submit("worker", "do work")
            item.done.wait(timeout=5.0)

            assert item.done.is_set()
            assert len(item.results) == 1
            assert item.results[0].object_id == "worker"
            assert item.results[0].reply == "hello"
        finally:
            rt.stop()

    def test_submit_chain(self):
        """Chained messages are processed within a single submit cycle."""
        brain = MockBrain()
        brain.script("a", LLMResponse(
            updated_state={"status": "a done"}, reply="A",
            outgoing_messages=[OutgoingMessage(recipient="b", content="from A")],
        ))
        brain.script("b", LLMResponse(updated_state={"status": "b done"}, reply="B"))
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.create_object(ObjectDefinition(object_id="b", role="B"))

        rt.start(poll_interval=0.01)
        try:
            item = rt.submit("a", "start")
            item.done.wait(timeout=5.0)
            assert len(item.results) == 2
        finally:
            rt.stop()

    def test_kill_object(self):
        """kill_object removes the object from the runtime."""
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.create_object(ObjectDefinition(object_id="b", role="B"))

        assert "a" in rt.topology()
        rt.kill_object("a")
        assert "a" not in rt.topology()
        assert "b" in rt.topology()

    def test_on_result_callback(self):
        """on_result fires for each ProcessingResult."""
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={}, reply="hi"))
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(object_id="w", role="W"))

        received = []
        rt.start(poll_interval=0.01, on_result=received.append)
        try:
            item = rt.submit("w", "hello")
            item.done.wait(timeout=5.0)
            assert len(received) == 1
            assert received[0].object_id == "w"
        finally:
            rt.stop()

    def test_inject_event_in_live_mode(self):
        """inject_event routes through run-loop in live mode."""
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={"status": "got event"}, reply="ok"))
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(
            object_id="listener",
            role="Listener",
            event_sources=["test-source"],
        ))

        rt.start(poll_interval=0.01)
        try:
            results = rt.inject_event("listener", "ping")
            assert len(results) == 1
            assert results[0].object_id == "listener"
            assert rt.state("listener") == {"status": "got event"}
        finally:
            rt.stop()
