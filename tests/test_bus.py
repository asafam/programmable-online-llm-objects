"""Tests for MessageBus (Phase 2)."""
import pytest

from src.lnl import (
    LLMObject,
    LLMResponse,
    Message,
    MessageType,
    MockBrain,
    ObjectDefinition,
    OutgoingMessage,
    PeerDeclaration,
)
from src.lnl.bus import ChainDepthExceeded, MessageBus


def _defn(oid: str, peers=None, subscriptions=None):
    return ObjectDefinition(
        object_id=oid,
        role=f"Role of {oid}",
        peers=peers or [],
        subscriptions=subscriptions or [],
    )


def _msg(sender: str, recipient: str, content: str, **kw) -> Message:
    return Message(
        sender=sender,
        recipient=recipient,
        type=kw.get("type", MessageType.DOMAIN),
        content=content,
        topic=kw.get("topic"),
    )


class TestChainProcessing:
    def test_simple_chain_a_b_c(self):
        """A sends to B, B produces message to C. All results returned."""
        brain = MockBrain()
        # B responds and sends a message to C
        brain.script("b", LLMResponse(
            updated_state="b got it",
            reply="B reply",
            outgoing_messages=[OutgoingMessage(recipient="c", content="from B")],
        ))
        brain.script("c", LLMResponse(
            updated_state="c got it",
            reply="C reply",
        ))

        bus = MessageBus(strict_peers=False)
        bus.register(LLMObject(_defn("a"), brain))
        bus.register(LLMObject(_defn("b"), brain))
        bus.register(LLMObject(_defn("c"), brain))

        results = bus.send(_msg("a", "b", "start chain"))

        assert len(results) == 2
        assert results[0].object_id == "b"
        assert results[1].object_id == "c"
        assert results[0].state_after == "b got it"
        assert results[1].state_after == "c got it"

    def test_chain_depth_limit(self):
        """Chain exceeding max depth raises ChainDepthExceeded."""
        brain = MockBrain()
        # Each object sends to the next, creating infinite loop
        brain.set_default(LLMResponse(
            updated_state="",
            reply="ok",
            outgoing_messages=[OutgoingMessage(recipient="a", content="loop")],
        ))

        bus = MessageBus(max_chain_depth=3, strict_peers=False)
        bus.register(LLMObject(_defn("a"), brain))

        with pytest.raises(ChainDepthExceeded):
            bus.send(_msg("__user__", "a", "start loop"))


class TestPeerValidation:
    def test_valid_peer_delivered(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state="", reply="ok"))

        bus = MessageBus(strict_peers=True)
        bus.register(LLMObject(
            _defn("a", peers=[PeerDeclaration("b", "helper")]),
            brain,
        ))
        bus.register(LLMObject(_defn("b"), brain))

        results = bus.send(_msg("a", "b", "hello"))
        assert len(results) == 1
        assert results[0].object_id == "b"

    def test_non_peer_blocked(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state="", reply="ok"))

        bus = MessageBus(strict_peers=True)
        bus.register(LLMObject(_defn("a", peers=[]), brain))
        bus.register(LLMObject(_defn("b"), brain))

        results = bus.send(_msg("a", "b", "hello"))
        assert len(results) == 0
        assert bus.log[-1].delivered is False
        assert "Peer validation" in bus.log[-1].error

    def test_user_bypasses_peer_check(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state="", reply="ok"))

        bus = MessageBus(strict_peers=True)
        bus.register(LLMObject(_defn("a"), brain))

        results = bus.send(_msg("__user__", "a", "hello"))
        assert len(results) == 1

    def test_system_bypasses_peer_check(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state="", reply="ok"))

        bus = MessageBus(strict_peers=True)
        bus.register(LLMObject(_defn("a"), brain))

        results = bus.send(_msg("__system__", "a", "hello"))
        assert len(results) == 1


class TestTopicSubscription:
    def test_topic_delivery(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state="", reply="ok"))

        bus = MessageBus(strict_peers=False)
        bus.register(LLMObject(_defn("pub"), brain))
        bus.register(LLMObject(_defn("sub1", subscriptions=["news"]), brain))
        bus.register(LLMObject(_defn("sub2", subscriptions=["news"]), brain))
        bus.register(LLMObject(_defn("nosub"), brain))

        msg = _msg("pub", "", "breaking news", topic="news")
        results = bus.send(msg)

        result_ids = {r.object_id for r in results}
        assert "sub1" in result_ids
        assert "sub2" in result_ids
        assert "nosub" not in result_ids


class TestBroadcast:
    def test_broadcast_delivery(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state="", reply="ok"))

        bus = MessageBus(strict_peers=False)
        bus.register(LLMObject(_defn("sender"), brain))
        bus.register(LLMObject(_defn("r1"), brain))
        bus.register(LLMObject(_defn("r2"), brain))

        results = bus.send(_msg("sender", "__broadcast__", "hello all"))
        result_ids = {r.object_id for r in results}
        assert result_ids == {"r1", "r2"}
        assert "sender" not in result_ids


class TestLoggingAndMetrics:
    def test_log_entries(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state="", reply="ok"))

        bus = MessageBus(strict_peers=False)
        bus.register(LLMObject(_defn("a"), brain))

        bus.send(_msg("__user__", "a", "hello"))

        assert len(bus.log) == 1
        assert bus.log[0].delivered is True

    def test_metrics_count(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state="", reply="ok"))

        bus = MessageBus(strict_peers=False)
        bus.register(LLMObject(_defn("a"), brain))
        bus.register(LLMObject(_defn("b"), brain))

        bus.send(_msg("__user__", "__broadcast__", "hello"))

        assert bus.metrics.messages_routed == 2


class TestTopology:
    def test_topology_returns_graph(self):
        brain = MockBrain()
        bus = MessageBus()
        bus.register(LLMObject(
            _defn("a", peers=[PeerDeclaration("b", "helper"), PeerDeclaration("c", "notifier")]),
            brain,
        ))
        bus.register(LLMObject(
            _defn("b", peers=[PeerDeclaration("a", "boss")]),
            brain,
        ))
        bus.register(LLMObject(_defn("c"), brain))

        topo = bus.topology()
        assert topo == {
            "a": ["b", "c"],
            "b": ["a"],
            "c": [],
        }


class TestUnregister:
    def test_unregister_removes_object(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state="", reply="ok"))

        bus = MessageBus(strict_peers=False)
        bus.register(LLMObject(_defn("a"), brain))
        bus.unregister("a")

        results = bus.send(_msg("__user__", "a", "hello"))
        assert len(results) == 0
