"""Tests for the post-execution evaluator (separate LLM call grading the
executor's most recent turn against the active plan).

Covers:
- build_evaluator_prompt formats correctly
- EVALUATOR_RESPONSE_SCHEMA has required fields
- LLMBrain.evaluate_call default raises NotImplementedError
- LLMObject.run_evaluator returns (None, None) when disabled or no plan
- LLMObject.run_evaluator invokes the brain when enabled + plan present
- Per-trace cycle counters increment correctly
"""
from __future__ import annotations
import pytest

from src.lnl.brain import (
    EVALUATOR_RESPONSE_SCHEMA,
    LLMBrain,
    build_evaluator_prompt,
)
from src.lnl.object import LLMObject
from src.lnl.types import (
    InferenceMetrics,
    LLMResponse,
    Message,
    MessageType,
    ObjectDefinition,
    OutgoingMessage,
    PeerDeclaration,
    Plan,
    PlanStep,
    ReactFinish,
    ReactStep,
)


def _make_definition(n_peers: int = 3) -> ObjectDefinition:
    return ObjectDefinition(
        object_id="orchestrator",
        role="Business logic orchestrator.",
        behavior="When event arrives, forward payload to declared peers.",
        peers=[
            PeerDeclaration(object_id=f"peer-{chr(ord('A') + i)}", relationship="")
            for i in range(n_peers)
        ],
    )


def _make_plan(trace_id: str = "t1") -> Plan:
    return Plan(
        goal="Forward event to A and B",
        steps=[
            PlanStep(kind="tell", target="peer-A", description="send to A", status="planned"),
            PlanStep(kind="tell", target="peer-B", description="send to B", status="planned"),
        ],
        status="active",
        trace_id=trace_id,
    )


class _NoopBrain(LLMBrain):
    def call(self, messages, schema, *, object_id=None):
        return LLMResponse(updated_state="", reply=""), InferenceMetrics(model="noop")

    def react_call(self, messages, *, object_id=None):
        return ReactStep(
            thought="", action="finish",
            state_update=None, plan_update=None, tool_call=None,
            finish=ReactFinish(reply="", outgoing_messages=[]),
        ), InferenceMetrics(model="noop")


class _EvaluatorBrain(_NoopBrain):
    def __init__(self, eval_result):
        super().__init__()
        self._result = eval_result
        self.calls = 0

    def evaluate_call(self, system_prompt, *, object_id=None):
        self.calls += 1
        return self._result, InferenceMetrics(model="eval")


# ── build_evaluator_prompt ───────────────────────────────────────────────────


def test_build_evaluator_prompt_includes_plan_outgoings_state():
    defn = _make_definition(2)
    plan = _make_plan()
    outgoings = [OutgoingMessage(recipient="peer-A", content="payload-A")]
    prompt = build_evaluator_prompt(
        defn, current_state="", plan=plan,
        outgoing_messages=outgoings, reply="dispatching step 0",
        message=_domain_msg(),
    )
    # Identity
    assert "orchestrator" in prompt
    # Plan goal + steps surface (plan mode)
    assert "Forward event to A and B" in prompt
    assert "peer-A" in prompt and "peer-B" in prompt
    assert "send to A" in prompt
    assert "status=planned" in prompt
    # Incoming message surfaces
    assert "go" in prompt  # _domain_msg content
    # Outgoings surface
    assert "payload-A" in prompt
    # Reply surfaces
    assert "dispatching step 0" in prompt
    # No leftover template braces
    assert "{plan_section}" not in prompt
    assert "{incoming_message}" not in prompt
    assert "{outgoing_messages}" not in prompt


def test_build_evaluator_prompt_handles_no_outgoings_and_empty_reply():
    prompt = build_evaluator_prompt(
        _make_definition(2), current_state={"x": 1}, plan=_make_plan(),
        outgoing_messages=[], reply="",
    )
    assert "(none — executor emitted no outgoings this turn)" in prompt
    assert "(empty)" in prompt
    # Dict state serializes
    assert '"x": 1' in prompt


