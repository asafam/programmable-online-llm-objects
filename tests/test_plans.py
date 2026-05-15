"""Tests for the single-active-plan mechanism.

Contract:
- One active plan per object at a time.
- Plans are runtime-owned: created automatically from outgoing Ask messages,
  auto-closed when all steps reach terminal status.
- LLM never authors plan or step ids; the Active Plan is read-only context.
- Runtime handles all correlation (outgoing stamping, reply tagging,
  auto-mark on reply, auto-done for Tell dispatches, auto-close on completion).
"""
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
from src.lnl.runtime import Runtime
from src.lnl.types import PlanUpdate, ReactFinish, ReactStep


def _defn(object_id="obj", **overrides):
    return ObjectDefinition(object_id=object_id, role="A test object.", **overrides)


_msg_seq = 0
def _user_msg(content, recipient="obj", trace_id: str | None = None):
    global _msg_seq
    _msg_seq += 1
    mid = f"test-msg-{_msg_seq}"
    tid = trace_id if trace_id is not None else mid
    return Message(
        sender="__user__",
        recipient=recipient,
        type=MessageType.DOMAIN,
        content=content,
        id=mid,
        trace_id=tid,
    )


class TestCreateAndClose:
    def test_auto_create_from_ask_outgoing(self):
        """Runtime auto-creates a plan when outgoing messages include an Ask."""
        brain = MockBrain()
        brain.script_react(ReactStep(
            thought="Need multi-step — send Ask to hr.",
            action="finish",
            finish=ReactFinish(
                reply="Working on it",
                outgoing_messages=[
                    OutgoingMessage(recipient="hr", content="look up manager", expects_reply=True),
                    OutgoingMessage(recipient="notifier", content="notify manager", expects_reply=False),
                ],
            ),
        ))
        brain.set_default(LLMResponse(updated_state={}, reply=""))

        rt = Runtime(brain)
        obj = rt.create_object(_defn("obj", peers=[
            PeerDeclaration("hr", "lookup"),
            PeerDeclaration("notifier", "notify"),
        ]))

        rt.send("obj", "go")

        plan = obj.active_plan
        assert plan is not None
        assert plan.status == "active"
        # Two steps: ask hr + tell notifier
        assert len(plan.steps) == 2
        assert plan.steps[0].kind == "ask"
        assert plan.steps[0].target == "hr"
        # Ask step should be dispatched (not done) after dispatch
        assert plan.steps[0].status == "dispatched"
        # Tell step should be done immediately
        assert plan.steps[1].kind == "tell"
        assert plan.steps[1].status == "done"

    def test_no_plan_created_without_ask_outgoing(self):
        """No plan is auto-created if outgoing messages are all Tells (or none)."""
        brain = MockBrain()
        brain.script_react(ReactStep(
            thought="Just a Tell — no plan needed.",
            action="finish",
            finish=ReactFinish(
                reply="done",
                outgoing_messages=[
                    OutgoingMessage(recipient="notifier", content="fyi", expects_reply=False),
                ],
            ),
        ))
        brain.set_default(LLMResponse(updated_state={}, reply=""))

        rt = Runtime(brain)
        obj = rt.create_object(_defn("obj", peers=[PeerDeclaration("notifier", "obs")]))
        rt.create_object(_defn("notifier"))

        rt.send("obj", "go")
        # No Ask → no active plan (Tell-only auto-creates plan but immediately auto-closes)
        assert obj.active_plan is None

    def test_plan_auto_closes_when_all_steps_done(self):
        """When all steps are terminal (done/failed/skipped), plan auto-closes."""
        brain = MockBrain()
        # Turn 1: Ask hr → auto-creates plan with dispatched ask step
        brain.script_react(ReactStep(
            thought="Ask hr.",
            action="finish",
            finish=ReactFinish(
                reply="",
                outgoing_messages=[OutgoingMessage(recipient="hr", content="?", expects_reply=True)],
            ),
        ))
        # Turn 2: hr replies → step auto-marked done → plan auto-closes
        brain.script_react(ReactStep(
            thought="Got reply.",
            action="finish",
            finish=ReactFinish(reply="ok"),
        ))
        brain.set_default(LLMResponse(updated_state={}, reply=""))

        rt = Runtime(brain)
        a = rt.create_object(_defn("obj-a", peers=[PeerDeclaration("hr", "lookup")]))
        rt.create_object(_defn("hr"))

        rt.send("obj-a", "go")

        # After hr replies, step is auto-done → plan auto-closes
        assert a.active_plan is None
        assert len(a.completed_plans) == 1
        assert a.completed_plans[0].status == "complete"
        assert a.completed_plans[0].steps[0].status == "done"


