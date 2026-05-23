"""Tests for the reactive step-retry + replan escalation feature.

Three behaviors under test:

1. _bump_step_retry_counts increments PlanStep.retry_count for every step
   the post-execution evaluator flagged FAIL.
2. _synthesize_reactive_replans appends a synthetic kind=replan step
   (with reactive_replan_for set) once a step's retry_count crosses
   step_max_retries, and respects the per-step step_replan_max cap.
3. When the synthesized replan's planner call fails, the originating
   step's plan flips to status="failed" and a step_retry_replan_exhausted
   synthetic event is logged. With the flag OFF, none of this fires.

Tests exercise the helpers directly with an in-memory plan so they don't
depend on a live evaluator brain or end-to-end execution.
"""
from __future__ import annotations

import datetime

import pytest

from src.lnl import LLMObject, MockBrain, ObjectDefinition
from src.lnl.types import Plan, PlanStep


def _make_object(**overrides) -> LLMObject:
    """Build an LLMObject with the step-retry-replan flag enabled by default."""
    kwargs = dict(
        enable_step_retry_replan=True,
        step_max_retries=2,
        step_replan_max=1,
        enable_evaluator=False,  # we drive plan state directly; no evaluator brain needed
        enable_planner=False,
    )
    kwargs.update(overrides)
    return LLMObject(
        ObjectDefinition(object_id="orch", role="r", behavior="b"),
        MockBrain(),
        **kwargs,
    )


def _seed_plan(obj: LLMObject, trace_id: str, steps: list[PlanStep]) -> Plan:
    plan = Plan(goal="g", steps=steps, trace_id=trace_id, status="active")
    with obj._plans_lock:
        obj._active_plans[trace_id] = plan
    return plan


# ── _bump_step_retry_counts ─────────────────────────────────────────────────


class TestBumpRetryCounts:
    def test_fail_criterion_increments_targeted_step(self):
        obj = _make_object()
        s1 = PlanStep(kind="tool", id="s1", description="lookup")
        s2 = PlanStep(kind="tell", id="s2", description="notify")
        _seed_plan(obj, "t1", [s1, s2])

        obj._bump_step_retry_counts("t1", [
            {"step_id": "s1", "status": "FAIL", "criterion": "missing arg"},
            {"step_id": "s2", "status": "PASS", "criterion": "ok"},
        ])

        assert s1.retry_count == 1
        assert s2.retry_count == 0

    def test_multiple_fails_on_same_step_count_once_per_call(self):
        """Multiple FAIL criteria for the same step in ONE evaluator cycle
        still represent ONE invalidation of that step — not three."""
        obj = _make_object()
        s1 = PlanStep(kind="tool", id="s1", description="lookup")
        _seed_plan(obj, "t1", [s1])

        obj._bump_step_retry_counts("t1", [
            {"step_id": "s1", "status": "FAIL", "criterion": "field A missing"},
            {"step_id": "s1", "status": "FAIL", "criterion": "field B wrong"},
            {"step_id": "s1", "status": "FAIL", "criterion": "field C missing"},
        ])

        assert s1.retry_count == 1, (
            "Three FAIL criteria for the same step in one evaluator cycle "
            "should bump retry_count by one, not three."
        )

    def test_no_criteria_no_change(self):
        obj = _make_object()
        s1 = PlanStep(kind="tool", id="s1", description="lookup")
        _seed_plan(obj, "t1", [s1])

        obj._bump_step_retry_counts("t1", [])
        obj._bump_step_retry_counts("t1", [{"step_id": "s1", "status": "PASS"}])

        assert s1.retry_count == 0

    def test_unknown_step_id_ignored(self):
        obj = _make_object()
        s1 = PlanStep(kind="tool", id="s1", description="lookup")
        _seed_plan(obj, "t1", [s1])

        obj._bump_step_retry_counts("t1", [
            {"step_id": "s99", "status": "FAIL", "criterion": "phantom"},
        ])

        assert s1.retry_count == 0


# ── _synthesize_reactive_replans ────────────────────────────────────────────