def test_build_evaluator_prompt_no_plan_renders_gracefully():
    """With plan=None build_evaluator_prompt renders a safe fallback — the
    evaluator won't be called in this state (run_evaluator skips when plan
    is None), but the function must not crash."""
    prompt = build_evaluator_prompt(
        _make_definition(0), current_state={"stored": True}, plan=None,
        outgoing_messages=[], reply="done",
        message=_domain_msg(),
    )
    assert "(no plan)" in prompt
    assert "{plan_section}" not in prompt
    assert "{incoming_message}" not in prompt


# ── Schema sanity ─────────────────────────────────────────────────────────────


def test_evaluator_schema_has_required_fields():
    s = EVALUATOR_RESPONSE_SCHEMA
    assert s["required"] == ["verdict", "criteria", "feedback"]
    verdict_enum = s["properties"]["verdict"]["enum"]
    assert "PASS" in verdict_enum and "FAIL" in verdict_enum
    crit_props = s["properties"]["criteria"]["items"]["properties"]
    for field in ("step_index", "status", "diagnostic"):
        assert field in crit_props
    status_enum = crit_props["status"]["enum"]
    assert set(status_enum) == {"PASS", "FAIL", "SKIP"}


# ── LLMBrain.evaluate_call default ────────────────────────────────────────────


def test_default_evaluate_call_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        _NoopBrain().evaluate_call("prompt")


# ── LLMObject.run_evaluator ──────────────────────────────────────────────────


def test_run_evaluator_returns_none_when_disabled():
    obj = LLMObject(_make_definition(2), _NoopBrain(), enable_evaluator=False)
    obj._active_plans["t1"] = _make_plan()
    eval_dict, metrics = obj.run_evaluator([], "")
    assert eval_dict is None and metrics is None


def test_run_evaluator_skips_when_no_message():
    """The evaluator grades a turn against its input — with no incoming
    message there is nothing to grade against, so it skips."""
    eval_brain = _EvaluatorBrain({"verdict": "PASS", "criteria": [], "feedback": ""})
    obj = LLMObject(
        _make_definition(2), _NoopBrain(),
        enable_evaluator=True, evaluator_brain=eval_brain,
    )
    obj._active_plans["t1"] = _make_plan()
    eval_dict, _ = obj.run_evaluator([], "", message=None)
    assert eval_dict is None
    assert eval_brain.calls == 0


def test_run_evaluator_invokes_brain_when_enabled_with_plan():
    eval_result = {
        "verdict": "FAIL",
        "criteria": [
            {"step_index": 0, "status": "PASS", "diagnostic": "dispatched"},
            {"step_index": 1, "status": "FAIL", "diagnostic": "no outgoing to peer-B"},
        ],
        "feedback": "send to peer-B",
    }
    eval_brain = _EvaluatorBrain(eval_result)
    obj = LLMObject(
        _make_definition(2), _NoopBrain(),
        enable_evaluator=True, evaluator_brain=eval_brain,
    )
    obj._active_plans["t1"] = _make_plan()
    outgoings = [OutgoingMessage(recipient="peer-A", content="x")]
    eval_dict, metrics = obj.run_evaluator(outgoings, "reply text", _domain_msg())
    assert eval_brain.calls == 1
    assert eval_dict == eval_result
    assert metrics is not None


def test_run_evaluator_recovers_from_notimplemented():
    obj = LLMObject(
        _make_definition(2), _NoopBrain(),
        enable_evaluator=True, evaluator_brain=_NoopBrain(),
    )
    obj._active_plans["t1"] = _make_plan()
    eval_dict, _ = obj.run_evaluator([], "", _domain_msg())
    assert eval_dict is None


def test_run_evaluator_recovers_from_unexpected_exception():
    class _Bad(_NoopBrain):
        def evaluate_call(self, system_prompt, *, object_id=None):
            raise RuntimeError("eval boom")

    obj = LLMObject(
        _make_definition(2), _NoopBrain(),
        enable_evaluator=True, evaluator_brain=_Bad(),
    )
    obj._active_plans["t1"] = _make_plan()
    eval_dict, _ = obj.run_evaluator([], "", _domain_msg())
    assert eval_dict is None  # treated as PASS, no crash


# ── Per-trace cycle counter ───────────────────────────────────────────────────


def test_evaluator_cycle_counters_track_per_trace():
    obj = LLMObject(_make_definition(2), _NoopBrain())
    assert obj.evaluator_cycles_for_trace("t1") == 0
    obj.record_evaluator_cycle("t1")
    obj.record_evaluator_cycle("t1")
    obj.record_evaluator_cycle("t2")
    assert obj.evaluator_cycles_for_trace("t1") == 2
    assert obj.evaluator_cycles_for_trace("t2") == 1
    assert obj.evaluator_cycles_for_trace(None) == 0