class TestIncrementalUpdate:
    def test_step_auto_marked_done_on_reply(self):
        """When a correlated reply arrives, the step is auto-marked done by the runtime."""
        brain = MockBrain()
        # Turn 1: obj asks peer → auto-creates plan with ask step (dispatched)
        brain.script_react(ReactStep(
            thought="Ask peer.",
            action="finish",
            finish=ReactFinish(
                reply="",
                outgoing_messages=[OutgoingMessage(recipient="peer", content="q?", expects_reply=True)],
            ),
        ))
        # Turn 2 (peer's turn): peer answers
        brain.script_react(ReactStep(
            thought="Answer.",
            action="finish",
            finish=ReactFinish(reply="42"),
        ))
        # Turn 3 (obj's turn, on reply): runtime auto-marks step done; plan auto-closes
        brain.script_react(ReactStep(
            thought="Got reply.",
            action="finish",
            finish=ReactFinish(reply=""),
        ))

        rt = Runtime(brain)
        obj = rt.create_object(_defn("obj-a", peers=[PeerDeclaration("peer", "q")]))
        rt.create_object(_defn("peer"))

        rt.send("obj-a", "go")

        # Plan auto-closed after step auto-marked done
        assert obj.active_plan is None
        assert len(obj.completed_plans) == 1
        assert obj.completed_plans[0].steps[0].status == "done"

    def test_second_ask_extends_active_plan(self):
        """A second Ask in a later turn extends the existing active plan."""
        brain = MockBrain()
        # Turn 1 (first user message): Ask peer-a → auto-creates plan with 1 step
        brain.script_react(ReactStep(
            thought="Ask peer-a.",
            action="finish",
            finish=ReactFinish(
                reply="",
                outgoing_messages=[OutgoingMessage(recipient="peer-a", content="?", expects_reply=True)],
            ),
        ))
        # Turn 2 (peer-a's response to user-msg-2): Also ask peer-b → extends plan
        brain.script_react(ReactStep(
            thought="Also ask peer-b.",
            action="finish",
            finish=ReactFinish(
                reply="",
                outgoing_messages=[OutgoingMessage(recipient="peer-b", content="?", expects_reply=True)],
            ),
        ))
        brain.set_default(LLMResponse(updated_state={}, reply=""))

        # Use process_message directly for deterministic control
        obj = LLMObject(_defn("obj-a", peers=[
            PeerDeclaration("peer-a", "q"),
            PeerDeclaration("peer-b", "q"),
        ]), brain)

        # First user message → creates plan with ask peer-a
        obj.process_message(_user_msg("go", recipient="obj-a", trace_id="t1"))

        plan_after_1 = obj.active_plan
        assert plan_after_1 is not None
        assert len(plan_after_1.steps) == 1
        assert plan_after_1.steps[0].target == "peer-a"

        # Second user message on the SAME trace → extends plan with ask peer-b
        obj.process_message(_user_msg("also ask", recipient="obj-a", trace_id="t1"))

        plan_after_2 = obj.active_plan
        assert plan_after_2 is not None
        assert len(plan_after_2.steps) == 2
        assert plan_after_2.steps[0].target == "peer-a"
        assert plan_after_2.steps[1].target == "peer-b"


