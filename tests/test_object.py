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


def _process_with_tools(obj, message):
    """Deliver message and drain the object synchronously (handles async tool REPLYs).

    Use instead of obj.process_message(msg) when the object has a tool_registry,
    because process_message returns 'pending' on tool dispatch and the final result
    arrives via the mailbox (tool REPLY messages).
    """
    results = []
    obj.deliver(message)
    obj.read(results.append)
    return results[-1] if results else None


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
        result = _process_with_tools(obj, _user_msg("go"))

        assert result.state_after == {"status": "tool done"}
        assert result.reply == "finished"
        assert len(mock_exec.call_log) == 1
        assert mock_exec.call_log[0].id == "t1"

    def test_max_tool_rounds_respected(self):
        """Tool loop stops after MAX_TOOL_ROUNDS even if LLM keeps requesting tools."""
        brain = MockBrain()
        # Script more tool-call responses than MAX_TOOL_ROUNDS (default=5).
        # After 5 async dispatches the cross-turn cap fires and forces a finish.
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
        _process_with_tools(obj, _user_msg("go"))

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
        _process_with_tools(obj, _user_msg("setup"))

        assert events == [("hi", "__code__")]

    def test_metrics_accumulated(self):
        """Metrics from tool continuation calls are accumulated across async turns."""
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
        # With async dispatch, the first process_message returns "pending" (10 tokens)
        # and the second (after tool REPLY) returns the final result (20 tokens).
        # Use _process_with_tools to collect all results, then sum metrics.
        results = []
        obj.deliver(_user_msg("go"))
        obj.read(results.append)

        total_input = sum(r.metrics.input_tokens for r in results if r.metrics)
        total_output = sum(r.metrics.output_tokens for r in results if r.metrics)
        assert total_input == 30
        assert total_output == 15


class TestSyncDispatch:
    """tool_dispatch='sync' — tools execute inline, no mailbox REPLY round-trip."""

    def test_sync_tool_call_then_final_response(self):
        """Sync mode: tool executes inline, single process_message call returns final result."""
        brain = MockBrain()
        brain.script("test-obj", LLMResponse(
            updated_state={}, reply="",
            tool_calls=[ToolCall(id="t1", tool="my_tool", arguments={"x": 1})],
        ))
        brain.script("test-obj", LLMResponse(
            updated_state={"status": "sync done"}, reply="finished sync",
        ))

        mock_exec = MockToolExecutor()
        mock_exec.script("sync output")
        reg = ToolRegistry()
        reg.register("my_tool", mock_exec)

        obj = LLMObject(_make_definition(), brain, tool_registry=reg, tool_dispatch="sync")
        result = obj.process_message(_user_msg("go"))

        assert result.reply == "finished sync"
        assert result.state_after == {"status": "sync done"}
        assert len(mock_exec.call_log) == 1
        # Sync dispatch never produces a "pending" result — exactly one ProcessingResult
        assert result.status != "pending"

    def test_sync_max_tool_rounds_respected(self):
        """Sync mode honours max_tool_rounds cap."""
        brain = MockBrain()
        for i in range(10):
            brain.script("test-obj", LLMResponse(
                updated_state={}, reply="",
                tool_calls=[ToolCall(id=f"t{i}", tool="my_tool", arguments={})],
            ))

        mock_exec = MockToolExecutor()
        for _ in range(10):
            mock_exec.script("ok")
        reg = ToolRegistry()
        reg.register("my_tool", mock_exec)

        obj = LLMObject(_make_definition(), brain, tool_registry=reg, tool_dispatch="sync")
        obj.process_message(_user_msg("go"))

        assert len(mock_exec.call_log) == 5  # default max_tool_rounds


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