def test_evaluator_cycle_record_noop_when_trace_none():
    obj = LLMObject(_make_definition(2), _NoopBrain())
    obj.record_evaluator_cycle(None)
    obj.record_evaluator_cycle(None)
    # No exception, no state pollution
    assert obj.evaluator_cycles_for_trace(None) == 0


# ── Evaluator — no peer gate, plan-required ───────────────────────────────────


def test_run_evaluator_runs_on_single_peer_objects():
    """Single-peer objects are NOT skipped — the evaluator runs on every
    LLM-object that has an active plan."""
    eval_result = {"verdict": "PASS", "criteria": [], "feedback": ""}
    eval_brain = _EvaluatorBrain(eval_result)
    obj = LLMObject(
        _make_definition(1),  # only 1 declared peer — no longer a skip condition
        _NoopBrain(),
        enable_evaluator=True,
        evaluator_brain=eval_brain,
    )
    obj._active_plans["t1"] = _make_plan()
    eval_dict, _ = obj.run_evaluator([], "", _domain_msg())
    assert eval_brain.calls == 1
    assert eval_dict == eval_result


def test_run_evaluator_skips_when_plan_is_none():
    """Without an active plan the evaluator always skips — regardless of
    peer count. Sinks get plans via effect steps; if the planner didn't
    fire, there is nothing to grade against."""
    for n_peers in (0, 1, 3):
        eval_brain = _EvaluatorBrain({"verdict": "PASS", "criteria": [], "feedback": ""})
        obj = LLMObject(
            _make_definition(n_peers), _NoopBrain(),
            enable_evaluator=True, evaluator_brain=eval_brain,
        )
        assert obj.active_plan is None
        eval_dict, _ = obj.run_evaluator([], "", _domain_msg())
        assert eval_dict is None, f"expected skip for n_peers={n_peers}"
        assert eval_brain.calls == 0, f"expected 0 eval calls for n_peers={n_peers}"


def test_run_evaluator_fires_when_all_plan_steps_terminal():
    """Even when every plan step is auto-closed (e.g. all tell steps marked
    done on dispatch), the evaluator MUST still fire to grade COMPLETENESS
    of the dispatched content. Auto-closure ≠ correctness. The prior skip
    was the gate that hid the dominant failure mode (orchestrators with
    pure tell plans whose dispatches were incomplete)."""
    eval_brain = _EvaluatorBrain({"verdict": "PASS", "criteria": [], "feedback": ""})
    obj = LLMObject(
        _make_definition(2), _NoopBrain(),
        enable_evaluator=True, evaluator_brain=eval_brain,
    )
    obj._active_plans["t1"] = Plan(
        goal="all done",
        steps=[
            PlanStep(kind="tell", target="peer-A", description="x", status="done"),
            PlanStep(kind="tell", target="peer-B", description="y", status="done"),
        ],
        status="active",
        trace_id="t1",
    )
    eval_dict, _ = obj.run_evaluator([], "", _domain_msg())
    assert eval_dict is not None
    assert eval_brain.calls == 1


def test_run_evaluator_fires_on_mixed_terminal_statuses():
    """Mixed terminal statuses (done + failed + skipped) still invoke the
    evaluator — it can confirm the outcomes are coherent (no contradictions
    between status and actual evidence) and grade completeness of the
    dispatched payloads."""
    eval_brain = _EvaluatorBrain({"verdict": "PASS", "criteria": [], "feedback": ""})
    obj = LLMObject(
        _make_definition(2), _NoopBrain(),
        enable_evaluator=True, evaluator_brain=eval_brain,
    )
    obj._active_plans["t1"] = Plan(
        goal="mixed",
        steps=[
            PlanStep(kind="tell", target="peer-A", description="x", status="done"),
            PlanStep(kind="ask",  target="peer-B", description="y", status="failed"),
            PlanStep(kind="tell", target="peer-C", description="z", status="skipped"),
        ],
        status="active",
        trace_id="t1",
    )
    eval_dict, _ = obj.run_evaluator([], "", _domain_msg())
    assert eval_dict is not None
    assert eval_brain.calls == 1


