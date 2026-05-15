"""Tests for the pre-execution planner (separate LLM call producing a plan
that the executor then walks one step per turn).

Covers:
- build_planner_prompt formats correctly with object definition + message
- plan_dict_to_plan converts raw planner output into a Plan
  - drops the `final` marker step
  - sets all real steps to `planned`
  - filters invalid `kind` values
- LLMBrain.plan_call default raises NotImplementedError (so MockBrain skips)
- LLMObject pre-execution planning hook:
  - fires only when fan-out-decomposition enabled, ≥2 peers, DOMAIN msg, no plan
  - skips when planner brain unavailable
  - sets active_plan when planner returns valid steps
  - does NOT fire on subsequent (replay/continuation) messages in the same trace
"""
from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Optional

import pytest

from src.lnl.brain import (
    LLMBrain,
    PLANNER_RESPONSE_SCHEMA,
    build_planner_prompt,
    plan_dict_to_plan,
)
from src.lnl.object import LLMObject
from src.lnl.types import (
    InferenceMetrics,
    LLMResponse,
    Message,
    MessageType,
    ObjectDefinition,
    PeerDeclaration,
    Plan,
    ReactStep,
    ReactFinish,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_definition(n_peers: int = 3) -> ObjectDefinition:
    return ObjectDefinition(
        object_id="orchestrator",
        role="Business logic orchestrator for the event payload.",
        behavior="When event arrives, forward payload to all declared peers.",
        peers=[
            PeerDeclaration(object_id=f"peer-{chr(ord('A') + i)}", relationship=f"Receives payload {i}")
            for i in range(n_peers)
        ],
    )


def _make_message(trace_id: str = "trace-1", mtype: MessageType = MessageType.DOMAIN) -> Message:
    return Message(
        sender="upstream",
        recipient="orchestrator",
        type=mtype,
        content="event payload",
        id="m1",
        trace_id=trace_id,
    )


class _NoopBrain(LLMBrain):
    """Minimal brain stub used when we don't need real LLM behavior."""

    def __init__(self, react_finish_outgoings=None):
        self._react_finish_outgoings = react_finish_outgoings or []

    def call(self, messages, schema, *, object_id=None):
        return LLMResponse(updated_state="", reply=""), InferenceMetrics(model="noop")

    def react_call(self, messages, *, object_id=None):
        return ReactStep(
            thought="noop",
            action="finish",
            state_update=None,
            plan_update=None,
            tool_call=None,
            finish=ReactFinish(reply="", outgoing_messages=self._react_finish_outgoings),
        ), InferenceMetrics(model="noop")


class _PlannerBrain(_NoopBrain):
    """Brain whose plan_call returns a scripted plan dict."""

    def __init__(self, plan_dict, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._plan = plan_dict
        self.plan_calls = 0

    def plan_call(self, system_prompt, *, object_id=None):
        self.plan_calls += 1
        return self._plan, InferenceMetrics(model="planner")


# ── build_planner_prompt ─────────────────────────────────────────────────────


def test_build_planner_prompt_substitutes_object_fields():
    defn = _make_definition(2)
    msg = _make_message()
    prompt = build_planner_prompt(defn, current_state="", message=msg)
    # Object identity + role + behavior surface verbatim
    assert "orchestrator" in prompt
    assert "Business logic orchestrator" in prompt
    assert "When event arrives, forward payload" in prompt
    # Declared peers are listed
    assert "peer-A" in prompt
    assert "peer-B" in prompt
    # Message content and sender are included
    assert "event payload" in prompt
    assert "upstream" in prompt
    # No leftover template braces
    assert "{object_id}" not in prompt
    assert "{role}" not in prompt


def test_build_planner_prompt_with_dict_state_serializes_json():
    defn = _make_definition(2)
    msg = _make_message()
    state = {"some_key": "some_value", "count": 7}
    prompt = build_planner_prompt(defn, current_state=state, message=msg)
    assert "some_key" in prompt and "some_value" in prompt and "7" in prompt


# ── plan_dict_to_plan ────────────────────────────────────────────────────────


def test_plan_dict_to_plan_basic_three_steps():
    plan = plan_dict_to_plan({
        "goal": "Fan out to three peers",
        "steps": [
            {"step_number": 1, "kind": "tell", "target": "peer-A",
             "description": "send to A", "reasoning": "behavior says so"},
            {"step_number": 2, "kind": "tell", "target": "peer-B",
             "description": "send to B", "reasoning": "behavior says so"},
            {"step_number": 3, "kind": "tell", "target": "peer-C",
             "description": "send to C", "reasoning": "behavior says so"},
            {"step_number": 4, "kind": "final", "target": "final",
             "description": "done", "reasoning": "all dispatched"},
        ],
    })
    assert isinstance(plan, Plan)
    assert plan.goal == "Fan out to three peers"
    assert len(plan.steps) == 3  # `final` step dropped
    assert all(s.kind == "tell" and s.status == "planned" for s in plan.steps)
    assert [s.target for s in plan.steps] == ["peer-A", "peer-B", "peer-C"]


def test_plan_dict_to_plan_filters_invalid_kinds():
    plan = plan_dict_to_plan({
        "goal": "x",
        "steps": [
            {"kind": "tell", "target": "a", "description": "", "reasoning": ""},
            {"kind": "invalid_kind", "target": "b", "description": "", "reasoning": ""},
            {"kind": "ask", "target": "c", "description": "", "reasoning": ""},
            "not a dict",
        ],
    })
    assert len(plan.steps) == 2
    assert [s.kind for s in plan.steps] == ["tell", "ask"]


def test_plan_dict_to_plan_handles_empty_or_only_final():
    plan = plan_dict_to_plan({"goal": "no-op", "steps": [
        {"kind": "final", "target": "final", "description": "", "reasoning": ""}
    ]})
    assert plan.steps == []
    assert plan.goal == "no-op"


# ── LLMBrain.plan_call default ────────────────────────────────────────────────


def test_default_plan_call_raises_not_implemented():
    brain = _NoopBrain()
    with pytest.raises(NotImplementedError):
        brain.plan_call("any prompt")


# ── Planner schema sanity ─────────────────────────────────────────────────────


def test_planner_schema_has_required_fields():
    s = PLANNER_RESPONSE_SCHEMA
    assert s["type"] == "object"
    assert "goal" in s["properties"]
    assert "steps" in s["properties"]
    assert s["required"] == ["goal", "steps"]
    step_props = s["properties"]["steps"]["items"]["properties"]
    for required in ("step_number", "kind", "target", "description", "reasoning"):
        assert required in step_props


# ── LLMObject planning hook ───────────────────────────────────────────────────


def test_planning_hook_fires_for_fan_out_capable_domain_message():
    """Orchestrator with ≥2 peers receives a fresh DOMAIN msg → planner runs,
    active_plan is set with the returned steps."""
    plan = {
        "goal": "Forward to A and B",
        "steps": [
            {"step_number": 1, "kind": "tell", "target": "peer-A",
             "description": "send to A", "reasoning": "..."},
            {"step_number": 2, "kind": "tell", "target": "peer-B",
             "description": "send to B", "reasoning": "..."},
            {"step_number": 3, "kind": "final", "target": "final",
             "description": "", "reasoning": ""},
        ],
    }
    planner = _PlannerBrain(plan)
    obj = LLMObject(
        _make_definition(2),
        _NoopBrain(),  # executor brain
        enable_planner=True,
        planner_brain=planner,
    )
    assert obj.active_plan is None
    obj.process_message(_make_message())
    assert planner.plan_calls == 1
    assert obj.active_plan is not None
    assert len(obj.active_plan.steps) == 2
    assert [s.target for s in obj.active_plan.steps] == ["peer-A", "peer-B"]


def test_planning_hook_skips_when_planner_disabled():
    plan = {"goal": "x", "steps": [
        {"step_number": 1, "kind": "tell", "target": "peer-A",
         "description": "", "reasoning": ""},
    ]}
    planner = _PlannerBrain(plan)
    obj = LLMObject(
        _make_definition(2),
        _NoopBrain(),
        enable_planner=False,  # flag off
        planner_brain=planner,
    )
    obj.process_message(_make_message())
    assert planner.plan_calls == 0
    assert obj.active_plan is None


def test_planning_hook_skips_for_single_peer_object():
    plan = {"goal": "x", "steps": []}
    planner = _PlannerBrain(plan)
    obj = LLMObject(
        _make_definition(1),  # only 1 peer
        _NoopBrain(),
        enable_planner=True,
        planner_brain=planner,
    )
    obj.process_message(_make_message())
    assert planner.plan_calls == 0


def test_planning_hook_skips_reply_messages():
    plan = {"goal": "x", "steps": []}
    planner = _PlannerBrain(plan)
    obj = LLMObject(
        _make_definition(2),
        _NoopBrain(),
        enable_planner=True,
        planner_brain=planner,
    )
    obj.process_message(_make_message(mtype=MessageType.REPLY))
    assert planner.plan_calls == 0


def test_planning_hook_runs_once_per_trace():
    """Second DOMAIN message with the same trace_id should not re-plan."""
    plan = {
        "goal": "x",
        "steps": [
            {"step_number": 1, "kind": "tell", "target": "peer-A",
             "description": "", "reasoning": ""},
            {"step_number": 2, "kind": "tell", "target": "peer-B",
             "description": "", "reasoning": ""},
        ],
    }
    planner = _PlannerBrain(plan)
    obj = LLMObject(
        _make_definition(2),
        _NoopBrain(),
        enable_planner=True,
        planner_brain=planner,
    )
    obj.process_message(_make_message(trace_id="t"))
    assert planner.plan_calls == 1
    # Clear plan as the runtime would after completion, but mark the trace
    # as already-planned. Simulate a second DOMAIN msg same trace:
    obj._active_plan = None
    obj.process_message(_make_message(trace_id="t"))
    assert planner.plan_calls == 1  # still just one — trace was already planned


def test_planning_hook_replans_on_new_trace():
    plan = {
        "goal": "x",
        "steps": [
            {"step_number": 1, "kind": "tell", "target": "peer-A",
             "description": "", "reasoning": ""},
            {"step_number": 2, "kind": "final", "target": "final",
             "description": "", "reasoning": ""},
        ],
    }
    planner = _PlannerBrain(plan)
    obj = LLMObject(
        _make_definition(2),
        _NoopBrain(),
        enable_planner=True,
        planner_brain=planner,
    )
    obj.process_message(_make_message(trace_id="t1"))
    obj._active_plan = None
    obj.process_message(_make_message(trace_id="t2"))
    assert planner.plan_calls == 2


def test_planning_hook_swallows_notimplemented_from_brain():
    """If the brain doesn't support plan_call, planning is skipped silently
    and the regular ReAct loop runs."""
    obj = LLMObject(
        _make_definition(2),
        _NoopBrain(),
        enable_planner=True,
        planner_brain=_NoopBrain(),  # plan_call → NotImplementedError
    )
    result = obj.process_message(_make_message())
    assert obj.active_plan is None
    assert result is not None


def test_planning_hook_recovers_from_unexpected_exception():
    """If plan_call raises an unexpected exception, planning is logged and
    skipped; the ReAct loop still runs and produces a result."""

    class _FailingBrain(_NoopBrain):
        def plan_call(self, system_prompt, *, object_id=None):
            raise RuntimeError("planner unavailable")

    obj = LLMObject(
        _make_definition(2),
        _NoopBrain(),
        enable_planner=True,
        planner_brain=_FailingBrain(),
    )
    result = obj.process_message(_make_message())
    assert obj.active_plan is None
    assert result is not None  # ReAct still completed


def test_planning_hook_no_active_plan_when_planner_returns_empty():
    """Planner returning only a `final` step → no executable steps → no plan."""
    planner = _PlannerBrain({"goal": "nothing to do", "steps": [
        {"step_number": 1, "kind": "final", "target": "final",
         "description": "", "reasoning": ""}
    ]})
    obj = LLMObject(
        _make_definition(2),
        _NoopBrain(),
        enable_planner=True,
        planner_brain=planner,
    )
    obj.process_message(_make_message())
    assert planner.plan_calls == 1
    assert obj.active_plan is None  # no real steps emitted


def test_planner_brain_defaults_to_executor_brain_when_unset():
    """If planner_brain is None at construction, falls back to the executor
    brain (no separate planning model required)."""
    exec_brain = _NoopBrain()
    obj = LLMObject(
        _make_definition(2),
        exec_brain,
        enable_planner=True,
        planner_brain=None,
    )
    assert obj._planner_brain is exec_brain


def test_planner_output_surfaced_via_log_callback():
    """After the planner produces a plan with real steps, the runtime should
    invoke the log_synthetic_message callback with a synthetic PLAN message
    containing the goal and step list (visible in eval evidence + debug logs)."""
    plan = {
        "goal": "Forward event to A and B",
        "steps": [
            {"step_number": 1, "kind": "tell", "target": "peer-A",
             "description": "send to A", "reasoning": "..."},
            {"step_number": 2, "kind": "tell", "target": "peer-B",
             "description": "send to B", "reasoning": "..."},
            {"step_number": 3, "kind": "final", "target": "final",
             "description": "", "reasoning": ""},
        ],
    }
    planner = _PlannerBrain(plan)
    logged: list = []
    obj = LLMObject(
        _make_definition(2),
        _NoopBrain(),
        enable_planner=True,
        planner_brain=planner,
        log_synthetic_message=lambda m: logged.append(m),
    )
    obj.process_message(_make_message())
    assert len(logged) == 1
    msg = logged[0]
    assert msg.sender == "__planner__"
    assert msg.recipient == "orchestrator"
    assert msg.type == MessageType.PLAN
    assert 'goal="Forward event to A and B"' in msg.content
    assert "peer-A" in msg.content
    assert "peer-B" in msg.content
    assert "tell" in msg.content


def test_planner_log_callback_optional():
    """No-op when log_synthetic_message is None."""
    plan = {"goal": "x", "steps": [
        {"step_number": 1, "kind": "tell", "target": "peer-A",
         "description": "", "reasoning": ""},
    ]}
    planner = _PlannerBrain(plan)
    obj = LLMObject(
        _make_definition(2),
        _NoopBrain(),
        enable_planner=True,
        planner_brain=planner,
        log_synthetic_message=None,
    )
    obj.process_message(_make_message())
    assert obj.active_plan is not None


def test_planner_log_callback_not_invoked_when_plan_empty():
    """Planner returning only `final` → no plan installed → no log entry."""
    planner = _PlannerBrain({"goal": "no-op", "steps": [
        {"step_number": 1, "kind": "final", "target": "final",
         "description": "", "reasoning": ""},
    ]})
    logged: list = []
    obj = LLMObject(
        _make_definition(2),
        _NoopBrain(),
        enable_planner=True,
        planner_brain=planner,
        log_synthetic_message=lambda m: logged.append(m),
    )
    obj.process_message(_make_message())
    assert logged == []
    assert obj.active_plan is None


def test_bus_log_synthetic_appends_without_delivery():
    """`MessageBus.log_synthetic` records into the bus log but does NOT
    deliver or schedule the recipient."""
    from src.lnl.bus import MessageBus

    bus = MessageBus()
    delivered_calls: list = []
    bus.on_message = lambda m: delivered_calls.append(m)
    msg = Message(
        sender="__planner__",
        recipient="some-object",
        type=MessageType.PLAN,
        content="goal=test",
        depth_remaining=0,
        id="synth-1",
        trace_id="t",
    )
    bus.log_synthetic(msg)
    assert len(bus.log) == 1
    assert bus.log[0].message is msg
    assert bus.log[0].delivered is False
    assert delivered_calls == [msg]