class TestSinkShimMergeSemantics:
    """The sink completion shim must NEVER overwrite LLM-authored auto_completion
    fields. It only fills missing subkeys (fill-missing merge). See the
    08d6350 → merge-fix story: the wholesale `set` cost ~13pt on the Zapier
    multistep eval by replacing LLM-specific fields (deal_id, file_name,
    amount) with the shim's generic blob."""

    def _make_sink_obj(self, role="A write service that stores records.") -> LLMObject:
        # `_SINK_ROLE_KEYWORDS` keyword detection — "write service" / "store" both
        # land in the vocabulary. Pass enable_sink_completion_shim=True so the
        # shim path runs.
        brain = MockBrain()
        obj = LLMObject(
            _make_definition(role=role),
            brain,
            enable_sink_completion_shim=True,
        )
        return obj

    def _origin_msg(self) -> Message:
        return Message(
            sender="upstream-peer",
            recipient="test-obj",
            type=MessageType.DOMAIN,
            content="deal_id=D-123 amount=$45000 customer=Acme",
            id="msg-001",
        )

    def test_shim_provides_everything_when_state_empty(self):
        """LLM wrote nothing → shim is the sole author of auto_completion."""
        obj = self._make_sink_obj()
        finish = ReactFinish(reply="done", updated_state={})
        pending: list[StateDelta] = []
        obj._apply_sink_shim(finish, pending, trace_id=None, origin_msg=self._origin_msg())

        ac_deltas = [d for d in pending if d.key == "auto_completion"]
        assert len(ac_deltas) == 1
        ac = ac_deltas[0].value
        assert ac["status"] == "completed"
        assert ac["completed_by"] == "runtime_sink_shim"
        assert isinstance(ac["artifact"], dict)
        assert ac["artifact"].get("content") == "deal_id=D-123 amount=$45000 customer=Acme"

    def test_shim_preserves_llm_authored_domain_fields(self):
        """LLM wrote {status: 'posted', deal_id: 'D-123'} → both fields survive,
        shim fills artifact + completed_by. Status stays LLM's value."""
        obj = self._make_sink_obj()
        obj.set_state({"auto_completion": {"status": "posted", "deal_id": "D-123"}})
        finish = ReactFinish(reply="done", updated_state={})
        pending: list[StateDelta] = []
        obj._apply_sink_shim(finish, pending, trace_id=None, origin_msg=self._origin_msg())

        ac = [d for d in pending if d.key == "auto_completion"][0].value
        # LLM-authored fields survive
        assert ac["status"] == "posted"  # LLM's value, NOT overridden by shim
        assert ac["deal_id"] == "D-123"  # domain field preserved
        # Shim-provided fields added
        assert ac["completed_by"] == "runtime_sink_shim"
        assert isinstance(ac["artifact"], dict)

    def test_shim_is_noop_when_llm_wrote_full_completion(self):
        """LLM wrote a complete auto_completion → shim values appear only where
        the LLM left a gap; LLM's fields all survive."""
        obj = self._make_sink_obj()
        llm_ac = {
            "status": "posted",
            "completed_by": "deal-pipeline",
            "artifact": {"id": "LLM-1", "url": "https://llm/1", "content": "full content"},
            "deal_id": "D-123",
        }
        obj.set_state({"auto_completion": llm_ac})
        finish = ReactFinish(reply="done", updated_state={})
        pending: list[StateDelta] = []
        obj._apply_sink_shim(finish, pending, trace_id=None, origin_msg=self._origin_msg())

        ac = [d for d in pending if d.key == "auto_completion"][0].value
        # Every LLM field survives verbatim
        assert ac["status"] == "posted"
        assert ac["completed_by"] == "deal-pipeline"
        assert ac["deal_id"] == "D-123"
        # Artifact subfields: LLM's id / url / content all win
        assert ac["artifact"]["id"] == "LLM-1"
        assert ac["artifact"]["url"] == "https://llm/1"
        assert ac["artifact"]["content"] == "full content"

    def test_shim_merges_partial_artifact_subkeys(self):
        """LLM wrote artifact.id only → shim fills artifact.url and
        artifact.content; LLM's id survives."""
        obj = self._make_sink_obj()
        obj.set_state({"auto_completion": {"artifact": {"id": "LLM-X"}}})
        finish = ReactFinish(reply="done", updated_state={})
        pending: list[StateDelta] = []
        obj._apply_sink_shim(finish, pending, trace_id=None, origin_msg=self._origin_msg())

        ac = [d for d in pending if d.key == "auto_completion"][0].value
        # LLM artifact.id preserved
        assert ac["artifact"]["id"] == "LLM-X"
        # Missing artifact subkeys filled by shim
        assert "url" in ac["artifact"]
        assert ac["artifact"]["content"] == "deal_id=D-123 amount=$45000 customer=Acme"

    def test_shim_preserves_non_dict_auto_completion_under_raw(self):
        """LLM wrote a bare string as auto_completion → preserved under 'raw',
        shim's structured fields layered alongside. Nothing is lost."""
        obj = self._make_sink_obj()
        obj.set_state({"auto_completion": "all done"})
        finish = ReactFinish(reply="done", updated_state={})
        pending: list[StateDelta] = []
        obj._apply_sink_shim(finish, pending, trace_id=None, origin_msg=self._origin_msg())

        ac = [d for d in pending if d.key == "auto_completion"][0].value
        assert ac.get("raw") == "all done"  # original preserved
        assert ac["status"] == "completed"  # shim provided

    def test_shim_telemetry_log_emitted(self, caplog):
        """The shim logs a structured `[sink_shim]` line per fire so post-run
        analysis can grep which TCs went through and which fields were
        preserved vs added."""
        import logging
        obj = self._make_sink_obj()
        obj.set_state({"auto_completion": {"status": "posted", "deal_id": "D-123"}})
        finish = ReactFinish(reply="done", updated_state={})
        pending: list[StateDelta] = []
        with caplog.at_level(logging.INFO, logger="src.lnl.object"):
            obj._apply_sink_shim(finish, pending, trace_id="trace-1", origin_msg=self._origin_msg())
        assert any("[sink_shim]" in rec.message for rec in caplog.records), (
            "expected the [sink_shim] telemetry line; got: "
            + repr([r.message for r in caplog.records])
        )


