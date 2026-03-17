"""Tests for LLMObject (Phase 1)."""
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


def _make_definition(**overrides):
    defaults = dict(
        object_id="test-obj",
        role="A test object.",
        state_description="Track a counter.",
    )
    defaults.update(overrides)
    return ObjectDefinition(**defaults)


def _user_msg(content: str, recipient: str = "test-obj") -> Message:
    return Message(
        sender="__user__",
        recipient=recipient,
        type=MessageType.DOMAIN,
        content=content,
    )


class TestLLMObjectBasics:
    def test_single_message_updates_state(self):
        brain = MockBrain()
        brain.script("test-obj", LLMResponse(
            updated_state="counter=1",
            reply="Incremented.",
            outgoing_messages=[],
            reasoning="First message.",
        ))
        obj = LLMObject(_make_definition(), brain)

        result = obj.process_message(_user_msg("increment"))

        assert result.object_id == "test-obj"
        assert result.reply == "Incremented."
        assert result.state_before == ""
        assert result.state_after == "counter=1"
        assert obj.state == "counter=1"

    def test_state_accumulates(self):
        brain = MockBrain()
        brain.script("test-obj", LLMResponse(
            updated_state="counter=1",
            reply="One.",
        ))
        brain.script("test-obj", LLMResponse(
            updated_state="counter=2",
            reply="Two.",
        ))
        obj = LLMObject(_make_definition(), brain)

        obj.process_message(_user_msg("first"))
        result = obj.process_message(_user_msg("second"))

        assert result.state_before == "counter=1"
        assert result.state_after == "counter=2"
        assert obj.state == "counter=2"

    def test_brain_receives_current_state(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(
            updated_state="updated",
            reply="ok",
        ))
        obj = LLMObject(_make_definition(), brain)
        obj.set_state("initial-state")

        obj.process_message(_user_msg("hello"))

        assert len(brain.call_log) == 1
        assert brain.call_log[0].current_state == "initial-state"

    def test_outgoing_messages(self):
        brain = MockBrain()
        brain.script("test-obj", LLMResponse(
            updated_state="",
            reply="done",
            outgoing_messages=[
                OutgoingMessage(recipient="peer-a", content="hello peer"),
            ],
        ))
        obj = LLMObject(_make_definition(), brain)

        result = obj.process_message(_user_msg("trigger"))

        assert len(result.outgoing_messages) == 1
        assert result.outgoing_messages[0].recipient == "peer-a"
        assert result.outgoing_messages[0].content == "hello peer"

    def test_history_grows(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state="", reply="ok"))
        obj = LLMObject(_make_definition(), brain)

        obj.process_message(_user_msg("msg1"))
        obj.process_message(_user_msg("msg2"))

        assert len(obj.history) == 2
        assert obj.history[0].content == "msg1"
        assert obj.history[1].content == "msg2"

    def test_metrics_returned(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state="", reply="ok"))
        obj = LLMObject(_make_definition(), brain)

        result = obj.process_message(_user_msg("test"))

        assert result.metrics is not None
        assert result.metrics.model == "mock"


class TestModifyDefinition:
    def test_preserves_state(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state="", reply="ok"))
        obj = LLMObject(_make_definition(), brain)
        obj.set_state("important-state")

        obj.modify_definition(role="Updated role.")

        assert obj.state == "important-state"
        assert obj.definition.role == "Updated role."

    def test_new_definition_visible_on_next_call(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state="", reply="ok"))
        obj = LLMObject(_make_definition(), brain)

        obj.modify_definition(role="New role text.")
        obj.process_message(_user_msg("test"))

        assert brain.call_log[-1].definition.role == "New role text."

    def test_invalid_field_raises(self):
        brain = MockBrain()
        obj = LLMObject(_make_definition(), brain)

        with pytest.raises(AttributeError, match="no_such_field"):
            obj.modify_definition(no_such_field="value")


class TestSnapshot:
    def test_snapshot_contents(self):
        brain = MockBrain()
        obj = LLMObject(_make_definition(), brain)
        obj.set_state("some state")

        snap = obj.snapshot()

        assert snap["object_id"] == "test-obj"
        assert snap["state"] == "some state"
        assert snap["definition"]["role"] == "A test object."
        assert snap["history_length"] == 0


class TestProperties:
    def test_peer_ids(self):
        defn = _make_definition(peers=[
            PeerDeclaration("a", "helper"),
            PeerDeclaration("b", "notifier"),
        ])
        obj = LLMObject(defn, MockBrain())
        assert obj.peer_ids == ["a", "b"]

    def test_subscriptions(self):
        defn = _make_definition(subscriptions=["topic-x", "topic-y"])
        obj = LLMObject(defn, MockBrain())
        assert obj.subscriptions == ["topic-x", "topic-y"]