def test_run_evaluator_fires_when_at_least_one_planned_step_remains():
    """A single non-terminal step is enough to invoke the evaluator."""
    eval_result = {
        "verdict": "FAIL",
        "criteria": [
            {"step_index": 0, "status": "PASS", "diagnostic": "dispatched"},
            {"step_index": 1, "status": "FAIL", "diagnostic": "missing"},
        ],
        "feedback": "do step 1",
    }
    eval_brain = _EvaluatorBrain(eval_result)
    obj = LLMObject(
        _make_definition(2), _NoopBrain(),
        enable_evaluator=True, evaluator_brain=eval_brain,
    )
    obj._active_plans["t1"] = Plan(
        goal="one remaining",
        steps=[
            PlanStep(kind="tell", target="peer-A", description="x", status="done"),
            PlanStep(kind="tell", target="peer-B", description="y", status="planned"),
        ],
        status="active",
        trace_id="t1",
    )
    eval_dict, _ = obj.run_evaluator([], "", _domain_msg())
    assert eval_brain.calls == 1
    assert eval_dict == eval_result


# ── Internal self-correction loop (process_message) ──────────────────────────


class _ScriptedBrain(LLMBrain):
    """Brain with scripted ReAct + evaluator responses, consumed in order.
    Falls back to an empty finish / PASS verdict once the scripts run out."""

    def __init__(self, react_steps, eval_results):
        super().__init__()
        self._react_steps = list(react_steps)
        self._eval_results = list(eval_results)
        self.react_calls = 0
        self.eval_calls = 0

    def call(self, messages, schema, *, object_id=None):
        return LLMResponse(updated_state="", reply=""), InferenceMetrics(model="scripted")

    def react_call(self, messages, *, object_id=None):
        self.react_calls += 1
        if self._react_steps:
            step = self._react_steps.pop(0)
        else:
            step = ReactStep(
                thought="", action="finish",
                state_update=None, plan_update=None, tool_call=None,
                finish=ReactFinish(reply="", outgoing_messages=[]),
            )
        return step, InferenceMetrics(model="scripted")

    def evaluate_call(self, system_prompt, *, object_id=None):
        self.eval_calls += 1
        if self._eval_results:
            result = self._eval_results.pop(0)
        else:
            result = {"verdict": "PASS", "criteria": [], "feedback": ""}
        return result, InferenceMetrics(model="scripted-eval")


def _finish_step(reply="", outgoings=None):
    return ReactStep(
        thought="", action="finish",
        state_update=None, plan_update=None, tool_call=None,
        finish=ReactFinish(reply=reply, outgoing_messages=outgoings or []),
    )


def _domain_msg(trace_id="t1"):
    return Message(
        sender="x", recipient="orchestrator", type=MessageType.DOMAIN,
        content="go", id="m1", depth_remaining=5, trace_id=trace_id,
    )


def test_process_message_self_corrects_on_fail_then_pass():
    """A FAIL verdict triggers a second internal ReAct cycle; PASS ends it."""
    brain = _ScriptedBrain(
        react_steps=[_finish_step("cycle1"), _finish_step("cycle2")],
        eval_results=[
            {"verdict": "FAIL",
             "criteria": [{"step_index": 1, "status": "FAIL", "diagnostic": "missing"}],
             "feedback": "do step 1"},
            {"verdict": "PASS", "criteria": [], "feedback": ""},
        ],
    )
    obj = LLMObject(
        _make_definition(2), brain,
        enable_evaluator=True, evaluator_brain=brain,
    )
    obj._active_plans["t1"] = _make_plan()  # 2 planned steps; no outgoings → stays active
    result = obj.process_message(_domain_msg())
    assert brain.react_calls == 2
    assert brain.eval_calls == 2
    assert result.reply == "cycle2"  # last cycle's reply wins


def test_process_message_single_cycle_on_pass():
    """PASS on the first evaluation → exactly one ReAct cycle, no retry."""
    brain = _ScriptedBrain(
        react_steps=[_finish_step("done")],
        eval_results=[{"verdict": "PASS", "criteria": [], "feedback": ""}],
    )
    obj = LLMObject(
        _make_definition(2), brain,
        enable_evaluator=True, evaluator_brain=brain,
    )
    obj._active_plans["t1"] = _make_plan()
    result = obj.process_message(_domain_msg())
    assert brain.react_calls == 1
    assert brain.eval_calls == 1
    assert result.reply == "done"