class TestOutgoingAutoCorrelation:
    def test_tell_auto_marks_done_on_dispatch(self):
        """A Tell outgoing is auto-correlated and its step marked done on dispatch."""
        brain = MockBrain()
        brain.script_react(ReactStep(
            thought="Send Tell and Ask.",
            action="finish",
            finish=ReactFinish(
                reply="",
                outgoing_messages=[
                    OutgoingMessage(recipient="peer-b", content="fyi", expects_reply=False),
                    OutgoingMessage(recipient="peer-c", content="?", expects_reply=True),
                ],
            ),
        ))
        brain.set_default(LLMResponse(updated_state={}, reply=""))

        rt = Runtime(brain)
        a = rt.create_object(_defn("obj-a", peers=[
            PeerDeclaration("peer-b", "obs"),
            PeerDeclaration("peer-c", "q"),
        ]))
        rt.create_object(_defn("peer-b"))
        rt.create_object(_defn("peer-c"))

        rt.send("obj-a", "trigger")

        # Plan created from the Ask; Tell step auto-marked done
        plan = a.active_plan
        assert plan is not None
        # tell step → done; ask step → dispatched
        tell_step = next(s for s in plan.steps if s.kind == "tell")
        ask_step = next(s for s in plan.steps if s.kind == "ask")
        assert tell_step.status == "done"
        assert ask_step.status == "dispatched"

    def test_ask_flips_to_dispatched(self):
        """An Ask step flips from planned to dispatched after send."""
        brain = MockBrain()
        brain.script_react(ReactStep(
            thought="Ask peer.",
            action="finish",
            finish=ReactFinish(
                reply="",
                outgoing_messages=[OutgoingMessage(recipient="peer-b", content="?", expects_reply=True)],
            ),
        ))
        brain.set_default(LLMResponse(updated_state={}, reply=""))

        rt = Runtime(brain)
        a = rt.create_object(_defn("obj-a", peers=[PeerDeclaration("peer-b", "resp")]))
        rt.create_object(_defn("peer-b"))

        rt.send("obj-a", "trigger")

        plan = a.active_plan
        assert plan is not None
        assert plan.steps[0].status == "dispatched"

    def test_outgoing_without_ask_passes_uncorrelated(self):
        """Tell-only outgoing (no Ask) creates no durable active plan."""
        brain = MockBrain()
        brain.script_react(ReactStep(
            thought="Send without Ask.",
            action="finish",
            finish=ReactFinish(
                reply="",
                outgoing_messages=[OutgoingMessage(recipient="peer-b", content="hi")],
            ),
        ))
        brain.set_default(LLMResponse(updated_state={}, reply=""))

        rt = Runtime(brain)
        obj_a = rt.create_object(_defn("obj-a", peers=[PeerDeclaration("peer-b", "x")]))
        rt.create_object(_defn("peer-b"))

        rt.send("obj-a", "trigger")

        delivered = [log.message for log in rt.message_log if log.message.recipient == "peer-b"]
        assert any(m.sender == "obj-a" for m in delivered)
        # Tell-only: plan auto-created then auto-closed (Tell step immediately done)
        assert obj_a.active_plan is None


class TestReplyAutoMark:
    def test_reply_auto_marks_step_done(self):
        """When a correlated reply arrives, the plan step is auto-marked done."""
        brain = MockBrain()
        # Turn 1: A asks B → auto-creates plan with ask step
        brain.script_react(ReactStep(
            thought="Ask B.",
            action="finish",
            finish=ReactFinish(
                reply="",
                outgoing_messages=[OutgoingMessage(recipient="b", content="What is X?", expects_reply=True)],
            ),
        ))
        # Turn 2: B answers.
        brain.script_react(ReactStep(
            thought="Answer.",
            action="finish",
            finish=ReactFinish(reply="42"),
        ))
        # Turn 3: A receives reply — runtime auto-marks step done and auto-closes plan.
        brain.script_react(ReactStep(
            thought="Got it.",
            action="finish",
            finish=ReactFinish(reply=""),
        ))

        rt = Runtime(brain)
        a = rt.create_object(_defn("obj-a", peers=[PeerDeclaration("b", "resp")]))
        rt.create_object(_defn("b"))
        rt.send("obj-a", "go")

        # Plan auto-closed after step auto-marked done.
        assert a.active_plan is None
        assert len(a.completed_plans) == 1
        plan = a.completed_plans[0]
        assert plan.steps[0].status == "done"