class TestSynthesizeReactiveReplans:
    def test_no_synthesis_below_threshold(self):
        obj = _make_object(step_max_retries=2)
        s1 = PlanStep(kind="tool", id="s1", description="lookup", retry_count=1)
        _seed_plan(obj, "t1", [s1])

        added = obj._synthesize_reactive_replans("t1")

        assert added == 0
        with obj._plans_lock:
            plan = obj._active_plans["t1"]
        assert len(plan.steps) == 1, "no synthetic step expected below threshold"

    def test_synthesis_at_threshold_appends_replan(self):
        obj = _make_object(step_max_retries=2)
        s1 = PlanStep(kind="tool", id="s1", description="lookup", retry_count=2)
        _seed_plan(obj, "t1", [s1])

        added = obj._synthesize_reactive_replans("t1")

        assert added == 1
        with obj._plans_lock:
            plan = obj._active_plans["t1"]
        assert len(plan.steps) == 2
        synth = plan.steps[1]
        assert synth.kind == "replan"
        assert synth.status == "planned"
        assert synth.reactive_replan_for == "s1"
        assert synth.depends_on == [], (
            "synthetic replan should fire same turn — no deps"
        )
        # The originating step's reactive_replan_count is bumped at synthesis.
        assert s1.reactive_replan_count == 1

    def test_per_step_cap_blocks_second_synthesis(self):
        """With step_replan_max=1, a step that already had one reactive
        replan synthesized must not get a second one even if retry_count
        keeps climbing."""
        obj = _make_object(step_max_retries=2, step_replan_max=1)
        s1 = PlanStep(
            kind="tool", id="s1", description="lookup",
            retry_count=5, reactive_replan_count=1,
        )
        _seed_plan(obj, "t1", [s1])

        added = obj._synthesize_reactive_replans("t1")

        assert added == 0

    def test_done_step_is_escalated(self):
        """A `done` step with retry_count past the threshold DOES trigger
        synthesis — `done` means the executor attempted it, but if the
        evaluator kept rejecting the output we still want an alternative."""
        obj = _make_object(step_max_retries=2)
        s1 = PlanStep(
            kind="tool", id="s1", description="lookup",
            retry_count=5, status="done",
        )
        _seed_plan(obj, "t1", [s1])

        added = obj._synthesize_reactive_replans("t1")

        assert added == 1

    def test_hard_terminal_steps_not_escalated(self):
        """Steps with `failed` or `skipped` status don't trigger synthesis
        even if retry_count is past the threshold — they were explicitly
        given up on or bypassed."""
        for status in ("failed", "skipped"):
            obj = _make_object(step_max_retries=2)
            s1 = PlanStep(
                kind="tool", id="s1", description="lookup",
                retry_count=5, status=status,
            )
            _seed_plan(obj, "t1", [s1])

            added = obj._synthesize_reactive_replans("t1")

            assert added == 0, f"expected no replan for status={status!r}"

    def test_failure_reason_in_replan_question(self):
        """last_failure_reason on the origin step appears in the synthetic
        replan step's replan_question so the planner has failure context."""
        obj = _make_object(step_max_retries=2)
        s1 = PlanStep(
            kind="tell", id="s1", description="send email",
            retry_count=3, status="done",
            last_failure_reason="no outgoing message observed",
        )
        _seed_plan(obj, "t1", [s1])

        obj._synthesize_reactive_replans("t1")

        plan = obj._active_plans["t1"]
        rr = next(s for s in plan.steps if s.reactive_replan_for == "s1")
        assert "no outgoing message observed" in (rr.replan_question or "")

    def test_reactive_replan_max_per_trace_cap(self):
        """reactive_replan_max_per_trace limits total synthetic replans
        across all steps in one trace — prevents plan explosion when many
        steps fail simultaneously."""
        obj = _make_object(step_max_retries=2, reactive_replan_max_per_trace=2)
        # Five steps all past the threshold.
        steps = [
            PlanStep(kind="tell", id=f"s{i}", description=f"step {i}", retry_count=3)
            for i in range(1, 6)
        ]
        _seed_plan(obj, "t1", steps)

        added = obj._synthesize_reactive_replans("t1")

        assert added == 2  # capped at reactive_replan_max_per_trace

    def test_reactive_replan_max_counts_already_synthesized(self):
        """Already-synthesized reactive replans count against the per-trace
        budget even after they complete."""
        obj = _make_object(step_max_retries=2, reactive_replan_max_per_trace=2)
        s1 = PlanStep(kind="tell", id="s1", description="step 1", retry_count=3)
        s2 = PlanStep(kind="tell", id="s2", description="step 2", retry_count=3)
        # Two reactive replans already completed — budget exhausted.
        done_rr1 = PlanStep(kind="replan", id="rr1", description="rr1", status="done", reactive_replan_for="s0")
        done_rr2 = PlanStep(kind="replan", id="rr2", description="rr2", status="done", reactive_replan_for="s0")
        _seed_plan(obj, "t1", [s1, s2, done_rr1, done_rr2])

        added = obj._synthesize_reactive_replans("t1")

        assert added == 0

    def test_pending_synthetic_replan_blocks_duplicate(self):
        """If a synthetic replan for this step is already pending in the
        plan, don't pile another on top this turn."""
        obj = _make_object(step_max_retries=2, step_replan_max=3)
        s1 = PlanStep(kind="tool", id="s1", description="lookup", retry_count=3)
        # Pre-existing synthetic replan still planned.
        existing = PlanStep(
            kind="replan", id="rr1", description="prior reactive replan",
            status="planned", reactive_replan_for="s1",
        )
        _seed_plan(obj, "t1", [s1, existing])

        added = obj._synthesize_reactive_replans("t1")

        assert added == 0