class TestHistoryTaskGrouping:
    """Task-grouped history: entries are tagged with the Plan.id that owned
    their processing; the bucket is flushed when the plan terminates."""

    def _msg(self, content: str, trace_id: str, sender: str = "__user__", msg_type: MessageType = MessageType.DOMAIN) -> Message:
        return Message(
            sender=sender,
            recipient="test-obj",
            type=msg_type,
            content=content,
            id=f"m-{trace_id}-{content}",
            trace_id=trace_id,
        )

    def _inject_plan(self, obj, trace_id: str, steps=None, status: str = "active"):
        """Inject a Plan directly into _active_plans, bypassing the planner.
        Returns the Plan so the test can read .id."""
        from src.lnl.types import Plan, PlanStep
        plan_steps = steps if steps is not None else [
            PlanStep(id="s1", kind="reason", description="placeholder", status="planned"),
        ]
        plan = Plan(goal="test goal", steps=list(plan_steps), status=status, trace_id=trace_id)
        with obj._plans_lock:
            obj._active_plans[trace_id] = plan
        with obj._planned_traces_lock:
            obj._planned_traces.add(trace_id)
        return plan

    def _make_obj(self, **kwargs):
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))
        defaults = dict(enable_planner=False, enable_evaluator=False)
        defaults.update(kwargs)
        return LLMObject(_make_definition(), brain, **defaults), brain

    def test_history_property_returns_messages(self):
        """Back-compat: obj.history still yields list[Message]."""
        obj, _ = self._make_obj()
        obj.process_message(_user_msg("hello"))
        hist = obj.history
        assert len(hist) == 1
        assert hist[0].content == "hello"
        assert isinstance(hist[0], Message)

    def test_history_entries_property_returns_entries(self):
        obj, _ = self._make_obj()
        obj.process_message(_user_msg("hello"))
        entries = obj.history_entries
        assert len(entries) == 1
        assert entries[0].message.content == "hello"

    def test_orphan_bucket_when_no_plan(self):
        """With planner disabled, no plan exists → entries land in the
        orphan (task_id=None) bucket."""
        obj, _ = self._make_obj()
        obj.process_message(_user_msg("x"))
        entries = obj.history_entries
        assert len(entries) == 1
        assert entries[0].task_id is None

    def test_history_tagged_with_plan_id_when_plan_active(self):
        obj, _ = self._make_obj()
        plan = self._inject_plan(obj, trace_id="t-A")
        obj.process_message(self._msg("first", trace_id="t-A"))
        entries = obj.history_entries
        assert len(entries) == 1
        assert entries[0].task_id == plan.id

    def test_reply_continuation_reuses_task_id(self):
        obj, _ = self._make_obj()
        plan = self._inject_plan(obj, trace_id="t-A")
        obj.process_message(self._msg("first", trace_id="t-A"))
        obj.process_message(self._msg("follow-up", trace_id="t-A", sender="peer-x", msg_type=MessageType.REPLY))
        entries = obj.history_entries
        assert len(entries) == 2
        assert entries[0].task_id == plan.id
        assert entries[1].task_id == plan.id

    def test_two_concurrent_traces_distinct_task_ids(self):
        obj, _ = self._make_obj()
        plan_a = self._inject_plan(obj, trace_id="t-A")
        plan_b = self._inject_plan(obj, trace_id="t-B")
        assert plan_a.id != plan_b.id  # distinct auto-minted ids
        obj.process_message(self._msg("on-A", trace_id="t-A"))
        obj.process_message(self._msg("on-B", trace_id="t-B"))
        entries = obj.history_entries
        task_ids = [e.task_id for e in entries]
        assert plan_a.id in task_ids
        assert plan_b.id in task_ids

    def test_history_flushed_on_plan_complete(self):
        """When a plan auto-closes (all steps terminal), its history entries
        are flushed."""
        from src.lnl.types import PlanStep
        obj, _ = self._make_obj()
        # Plan whose only step is already terminal — auto_close will fire
        # at the end of the next process_message turn for this trace.
        plan = self._inject_plan(obj, trace_id="t-A", steps=[
            PlanStep(id="s1", kind="reason", description="done already", status="done"),
        ])
        obj.process_message(self._msg("trigger", trace_id="t-A"))
        # Plan should be closed and entry flushed.
        assert obj.plan_for("t-A") is None
        assert all(e.task_id != plan.id for e in obj.history_entries)

    def test_history_preserved_on_other_trace_when_one_plan_completes(self):
        """Flushing task A leaves task B's entries intact."""
        from src.lnl.types import PlanStep
        obj, _ = self._make_obj()
        plan_a = self._inject_plan(obj, trace_id="t-A", steps=[
            PlanStep(id="s1", kind="reason", description="done", status="done"),
        ])
        plan_b = self._inject_plan(obj, trace_id="t-B")
        obj.process_message(self._msg("on-B", trace_id="t-B"))   # tagged plan_b.id, stays
        obj.process_message(self._msg("on-A", trace_id="t-A"))   # tagged plan_a.id, flushed
        task_ids = [e.task_id for e in obj.history_entries]
        assert plan_b.id in task_ids
        assert plan_a.id not in task_ids

    def test_history_cap_32_total(self):
        """Total entries never exceed max_history; oldest evicted first."""
        obj, _ = self._make_obj()  # default max_history=32
        for i in range(50):
            obj.process_message(_user_msg(f"m{i}"))
        entries = obj.history_entries
        assert len(entries) == 32
        # Should be the last 32: m18..m49
        assert entries[0].message.content == "m18"
        assert entries[-1].message.content == "m49"

    def test_custom_max_history_respected(self):
        obj, _ = self._make_obj(max_history=5)
        for i in range(10):
            obj.process_message(_user_msg(f"m{i}"))
        assert len(obj.history_entries) == 5
        assert obj.history_entries[-1].message.content == "m9"

    def test_replan_in_place_keeps_task_id(self):
        """When the planner re-plans in place (needs_replan=True), Plan.id
        is preserved, so existing history entries remain correctly tagged."""
        from src.lnl.types import PlanStep
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))
        # Script a replacement plan dict for the planner.
        brain.script_plan(
            {
                "goal": "replanned",
                "steps": [
                    {"kind": "reason", "description": "step after replan"},
                ],
            },
            object_id="test-obj",
        )
        obj = LLMObject(
            _make_definition(), brain,
            enable_planner=True, enable_evaluator=False,
        )
        plan = self._inject_plan(obj, trace_id="t-A", steps=[
            PlanStep(id="s1", kind="reason", description="pre-replan", status="planned"),
        ])
        original_id = plan.id
        obj.process_message(self._msg("first", trace_id="t-A"))
        entry_before = obj.history_entries[-1]
        assert entry_before.task_id == original_id
        # Mark plan as needs_replan; next DOMAIN turn re-plans in place.
        with obj._plans_lock:
            obj._active_plans["t-A"].needs_replan = True
        obj.process_message(self._msg("second", trace_id="t-A"))
        # Plan.id unchanged after replan.
        assert obj.plan_for("t-A") is not None
        assert obj.plan_for("t-A").id == original_id
        # Both history entries share the same task_id.
        ids = [e.task_id for e in obj.history_entries]
        assert ids[-2] == original_id
        assert ids[-1] == original_id

    def test_build_chat_messages_groups_by_task(self):
        """_build_chat_messages renders history grouped by task_id with
        task headers; orphan bucket gets '-- Other --'."""
        from src.lnl.brain import _build_chat_messages
        from src.lnl.types import HistoryEntry
        history = [
            HistoryEntry(message=self._msg("a1", trace_id="t-A"), task_id="taskAAAA1234"),
            HistoryEntry(message=self._msg("b1", trace_id="t-B"), task_id="taskBBBB5678"),
            HistoryEntry(message=self._msg("a2", trace_id="t-A"), task_id="taskAAAA1234"),
            HistoryEntry(message=self._msg("orphan", trace_id="t-C"), task_id=None),
        ]
        msgs = _build_chat_messages("sys", history, self._msg("new", trace_id="t-D"))
        # Find the past-messages user message.
        past = next(m for m in msgs if m["role"] == "user" and "[Past messages" in m["content"])
        content = past["content"]
        # Headers present, in first-occurrence order.
        idx_a = content.find("-- Task taskAAAA")
        idx_b = content.find("-- Task taskBBBB")
        idx_other = content.find("-- Other --")
        assert idx_a >= 0 and idx_b >= 0 and idx_other >= 0
        assert idx_a < idx_b < idx_other
        # Both A entries appear under the A header (i.e. before B starts).
        a1_pos = content.find("a1")
        a2_pos = content.find("a2")
        assert idx_a < a1_pos < idx_b
        assert idx_a < a2_pos < idx_b

    def test_snapshot_exposes_history_task_ids(self):
        obj, _ = self._make_obj()
        plan = self._inject_plan(obj, trace_id="t-A")
        obj.process_message(self._msg("x", trace_id="t-A"))
        snap = obj.snapshot()
        assert snap["history_length"] == 1
        assert snap["history_task_ids"] == [plan.id]

    def test_default_max_history_is_32(self):
        """Regression: the new default cap is 32 (was 6)."""
        obj, _ = self._make_obj()
        assert obj._max_history == 32