class TestNestedPlans:
    """A→B→C→B→A. B has its own plan mid-flow. Mid-plan B→A steps use
    B's ids internally; the final non-step reply from B to A uses A's
    correlation (runtime propagates from B's pending-inbound Ask)."""

    def test_nested_chain(self):
        brain = MockBrain()
        # Turn 1: A asks B → auto-creates plan
        brain.script_react(ReactStep(
            thought="Ask B.",
            action="finish",
            finish=ReactFinish(
                reply="",
                outgoing_messages=[OutgoingMessage(recipient="b", content="data?", expects_reply=True)],
            ),
        ))
        # Turn 2: B receives A's ask — needs to ask C → auto-creates B's plan
        brain.script_react(ReactStep(
            thought="Need C.",
            action="finish",
            finish=ReactFinish(
                reply="",
                outgoing_messages=[OutgoingMessage(recipient="c", content="raw?", expects_reply=True)],
            ),
        ))
        # Turn 3: C answers B.
        brain.script_react(ReactStep(
            thought="Here.",
            action="finish",
            finish=ReactFinish(reply="XYZ"),
        ))
        # Turn 4: B receives C's reply; step 0 auto-done; plan auto-closes.
        # B replies to A via an outgoing Tell (runtime treats as reply).
        brain.script_react(ReactStep(
            thought="Done.",
            action="finish",
            finish=ReactFinish(
                reply="",
                outgoing_messages=[OutgoingMessage(recipient="obj-a", content="XYZ", expects_reply=False)],
            ),
        ))
        # Turn 5: A receives B's reply; step auto-marked done; plan auto-closes.
        brain.script_react(ReactStep(
            thought="Got it.",
            action="finish",
            finish=ReactFinish(reply=""),
        ))

        rt = Runtime(brain)
        a = rt.create_object(_defn("obj-a", peers=[PeerDeclaration("b", "r")]))
        b = rt.create_object(_defn("b", peers=[
            PeerDeclaration("c", "r"),
            PeerDeclaration("obj-a", "asker"),
        ]))
        rt.create_object(_defn("c"))

        rt.send("obj-a", "start")

        # A's plan closed cleanly.
        assert a.active_plan is None
        assert len(a.completed_plans) == 1
        assert a.completed_plans[0].steps[0].status == "done"

        # B's plan closed cleanly and is independent of A's.
        assert b.active_plan is None
        assert len(b.completed_plans) == 1
        b_plan = b.completed_plans[0]
        # Goal is auto-generated from message content
        assert b_plan.steps[0].status == "done"

        # The final B→A reply was routed as MessageType.REPLY (not DOMAIN).
        replies_to_a = [
            log.message for log in rt.message_log
            if log.message.type == MessageType.REPLY
            and log.message.sender == "b"
            and log.message.recipient == "obj-a"
        ]
        assert len(replies_to_a) == 1


class TestPromptRendering:
    def test_active_plan_rendered_without_ids(self):
        """Active plan is rendered in the system prompt without internal ids."""
        from src.lnl.brain import build_system_prompt

        brain = MockBrain()
        brain.script_react(ReactStep(
            thought="Ask hr.",
            action="finish",
            finish=ReactFinish(
                reply="",
                outgoing_messages=[
                    OutgoingMessage(recipient="hr", content="find email", expects_reply=True),
                ],
            ),
        ))
        brain.set_default(LLMResponse(updated_state={}, reply=""))

        rt = Runtime(brain)
        obj = rt.create_object(_defn("obj", peers=[PeerDeclaration("hr", "lookup")]))
        rt.create_object(_defn("hr"))
        rt.send("obj", "go")

        sys_prompt = build_system_prompt(
            obj.definition, obj.state, active_plan=obj.active_plan,
        )
        # Active plan rendered — goal derived from message content
        assert "Active Plan" in sys_prompt
        # Steps rendered by index, no runtime ids.
        assert "[0]" in sys_prompt
        # Ask hr step is visible
        assert "hr" in sys_prompt

    def test_no_active_plan_renders_none(self):
        from src.lnl.brain import build_system_prompt
        sys_prompt = build_system_prompt(_defn(), current_state={}, active_plan=None)
        assert "(none)" in sys_prompt
        assert "Active Plan" in sys_prompt

    def test_llm_plan_update_still_applied_via_apply_plan_update(self):
        """_apply_plan_update is still callable for backward compat (e.g. tests/scripts)."""
        brain = MockBrain()
        obj = LLMObject(_defn(), brain)
        # Directly call _apply_plan_update (not via process_message) with a trace_id
        from src.lnl.types import PlanUpdate
        obj._apply_plan_update(PlanUpdate(
            goal="test goal",
            steps=[{"kind": "ask", "description": "test step", "target": "peer"}],
        ), trace_id="t1")
        plan = obj.active_plan
        assert plan is not None
        assert plan.goal == "test goal"
        assert plan.steps[0].kind == "ask"