# ── Terminal failure when reactive replan exhausts ──────────────────────────


class _RaisingPlannerBrain(MockBrain):
    """MockBrain whose plan_call always raises, simulating planner failure."""

    def plan_call(self, *_args, **_kwargs):
        raise RuntimeError("simulated planner outage")


class TestReactiveReplanTerminalFailure:
    def test_planner_exception_flips_plan_to_failed(self):
        """Dispatcher's planner-failure branch must mark plan.status=failed
        and log step_retry_replan_exhausted when the failing replan step
        was synthetic (reactive_replan_for set)."""
        brain = _RaisingPlannerBrain()
        obj = LLMObject(
            ObjectDefinition(object_id="orch", role="r", behavior="b"),
            brain,
            enable_step_retry_replan=True,
            step_max_retries=2,
            step_replan_max=1,
            enable_evaluator=False,
            enable_planner=False,
        )

        # Hand-seed a plan with an already-pending synthetic replan ready to fire.
        s1 = PlanStep(
            kind="tool", id="s1", description="lookup",
            retry_count=3, reactive_replan_count=1, status="planned",
        )
        synth = PlanStep(
            kind="replan", id="rr1", description="reactive replan for s1",
            depends_on=[], status="planned", reactive_replan_for="s1",
            replan_question="Alternative continuation?",
        )
        plan = _seed_plan(obj, "t1", [s1, synth])

        fired = obj._dispatch_pending_replans("t1", message=None)

        assert fired == 1
        assert synth.status == "failed"
        assert "replan call failed" in (synth.result_summary or "")
        assert plan.status == "failed", (
            "synthetic replan's planner exception must flip the plan to failed "
            "so the trace concludes gracefully"
        )

    def test_planner_exception_on_non_synthetic_replan_does_not_fail_plan(self):
        """Regression guard: when a PLANNER-EMITTED replan step's planner
        call fails, only the step is marked failed — the plan stays active
        (existing behavior, must not regress)."""
        brain = _RaisingPlannerBrain()
        obj = LLMObject(
            ObjectDefinition(object_id="orch", role="r", behavior="b"),
            brain,
            enable_replan_checkpoints=True,  # the OTHER replan path
            replan_max_per_trace=3,
            enable_step_retry_replan=False,
            enable_evaluator=False,
            enable_planner=False,
        )

        s1 = PlanStep(kind="reason", id="s1", description="x", status="done")
        planner_replan = PlanStep(
            kind="replan", id="s2", description="planner-emitted replan",
            depends_on=["s1"], status="planned",
            replan_question="branch?",
            # NOTE: reactive_replan_for is None — this is a planner-emitted replan.
        )
        plan = _seed_plan(obj, "t1", [s1, planner_replan])

        obj._dispatch_pending_replans("t1", message=None)

        assert planner_replan.status == "failed"
        assert plan.status == "active", (
            "planner-emitted replan failure must NOT cascade to plan-level failure"
        )


# ── Flag-off no-op guarantees ───────────────────────────────────────────────


class TestFlagOffNoop:
    def test_bump_helper_called_via_guarded_path_only(self):
        """When the flag is OFF, retry_count never moves because the
        bump helper is never invoked from process_message. (Direct
        invocation still works — it's a pure helper — but production
        code paths are guarded.)"""
        obj = LLMObject(
            ObjectDefinition(object_id="orch", role="r", behavior="b"),
            MockBrain(),
            enable_step_retry_replan=False,
            enable_evaluator=False,
            enable_planner=False,
        )
        assert obj._enable_step_retry_replan is False

    def test_synthesize_with_zero_step_max_retries_noops(self):
        """Defensive: step_max_retries<=0 disables synthesis entirely."""
        obj = _make_object(step_max_retries=0)
        s1 = PlanStep(kind="tool", id="s1", description="lookup", retry_count=99)
        _seed_plan(obj, "t1", [s1])

        added = obj._synthesize_reactive_replans("t1")

        assert added == 0
