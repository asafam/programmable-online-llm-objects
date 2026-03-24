"""Tests for MessageBus — delivery and routing only (no processing)."""
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
from src.lnl.bus import MessageBus


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


class TestDelivery:
    def test_deliver_to_mailbox(self):
        """deliver() puts message in target's mailbox without processing."""
        brain = MockBrain()
        bus = MessageBus(strict_peers=False)
        obj = LLMObject(_defn("a"), brain)
        bus.register(obj)

        recipients = bus.deliver(_msg("__user__", "a", "hello"))

        assert len(recipients) == 1
        assert recipients[0].object_id == "a"
        assert obj.has_pending
        assert obj.mailbox[0].content == "hello"

    def test_deliver_to_nonexistent_object(self):
        """deliver() to unknown object returns empty list."""
        bus = MessageBus(strict_peers=False)
        recipients = bus.deliver(_msg("__user__", "unknown", "hello"))
        assert len(recipients) == 0

    def test_deliver_does_not_process(self):
        """deliver() only queues — object state should not change."""
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={"status": "changed"}, reply="ok"))

        bus = MessageBus(strict_peers=False)
        obj = LLMObject(_defn("a"), brain)
        bus.register(obj)

        bus.deliver(_msg("__user__", "a", "hello"))

        # State should still be empty — message was queued, not processed
        assert obj.state == {}
        assert obj.has_pending


class TestPeerValidation:
    def test_valid_peer_delivered(self):
        brain = MockBrain()
        bus = MessageBus(strict_peers=True)
        bus.register(LLMObject(
            _defn("a", peers=[PeerDeclaration("b", "helper")]),
            brain,
        ))
        obj_b = LLMObject(_defn("b"), brain)
        bus.register(obj_b)

        recipients = bus.deliver(_msg("a", "b", "hello"))
        assert len(recipients) == 1
        assert obj_b.has_pending

    def test_non_peer_blocked(self):
        brain = MockBrain()
        bus = MessageBus(strict_peers=True)
        bus.register(LLMObject(_defn("a", peers=[]), brain))
        obj_b = LLMObject(_defn("b"), brain)
        bus.register(obj_b)

        recipients = bus.deliver(_msg("a", "b", "hello"))
        assert len(recipients) == 0
        assert not obj_b.has_pending
        assert bus.log[-1].delivered is False
        assert "Peer validation" in bus.log[-1].error

    def test_user_bypasses_peer_check(self):
        brain = MockBrain()
        bus = MessageBus(strict_peers=True)
        obj = LLMObject(_defn("a"), brain)
        bus.register(obj)

        recipients = bus.deliver(_msg("__user__", "a", "hello"))
        assert len(recipients) == 1

    def test_system_bypasses_peer_check(self):
        brain = MockBrain()
        bus = MessageBus(strict_peers=True)
        obj = LLMObject(_defn("a"), brain)
        bus.register(obj)

        recipients = bus.deliver(_msg("__system__", "a", "hello"))
        assert len(recipients) == 1

    def test_external_bypasses_peer_check(self):
        brain = MockBrain()
        bus = MessageBus(strict_peers=True)
        obj = LLMObject(_defn("a"), brain)
        bus.register(obj)

        recipients = bus.deliver(_msg("__external__", "a", "hello"))
        assert len(recipients) == 1


class TestTopicSubscription:
    def test_topic_delivery(self):
        brain = MockBrain()
        bus = MessageBus(strict_peers=False)
        bus.register(LLMObject(_defn("pub"), brain))
        sub1 = LLMObject(_defn("sub1", subscriptions=["news"]), brain)
        sub2 = LLMObject(_defn("sub2", subscriptions=["news"]), brain)
        nosub = LLMObject(_defn("nosub"), brain)
        bus.register(sub1)
        bus.register(sub2)
        bus.register(nosub)

        msg = _msg("pub", "", "breaking news", topic="news")
        recipients = bus.deliver(msg)

        recipient_ids = {r.object_id for r in recipients}
        assert "sub1" in recipient_ids
        assert "sub2" in recipient_ids
        assert "nosub" not in recipient_ids
        assert sub1.has_pending
        assert sub2.has_pending
        assert not nosub.has_pending


class TestBroadcast:
    def test_broadcast_delivery(self):
        brain = MockBrain()
        bus = MessageBus(strict_peers=False)
        bus.register(LLMObject(_defn("sender"), brain))
        r1 = LLMObject(_defn("r1"), brain)
        r2 = LLMObject(_defn("r2"), brain)
        bus.register(r1)
        bus.register(r2)

        recipients = bus.deliver(_msg("sender", "__broadcast__", "hello all"))
        recipient_ids = {r.object_id for r in recipients}
        assert recipient_ids == {"r1", "r2"}
        assert "sender" not in recipient_ids


class TestLogging:
    def test_log_entries(self):
        brain = MockBrain()
        bus = MessageBus(strict_peers=False)
        bus.register(LLMObject(_defn("a"), brain))

        bus.deliver(_msg("__user__", "a", "hello"))

        assert len(bus.log) == 1
        assert bus.log[0].delivered is True


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
        bus = MessageBus(strict_peers=False)
        bus.register(LLMObject(_defn("a"), brain))
        bus.unregister("a")

        recipients = bus.deliver(_msg("__user__", "a", "hello"))
        assert len(recipients) == 0