def test_process_message_caps_self_correction_cycles():
    """Persistent FAIL verdicts are capped by evaluator_max_cycles_per_trace."""
    fail = {"verdict": "FAIL",
            "criteria": [{"step_index": 0, "status": "FAIL", "diagnostic": "x"}],
            "feedback": "retry"}
    brain = _ScriptedBrain(react_steps=[], eval_results=[fail] * 20)
    obj = LLMObject(
        _make_definition(2), brain,
        enable_evaluator=True, evaluator_brain=brain,
        evaluator_max_cycles_per_trace=3,
    )
    obj._active_plans["t1"] = _make_plan()
    result = obj.process_message(_domain_msg(trace_id="t1"))
    # cap=3 → initial cycle + 3 correction cycles = 4 ReAct calls, 3 evals
    assert brain.react_calls == 4
    assert brain.eval_calls == 3
    assert obj.evaluator_cycles_for_trace("t1") == 3
    assert result is not None  # no crash


def test_process_message_accumulates_outgoings_across_cycles():
    """Outgoings from every self-correction cycle are returned together —
    no partial dispatch, the runtime sees one corrected set."""
    brain = _ScriptedBrain(
        react_steps=[
            _finish_step("c1", [OutgoingMessage(recipient="peer-A", content="to-A")]),
            _finish_step("c2", [OutgoingMessage(recipient="peer-B", content="to-B")]),
        ],
        eval_results=[
            {"verdict": "FAIL",
             "criteria": [{"step_index": 1, "status": "FAIL", "diagnostic": "missing B"}],
             "feedback": "send B"},
            {"verdict": "PASS", "criteria": [], "feedback": ""},
        ],
    )
    obj = LLMObject(
        _make_definition(2), brain,
        enable_evaluator=True, evaluator_brain=brain,
    )
    obj._active_plans["t1"] = _make_plan()
    result = obj.process_message(_domain_msg())
    recipients = {o.recipient for o in result.outgoing_messages}
    assert recipients == {"peer-A", "peer-B"}
    assert brain.react_calls == 2


def test_process_message_no_evaluator_runs_single_cycle():
    """With the evaluator disabled, process_message runs exactly one cycle —
    behavior is unchanged from before the internalization refactor."""
    brain = _ScriptedBrain(
        react_steps=[_finish_step("once")],
        eval_results=[{"verdict": "FAIL", "criteria": [], "feedback": "ignored"}],
    )
    obj = LLMObject(
        _make_definition(2), brain,
        enable_evaluator=False, evaluator_brain=brain,
    )
    obj._active_plans["t1"] = _make_plan()
    result = obj.process_message(_domain_msg())
    assert brain.react_calls == 1
    assert brain.eval_calls == 0
    assert result.reply == "once"


# ── Effect step kind (rubric-plan approach) ───────────────────────────────────


def test_plan_dict_to_plan_accepts_effect_steps():
    """plan_dict_to_plan accepts the legacy 'effect' kind and normalizes
    it to 'reason' (back-compat alias)."""
    from src.lnl.brain import plan_dict_to_plan
    plan = plan_dict_to_plan({
        "goal": "Store the order",
        "steps": [
            {"step_number": 1, "kind": "effect", "target": "self",
             "description": "Record order_id and status=completed in state",
             "reasoning": "sink must persist"},
            {"step_number": 2, "kind": "final", "target": "final",
             "description": "done", "reasoning": "all steps done"},
        ],
    })
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.kind == "reason"  # normalized from "effect"
    assert step.target is None  # "self" is normalized to None
    assert "order_id" in step.description
    assert step.status == "planned"


def test_plan_dict_to_plan_mixed_effect_and_tell():
    """Effect and tell steps can coexist in one plan."""
    from src.lnl.brain import plan_dict_to_plan
    plan = plan_dict_to_plan({
        "goal": "Store and notify",
        "steps": [
            {"step_number": 1, "kind": "effect", "target": "self",
             "description": "Record in state", "reasoning": "sink stores"},
            {"step_number": 2, "kind": "tell", "target": "peer-A",
             "description": "Notify peer-A", "reasoning": "downstream"},
            {"step_number": 3, "kind": "final", "target": "final",
             "description": "done", "reasoning": "complete"},
        ],
    })
    assert len(plan.steps) == 2
    assert plan.steps[0].kind == "reason"  # normalized from "effect"
    assert plan.steps[0].target is None
    assert plan.steps[1].kind == "tell"
    assert plan.steps[1].target == "peer-A"


