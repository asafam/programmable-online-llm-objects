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
from src.lnl.types import ReactFinish, ReactStep, StateDelta, ToolCall


def _make_definition(**overrides):
    defaults = dict(
        object_id="test-obj",
        role="A test object.",
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
        # The current state is serialised into the system prompt (first message)
        assert '"status": "initial"' in brain.call_log[0].messages[0]["content"]

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

        # The updated role is serialised into the system prompt (first message)
        assert "New role text." in brain.call_log[-1].messages[0]["content"]

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
        """Without a tool_registry, a tool_call step loops with an unavailability notice, then finishes."""
        brain = MockBrain()
        brain.script("test-obj", LLMResponse(
            updated_state={}, reply="",
            tool_calls=[ToolCall(id="t1", tool="execute_code", arguments={"code": "x"})],
        ))
        brain.script("test-obj", LLMResponse(
            updated_state={"status": "done"},
            reply="ok",
        ))
        obj = LLMObject(_make_definition(), brain)
        result = obj.process_message(_user_msg("go"))
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

        # Should have stopped at the default max_tool_rounds (5)
        assert len(mock_exec.call_log) == 5

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


class TestStateDelta:
    """State delta (state_update) tests — incremental, deliberate state changes."""

    def test_delta_set_on_finish(self):
        """A set delta on the finish step writes the key to state."""
        brain = MockBrain()
        brain.script_react(ReactStep(
            thought="Guest count updated.",
            action="finish",
            state_update=StateDelta(op="set", key="guest_count", value=47),
            finish=ReactFinish(reply="Done."),
        ))
        obj = LLMObject(_make_definition(), brain)
        result = obj.process_message(_user_msg("update count"))

        assert obj.state == {"guest_count": 47}
        assert result.state_after == {"guest_count": 47}
        assert result.reply == "Done."

    @pytest.mark.skip(
        reason="State deltas on tool_call steps are now intentionally discarded "
               "(only-on-response principle). The loop is back with parallel tool "
               "execution, but mid-loop deltas are still discarded by design — "
               "they're reasoning, not commitments."
    )
    def test_delta_mid_loop(self):
        pass

    def test_delta_delete(self):
        """A delete delta removes a key from existing state."""
        brain = MockBrain()
        brain.script_react(ReactStep(
            thought="Clearing pending.",
            action="finish",
            state_update=StateDelta(op="delete", key="_pending"),
            finish=ReactFinish(reply="cleared"),
        ))
        obj = LLMObject(_make_definition(), brain)
        obj.set_state({"_pending": {"waiting_for": "x"}, "other": 1})
        obj.process_message(_user_msg("clear"))

        assert obj.state == {"other": 1}
        assert "_pending" not in obj.state

    def test_delta_append(self):
        """Append deltas add to a list; repeated appends grow the list."""
        brain = MockBrain()
        brain.script_react(ReactStep(
            thought="First log entry.",
            action="finish",
            state_update=StateDelta(op="append", key="log", value="Alice checked in"),
            finish=ReactFinish(reply="logged"),
        ))
        obj = LLMObject(_make_definition(), brain)
        obj.process_message(_user_msg("log alice"))
        assert obj.state == {"log": ["Alice checked in"]}

        brain.script_react(ReactStep(
            thought="Second log entry.",
            action="finish",
            state_update=StateDelta(op="append", key="log", value="Bob checked in"),
            finish=ReactFinish(reply="logged"),
        ))
        obj.process_message(_user_msg("log bob"))
        assert obj.state == {"log": ["Alice checked in", "Bob checked in"]}

    def test_no_delta_state_unchanged(self):
        """When no delta is emitted and no updated_state is set, state is unchanged."""
        brain = MockBrain()
        brain.script_react(ReactStep(
            thought="Read-only lookup.",
            action="finish",
            finish=ReactFinish(reply="The value is 42."),
        ))
        obj = LLMObject(_make_definition(), brain)
        obj.set_state({"existing": "data"})
        result = obj.process_message(_user_msg("what is the value?"))

        assert obj.state == {"existing": "data"}
        assert result.state_after == {"existing": "data"}
        assert result.reply == "The value is 42."

    def test_updated_state_fallback(self):
        """MockBrain scripts using updated_state still work via the fallback path."""
        brain = MockBrain()
        brain.script("test-obj", LLMResponse(
            updated_state={"legacy": True},
            reply="compat",
        ))
        obj = LLMObject(_make_definition(), brain)
        obj.process_message(_user_msg("go"))

        assert obj.state == {"legacy": True}