class TestConcurrentTraces:
    """Per-trace plan isolation: two cascades through the same object
    produce separate plans, each keyed by their own trace_id."""

    def test_two_traces_create_separate_plans(self):
        brain = MockBrain()
        # Each call asks a different peer
        for peer in ("peer-a", "peer-b"):
            brain.script_react(ReactStep(
                thought=f"Ask {peer}.",
                action="finish",
                finish=ReactFinish(
                    reply="",
                    outgoing_messages=[OutgoingMessage(recipient=peer, content="?", expects_reply=True)],
                ),
            ))
        brain.set_default(LLMResponse(updated_state={}, reply=""))

        obj = LLMObject(_defn("obj-a", peers=[
            PeerDeclaration("peer-a", "q"),
            PeerDeclaration("peer-b", "q"),
        ]), brain)

        obj.process_message(_user_msg("first cascade", recipient="obj-a", trace_id="trace-A"))
        obj.process_message(_user_msg("second cascade", recipient="obj-a", trace_id="trace-B"))

        plans = obj.active_plans
        assert set(plans.keys()) == {"trace-A", "trace-B"}
        assert plans["trace-A"].trace_id == "trace-A"
        assert plans["trace-B"].trace_id == "trace-B"
        # Each plan has its own single step pointing at its peer
        assert len(plans["trace-A"].steps) == 1
        assert plans["trace-A"].steps[0].target == "peer-a"
        assert len(plans["trace-B"].steps) == 1
        assert plans["trace-B"].steps[0].target == "peer-b"

    def test_plan_for_returns_only_matching_trace(self):
        obj = LLMObject(_defn(), MockBrain())
        obj._active_plans["t1"] = __import__("src.lnl.types", fromlist=["Plan"]).Plan(
            goal="A", steps=[], status="active", trace_id="t1",
        )
        obj._active_plans["t2"] = __import__("src.lnl.types", fromlist=["Plan"]).Plan(
            goal="B", steps=[], status="active", trace_id="t2",
        )
        assert obj.plan_for("t1").goal == "A"
        assert obj.plan_for("t2").goal == "B"
        assert obj.plan_for("missing") is None
        assert obj.plan_for(None) is None
        # active_plan backward-compat: returns None when there are multiple
        assert obj.active_plan is None

    def test_reply_routes_to_correct_trace_plan(self):
        """A reply tagged with plan_step_index marks the step done on the
        plan keyed by message.trace_id — not any other concurrent plan."""
        obj = LLMObject(_defn(), MockBrain())
        from src.lnl.types import Plan, PlanStep
        obj._active_plans["t1"] = Plan(
            goal="A", trace_id="t1", status="active",
            steps=[PlanStep(kind="ask", target="peer", description="x", status="dispatched")],
        )
        obj._active_plans["t2"] = Plan(
            goal="B", trace_id="t2", status="active",
            steps=[PlanStep(kind="ask", target="peer", description="y", status="dispatched")],
        )
        # Mark step 0 done on t1 via the reply hook
        obj._auto_mark_step_on_reply(0, trace_id="t1")
        # t1 closes (only step is now done); t2 remains active and untouched
        assert "t1" not in obj._active_plans
        assert obj._active_plans["t2"].steps[0].status == "dispatched"

    def test_reply_payload_captured_on_step_result(self):
        """The reply's content auto-populates step.result with result_kind='nl'
        — downstream steps can reference it from the rendered plan."""
        obj = LLMObject(_defn(), MockBrain())
        from src.lnl.types import Plan, PlanStep
        obj._active_plans["t1"] = Plan(
            goal="G", trace_id="t1", status="active",
            steps=[
                PlanStep(kind="ask", target="peer", description="get email", status="dispatched"),
                PlanStep(kind="tell", target="other", description="forward", status="planned"),
            ],
        )
        obj._auto_mark_step_on_reply(0, trace_id="t1", reply_content="john@snow.com")
        step = obj._active_plans["t1"].steps[0]
        assert step.status == "done"
        assert step.result == "john@snow.com"
        assert step.result_kind == "nl"
        assert step.completed_at is not None

    def test_tool_call_with_step_index_captures_structured_result(self):
        """A tool_call tagged with plan_step_index lands the tool's parsed
        output on step.result with result_kind='tool', and flips status to done."""
        from src.lnl.types import Plan, PlanStep, ToolResult
        obj = LLMObject(_defn(), MockBrain())
        obj._active_plans["t1"] = Plan(
            goal="G", trace_id="t1", status="active",
            steps=[
                PlanStep(kind="tool", target="python", description="compute discount", status="planned"),
                PlanStep(kind="tell", target="other", description="emit", status="planned"),
            ],
        )
        # Simulate a tool returning structured JSON
        obj._capture_tool_result_on_step(
            "t1", 0,
            ToolResult(id="tc1", output='{"discount": 0.15, "qty": 2}'),
        )
        step = obj._active_plans["t1"].steps[0]
        assert step.status == "done"
        assert step.result == {"discount": 0.15, "qty": 2}
        assert step.result_kind == "tool"
        assert step.completed_at is not None

    def test_tool_call_failure_captures_error_and_marks_failed(self):
        from src.lnl.types import Plan, PlanStep, ToolResult
        obj = LLMObject(_defn(), MockBrain())
        obj._active_plans["t1"] = Plan(
            goal="G", trace_id="t1", status="active",
            steps=[PlanStep(kind="tool", target="python", description="x", status="planned")],
        )
        obj._capture_tool_result_on_step(
            "t1", 0,
            ToolResult(id="tc1", output="", error="ZeroDivisionError"),
        )
        step = obj._active_plans["t1"].steps[0]
        assert step.status == "failed"
        assert step.result == {"output": "", "error": "ZeroDivisionError"}
        assert step.result_kind == "tool"

    def test_tool_call_non_json_output_stays_string(self):
        from src.lnl.types import Plan, PlanStep, ToolResult
        obj = LLMObject(_defn(), MockBrain())
        obj._active_plans["t1"] = Plan(
            goal="G", trace_id="t1", status="active",
            steps=[PlanStep(kind="tool", target="python", description="x", status="planned")],
        )
        obj._capture_tool_result_on_step(
            "t1", 0,
            ToolResult(id="tc1", output="hello world"),
        )
        step = obj._active_plans["t1"].steps[0]
        assert step.result == "hello world"  # not JSON-parseable, stored as string
        assert step.result_kind == "tool"

    def test_reply_payload_visible_in_system_prompt(self):
        """Captured step.result is rendered into the LLM's system prompt
        for downstream steps to consume natively."""
        from src.lnl.brain import build_system_prompt
        from src.lnl.types import Plan, PlanStep
        obj = LLMObject(_defn(), MockBrain())
        obj._active_plans["t1"] = Plan(
            goal="G", trace_id="t1", status="active",
            steps=[
                PlanStep(
                    kind="ask", target="peer", description="get email",
                    status="done", result="john@snow.com", result_kind="nl",
                ),
                PlanStep(kind="tell", target="other", description="forward", status="planned"),
            ],
        )
        prompt = build_system_prompt(
            obj.definition, obj.state, active_plan=obj.plan_for("t1"),
        )
        assert "john@snow.com" in prompt
        assert "(nl)" in prompt