def test_mark_effect_steps_done_on_evaluator_pass():
    """When evaluator returns PASS, effect steps transition from planned → done."""
    eval_result = {
        "verdict": "PASS",
        "criteria": [{"step_index": 0, "status": "PASS", "diagnostic": "state has status=completed"}],
        "feedback": "",
    }
    brain = _ScriptedBrain(
        react_steps=[_finish_step("stored")],
        eval_results=[eval_result],
    )
    obj = LLMObject(
        ObjectDefinition(
            object_id="sink", role="Write service", behavior="store records", peers=[],
        ),
        brain,
        enable_evaluator=True, evaluator_brain=brain,
    )
    from src.lnl.types import Plan, PlanStep
    obj._active_plans["t1"] = Plan(
        goal="Store record",
        steps=[PlanStep(kind="reason", description="Record in state", target=None, status="planned")],
        status="active",
        trace_id="t1",
    )
    result = obj.process_message(_domain_msg())
    # After PASS, reason step should be done and plan closed
    assert obj.active_plan is None  # auto-closed after all steps terminal
    assert len(obj.completed_plans) == 1
    assert result.reply == "stored"


def test_build_evaluator_prompt_includes_reason_step():
    """Reason steps (formerly 'effect') are rendered in the plan section with
    kind=reason and no target."""
    from src.lnl.types import Plan, PlanStep
    plan = Plan(
        goal="Store order",
        steps=[PlanStep(kind="reason", description="Record in state", target=None, status="planned")],
        status="active",
    )
    prompt = build_evaluator_prompt(
        _make_definition(0), current_state={}, plan=plan,
        outgoing_messages=[], reply="stored", message=_domain_msg(),
    )
    assert "reason" in prompt
    assert "Record in state" in prompt
    assert "{plan_section}" not in prompt


# ── F2: cumulative dispatch evidence ─────────────────────────────────────────


def test_evaluator_prompt_renders_cumulative_dispatch_log():
    """The evaluator sees the harness-recorded all-turns dispatch log, not just
    this turn — completed prior-turn work must be visible as evidence."""
    defn = _make_definition(1)
    plan = _make_plan()
    prompt = build_evaluator_prompt(
        defn, current_state="", plan=plan,
        outgoing_messages=[], reply="continuing",
        message=_domain_msg(),
        tool_calls_this_turn=[],
        dispatch_log=["tool append_expense_row executed",
                      "ask -> expense-window: read Travel window"],
    )
    assert "Cumulative for this task" in prompt
    assert "append_expense_row" in prompt
    assert "ask -> expense-window" in prompt


def test_evaluator_prompt_states_statuses_are_runtime_set():
    """The grading rules must tell the evaluator that step statuses are
    deterministic runtime bookkeeping — a done step from an earlier turn is
    evidence, not an executor claim to distrust."""
    defn = _make_definition(1)
    plan = _make_plan()
    plan.steps[0].status = "done"
    prompt = build_evaluator_prompt(
        defn, current_state="", plan=plan,
        outgoing_messages=[], reply="r", message=_domain_msg(),
    )
    assert "set by the RUNTIME" in prompt
    assert "must NOT be failed for lacking" in prompt
    assert "status=done" in prompt  # the status itself is rendered


def test_object_dispatch_log_accumulates_across_turns():
    """Tool executions and outgoing sends append to the per-trace log the
    evaluator is fed."""
    obj = LLMObject(
        ObjectDefinition(object_id="t", role="r",
                         peers=[PeerDeclaration("p", "x")], skills=["my_tool"]),
        _NoopBrain(),
    )
    obj._log_dispatch("tr", "tool my_tool executed")
    msgs = [OutgoingMessage(recipient="p", content="hello", expects_reply=True)]
    obj._correlate_outgoing(msgs, "tr")
    log = obj._trace_dispatch_log["tr"]
    assert any("my_tool" in e for e in log)
    assert any(e.startswith("ask -> p") for e in log)
