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
from src.lnl.tools import MockToolExecutor, ToolRegistry
from src.lnl.types import ToolCall


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
            updated_state={"counter": 1},
            reply="Incremented.",
            outgoing_messages=[],
            reasoning="First message.",
        ))
        obj = LLMObject(_make_definition(), brain)

        result = obj.process_message(_user_msg("increment"))

        assert result.object_id == "test-obj"
        assert result.reply == "Incremented."
        assert result.state_before == {}
        assert result.state_after == {"counter": 1}
        assert obj.state == {"counter": 1}

    def test_state_accumulates(self):
        brain = MockBrain()
        brain.script("test-obj", LLMResponse(
            updated_state={"counter": 1},
            reply="One.",
        ))
        brain.script("test-obj", LLMResponse(
            updated_state={"counter": 2},
            reply="Two.",
        ))
        obj = LLMObject(_make_definition(), brain)

        obj.process_message(_user_msg("first"))
        result = obj.process_message(_user_msg("second"))

        assert result.state_before == {"counter": 1}
        assert result.state_after == {"counter": 2}
        assert obj.state == {"counter": 2}

    def test_brain_receives_current_state(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(
            updated_state={"status": "updated"},
            reply="ok",
        ))
        obj = LLMObject(_make_definition(), brain)
        obj.set_state({"status": "initial"})

        obj.process_message(_user_msg("hello"))

        assert len(brain.call_log) == 1
        assert brain.call_log[0].current_state == {"status": "initial"}

    def test_outgoing_messages(self):
        brain = MockBrain()
        brain.script("test-obj", LLMResponse(
            updated_state={},
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
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))
        obj = LLMObject(_make_definition(), brain)

        obj.process_message(_user_msg("msg1"))
        obj.process_message(_user_msg("msg2"))

        assert len(obj.history) == 2
        assert obj.history[0].content == "msg1"
        assert obj.history[1].content == "msg2"

    def test_metrics_returned(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))
        obj = LLMObject(_make_definition(), brain)

        result = obj.process_message(_user_msg("test"))

        assert result.metrics is not None
        assert result.metrics.model == "mock"


class TestModifyDefinition:
    def test_preserves_state(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))
        obj = LLMObject(_make_definition(), brain)
        obj.set_state({"status": "important"})

        obj.modify_definition(role="Updated role.")

        assert obj.state == {"status": "important"}
        assert obj.definition.role == "Updated role."

    def test_new_definition_visible_on_next_call(self):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))
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
        obj.set_state({"value": "some state"})

        snap = obj.snapshot()

        assert snap["object_id"] == "test-obj"
        assert snap["state"] == {"value": "some state"}
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


class TestToolLoop:
    def test_no_tool_registry_processes_normally(self):
        """Without a tool_registry, tool_calls in response are ignored."""
        brain = MockBrain()
        brain.script("test-obj", LLMResponse(
            updated_state={"status": "done"},
            reply="ok",
            tool_calls=[ToolCall(id="t1", tool="execute_code", arguments={"code": "x"})],
        ))
        obj = LLMObject(_make_definition(), brain)
        result = obj.process_message(_user_msg("go"))
        # tool_calls present but no registry → treated as final response
        assert result.state_after == {"status": "done"}

    def test_tool_call_then_final_response(self):
        """Tool call → execution → continuation → final response."""
        brain = MockBrain()
        # First response: tool call
        brain.script("test-obj", LLMResponse(
            updated_state={}, reply="",
            tool_calls=[ToolCall(id="t1", tool="my_tool", arguments={"code": "test"})],
        ))
        # Second response: final
        brain.script("test-obj", LLMResponse(
            updated_state={"status": "tool done"}, reply="finished",
        ))

        mock_exec = MockToolExecutor()
        mock_exec.script("tool output")
        reg = ToolRegistry()
        reg.register("my_tool", mock_exec)

        obj = LLMObject(_make_definition(), brain, tool_registry=reg)
        result = obj.process_message(_user_msg("go"))

        assert result.state_after == {"status": "tool done"}
        assert result.reply == "finished"
        assert len(mock_exec.call_log) == 1
        assert mock_exec.call_log[0].id == "t1"

    def test_max_tool_rounds_respected(self):
        """Tool loop stops after MAX_TOOL_ROUNDS even if LLM keeps requesting tools."""
        brain = MockBrain()
        # Script more tool-call responses than MAX_TOOL_ROUNDS
        for i in range(10):
            brain.script("test-obj", LLMResponse(
                updated_state={"round": i}, reply="",
                tool_calls=[ToolCall(id=f"t{i}", tool="my_tool", arguments={"code": ""})],
            ))

        mock_exec = MockToolExecutor()
        for _ in range(10):
            mock_exec.script("ok")
        reg = ToolRegistry()
        reg.register("my_tool", mock_exec)

        obj = LLMObject(_make_definition(), brain, tool_registry=reg)
        result = obj.process_message(_user_msg("go"))

        # Should have stopped at MAX_TOOL_ROUNDS
        assert len(mock_exec.call_log) == LLMObject.MAX_TOOL_ROUNDS

    def test_tool_context_factory(self):
        """Tool context factory is called and provides context to executor."""
        brain = MockBrain()
        brain.script("test-obj", LLMResponse(
            updated_state={}, reply="",
            tool_calls=[ToolCall(id="t1", tool="exec", arguments={"code": "push_event('hi')"})],
        ))
        brain.script("test-obj", LLMResponse(
            updated_state={"status": "setup done"}, reply="ok",
        ))

        events = []

        from src.lnl.tools import CodeExecutor
        reg = ToolRegistry()
        reg.register("exec", CodeExecutor())

        obj = LLMObject(
            _make_definition(), brain,
            tool_registry=reg,
            tool_context_factory=lambda o: {"push_event": lambda c, s="__code__": events.append((c, s))},
        )
        obj.process_message(_user_msg("setup"))

        assert events == [("hi", "__code__")]

    def test_metrics_accumulated(self):
        """Metrics from tool continuation calls are accumulated."""
        from src.lnl.types import InferenceMetrics

        brain = MockBrain()
        brain.script("test-obj", LLMResponse(
            updated_state={}, reply="",
            tool_calls=[ToolCall(id="t1", tool="t", arguments={"code": ""})],
        ), metrics=InferenceMetrics(input_tokens=10, output_tokens=5, model="mock"))
        brain.script("test-obj", LLMResponse(
            updated_state={"status": "done"}, reply="ok",
        ), metrics=InferenceMetrics(input_tokens=20, output_tokens=10, model="mock"))

        mock_exec = MockToolExecutor()
        mock_exec.script("ok")
        reg = ToolRegistry()
        reg.register("t", mock_exec)

        obj = LLMObject(_make_definition(), brain, tool_registry=reg)
        result = obj.process_message(_user_msg("go"))

        assert result.metrics.input_tokens == 30
        assert result.metrics.output_tokens == 15