class TestPlanRetirement:
    """Stage 3: stale plans get force-retired; cardinality cap evicts oldest."""

    def test_stale_plan_retired_as_abandoned(self):
        import datetime as _dt
        from src.lnl.types import Plan, PlanStep
        obj = LLMObject(_defn(), MockBrain(), stale_plan_seconds=0.5)
        # Pretend the plan hasn't progressed in 10s
        old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=10)
        obj._active_plans["t1"] = Plan(
            goal="G", trace_id="t1", status="active",
            steps=[PlanStep(kind="ask", target="x", description="d", status="dispatched")],
            created_at=old, last_progress_at=old,
        )
        obj._sweep_stale_plans()
        assert "t1" not in obj._active_plans
        assert len(obj.completed_plans) == 1
        assert obj.completed_plans[0].status == "abandoned"

    def test_cardinality_cap_evicts_oldest(self):
        import datetime as _dt
        from src.lnl.types import Plan, PlanStep
        obj = LLMObject(_defn(), MockBrain(), max_active_plans=2, stale_plan_seconds=99999)
        now = _dt.datetime.now(_dt.timezone.utc)
        # Three plans of different ages; cap is 2 → oldest evicted
        for i, age in enumerate([30, 20, 10]):
            tid = f"t{i}"
            t = now - _dt.timedelta(seconds=age)
            obj._active_plans[tid] = Plan(
                goal=tid, trace_id=tid, status="active",
                steps=[PlanStep(kind="ask", target="x", description="d", status="dispatched")],
                created_at=t, last_progress_at=t,
            )
        obj._sweep_stale_plans()
        # t0 (oldest at 30s) must be evicted; t1 and t2 stay
        assert "t0" not in obj._active_plans
        assert "t1" in obj._active_plans
        assert "t2" in obj._active_plans
        assert len(obj.completed_plans) == 1
        assert obj.completed_plans[0].status == "abandoned"
        assert obj.completed_plans[0].trace_id == "t0"

    def test_fresh_plans_not_retired(self):
        from src.lnl.types import Plan, PlanStep
        obj = LLMObject(_defn(), MockBrain(), stale_plan_seconds=99999)
        obj._active_plans["t1"] = Plan(
            goal="G", trace_id="t1", status="active",
            steps=[PlanStep(kind="ask", target="x", description="d", status="dispatched")],
        )
        obj._sweep_stale_plans()
        assert "t1" in obj._active_plans
