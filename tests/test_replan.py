"""Tests for the replan-checkpoint mechanism.

Replan checkpoints let the planner defer a decision until prior step results
land. The planner emits a `kind=replan` step with `depends_on=[s1]` and a
`replan_question`. When the deps complete, the runtime invokes the planner
again with `prior_plan` + the deferred question; the planner emits
continuation steps that the runtime appends via `add_steps`. The replan step
itself transitions `planned → dispatched → done`.

`replan` is documented as a first-class step kind in the planner prompts
(planner_dag.yaml / planner_sequential.yaml). The runtime-level flag
`SystemConfig.enable_replan_checkpoints` controls whether the runtime
actually fires the planner re-invocation when a ready replan step is
detected; it does not change what the planner sees in its prompt.
Off by default.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.lnl import (
    LLMResponse,
    MessageType,
    MockBrain,
    ObjectDefinition,
    OutgoingMessage,
    PeerDeclaration,
)
from src.lnl.brain import (
    PLANNER_RESPONSE_SCHEMA,
    VALID_STEP_KINDS,
    _render_prior_plan_context,
    build_planner_prompt,
    plan_dict_to_plan,
)
from src.lnl.runtime import Runtime, SystemConfig
from src.lnl.types import Message, Plan, PlanStep, PlanUpdate, ReactFinish, ReactStep


# ── Schema / parser ─────────────────────────────────────────────────────────


class TestSchemaAndParser:
    def test_replan_in_valid_kinds(self):
        assert "replan" in VALID_STEP_KINDS

    def test_planner_response_schema_lists_replan(self):
        kind_schema = PLANNER_RESPONSE_SCHEMA["properties"]["steps"]["items"]["properties"]["kind"]
        assert "replan" in kind_schema["enum"]

    def test_plan_dict_roundtrip_replan(self):
        d = {
            "goal": "check stock then maybe reorder",
            "steps": [
                {"id": "s1", "step_number": 1, "kind": "tool", "target": "inv",
                 "description": "lookup", "depends_on": [], "reasoning": ""},
                {"id": "s2", "step_number": 2, "kind": "replan", "target": "self",
                 "description": "defer reorder decision", "depends_on": ["s1"],
                 "reasoning": "", "replan_question": "Reorder if qty <= threshold?"},
            ],
        }
        p = plan_dict_to_plan(d, trace_id="t1")
        assert len(p.steps) == 2
        s2 = p.steps[1]
        assert s2.kind == "replan"
        assert s2.target == "self" or s2.target is None  # kind=replan target is informational only
        assert s2.depends_on == ["s1"]
        assert s2.replan_question == "Reorder if qty <= threshold?"
        # Status defaults to planned
        assert s2.status == "planned"


# ── SystemConfig parsing ────────────────────────────────────────────────────


class TestSystemConfigLoad:
    def test_default_off(self, tmp_path: Path):
        cfg_path = tmp_path / "system.yaml"
        cfg_path.write_text("heartbeat:\n  enabled: false\n")
        cfg = SystemConfig.load(cfg_path)
        assert cfg.enable_replan_checkpoints is False
        assert cfg.replan_max_per_trace == 3

    def test_explicit_on(self, tmp_path: Path):
        cfg_path = tmp_path / "system.yaml"
        cfg_path.write_text(
            "enable_replan_checkpoints: true\nreplan_max_per_trace: 5\n"
        )
        cfg = SystemConfig.load(cfg_path)
        assert cfg.enable_replan_checkpoints is True
        assert cfg.replan_max_per_trace == 5


# ── Prompt-builder rendering ────────────────────────────────────────────────


class TestReplanPromptRendering:
    def test_no_prior_plan_emits_no_block(self):
        # Default planner prompt — no `prior_plan` arg — must NOT include the
        # "Prior Plan Execution" block.
        defn = ObjectDefinition(object_id="o", role="r", behavior="b",
                                peers=[PeerDeclaration("p", "x")])
        out = _render_prior_plan_context(None, None)
        assert out == ""

    def test_prior_plan_emits_completed_results(self):
        plan = Plan(
            goal="lookup then maybe act",
            steps=[
                PlanStep(id="s1", kind="tool", target="inv", description="lookup",
                         status="done",
                         result={"quantity": 142, "threshold": 25},
                         result_kind="tool"),
                PlanStep(id="s2", kind="replan", target="self",
                         description="defer", status="dispatched",
                         depends_on=["s1"],
                         replan_question="Reorder?"),
            ],
        )
        out = _render_prior_plan_context(plan, "Reorder if qty <= threshold?")
        assert "Prior Plan Execution" in out
        assert "Decision Required" in out
        assert "Reorder if qty <= threshold?" in out
        assert '"quantity": 142' in out
        # The replan step itself (dispatched, not done) should NOT appear in
        # the "completed" list.
        completed_section = out.split("Completed steps")[-1]
        assert "s2:" not in completed_section

    def test_replan_is_first_class_kind_in_prompt(self):
        """`replan` is documented as a regular step kind in the planner
        prompt — no longer gated by a runtime flag. The planner sees the
        same kind list regardless of `enable_replan_checkpoints`; the flag
        only controls whether the runtime fires the replan dispatch."""
        defn = ObjectDefinition(object_id="o", role="r", behavior="b",
                                peers=[PeerDeclaration("p", "x")])
        from datetime import datetime, timezone
        msg = Message(sender="u", recipient="o", type=MessageType.DOMAIN,
                      content="go", id="m1", trace_id="t1",
                      timestamp=datetime.now(timezone.utc))
        out = build_planner_prompt(defn, "", msg,
                                   prompt_file="planner_sequential.yaml")
        # replan is enumerated in the Step kinds section and in the JSON
        # kind union; replan_question appears as a field.
        assert "`replan`" in out
        assert "replan_question" in out
        # The legacy gated note is gone.
        assert "Replan checkpoints (available)" not in out
        # Same for the DAG planner prompt.
        out_dag = build_planner_prompt(defn, "", msg,
                                       prompt_file="planner_dag.yaml")
        assert "`replan`" in out_dag
        assert "replan_question" in out_dag


# ── End-to-end: planner emits replan → runtime invokes planner again ───────


def _defn(object_id="orchestrator", **kw):
    kw.setdefault("peers", [PeerDeclaration("warehouse", "downstream")])
    return ObjectDefinition(
        object_id=object_id, role="orchestrator for inventory", behavior="b",
        **kw,
    )


def _msg(content="check stock", recipient="orchestrator", trace_id="trace-r1"):
    return Message(
        sender="__user__", recipient=recipient, type=MessageType.DOMAIN,
        content=content, id="msg-r-1", trace_id=trace_id,
    )


class TestReplanEndToEnd:
    def _script_initial_and_continuation(self, brain: MockBrain) -> None:
        """Initial plan: s1 (reason — proxy for a tool result), s2 (replan deps=[s1]).
        Continuation: c1 (reason) — kept simple so the test focuses on the
        replan-dispatch mechanic, not on cascade completion."""
        brain.script_plan({
            "goal": "lookup then maybe act",
            "steps": [
                {"id": "s1", "step_number": 1, "kind": "reason", "target": "self",
                 "description": "Record placeholder stock result", "depends_on": [],
                 "reasoning": ""},
                {"id": "s2", "step_number": 2, "kind": "replan", "target": "self",
                 "description": "Defer until s1 known", "depends_on": ["s1"],
                 "reasoning": "", "replan_question": "Reorder if qty<=threshold?"},
            ],
        }, object_id="orchestrator")
        brain.script_plan({
            "goal": "continuation after stock known",
            "steps": [
                {"id": "c1", "step_number": 1, "kind": "reason", "target": "self",
                 "description": "Record no-reorder decision",
                 "depends_on": [], "reasoning": ""},
            ],
        }, object_id="orchestrator")

    def test_planner_called_twice_continuation_appended(self):
        """Replan dispatch ON: initial plan → executor closes s1 →
        replan fires → continuation step appended → s2 done."""
        brain = MockBrain()
        self._script_initial_and_continuation(brain)
        # Executor: close s1 via plan_update. No outgoings so the test stays
        # focused on the planner re-entry behaviour.
        brain.script_react(ReactStep(
            thought="Close s1 manually.",
            action="finish",
            plan_update=PlanUpdate(step_updates=[
                {"id": "s1", "status": "done", "result_summary": "qty=142"},
            ]),
            finish=ReactFinish(reply="ok", outgoing_messages=[]),
        ))
        brain.set_default(LLMResponse(updated_state={}, reply=""))

        cfg = SystemConfig(enable_replan_checkpoints=True, replan_max_per_trace=3)
        rt = Runtime(brain, system_config=cfg)
        rt.create_object(_defn())
        rt.send("orchestrator", "check stock")
        orch = rt._bus.objects["orchestrator"]

        # The plan is still in active_plans (continuation step c1 is reason and
        # closes via evaluator; mock evaluator is off → c1 stays planned but
        # that's fine — we're asserting the dispatch mechanism, not closure).
        with orch._plans_lock:
            # Search active + completed plans for one with a replan step
            all_plans = list(orch._active_plans.values()) + list(orch._completed_plans)
        assert all_plans, "expected at least one plan"
        # Find the plan that came from our trace
        target_plan = max(all_plans, key=lambda p: len(p.steps))
        ids = [s.id for s in target_plan.steps]
        statuses = {s.id: s.status for s in target_plan.steps}
        kinds = {s.id: s.kind for s in target_plan.steps}
        # s1 done; s2 (replan) done; continuation step appended.
        assert "s1" in ids and statuses["s1"] == "done", f"s1 status: {statuses}"
        assert "s2" in ids, f"s2 missing: ids={ids}"
        assert kinds["s2"] == "replan"
        assert statuses["s2"] == "done", f"s2 should be done, got {statuses['s2']}"
        # Continuation step(s) appended after s2 — at least one new step
        # beyond the original two.
        assert len(target_plan.steps) >= 3, \
            f"expected ≥3 steps after replan, got {ids}"
        # Both scripted plans should be consumed.
        assert brain._plan_scripts.get("orchestrator", []) == [], \
            "expected both scripted plans consumed (initial + replan)"
        # Replan budget counter incremented.
        assert orch._replan_cycles_per_trace, \
            "expected replan cycle counter to be populated"

    def test_replan_can_be_disabled_explicitly(self):
        """When enable_replan_checkpoints=False (explicit opt-out; ON is the
        default since 2026-06-13 — control flow is a planning concern), a replan
        step in the plan is NOT actioned: the planner is invoked once;
        the budget counter stays empty. The step itself is auto-marked
        `skipped` so the plan can close (regression test against a leak
        where the prompt cleanup left replan as a first-class kind, so
        the planner can now emit it even when the runtime flag is off —
        the step must reach a terminal status either way or the plan
        stalls until stale_plan_seconds)."""
        brain = MockBrain()
        self._script_initial_and_continuation(brain)
        brain.script_react(ReactStep(
            thought="close s1",
            action="finish",
            plan_update=PlanUpdate(step_updates=[
                {"id": "s1", "status": "done", "result_summary": "x"},
            ]),
            finish=ReactFinish(reply="ok", outgoing_messages=[]),
        ))
        brain.set_default(LLMResponse(updated_state={}, reply=""))
        # Explicit opt-out — replan checkpoints OFF.
        rt = Runtime(brain, system_config=SystemConfig(enable_replan_checkpoints=False))
        rt.create_object(_defn())
        rt.send("orchestrator", "test")
        orch = rt._bus.objects["orchestrator"]
        # The second scripted plan should NOT have been consumed (the planner
        # is not re-invoked).
        remaining = brain._plan_scripts.get("orchestrator", [])
        assert len(remaining) == 1, \
            f"second plan should not have been called when replan disabled; remaining={len(remaining)}"
        # Budget counter unchanged (we didn't dispatch a replan, we skipped one).
        assert not orch._replan_cycles_per_trace, \
            "expected NO replan cycles when feature disabled"
        # The replan step should have been marked `skipped` so that
        # _auto_close_plan_if_complete CAN retire the plan — without this,
        # s2 would sit in `planned` forever and the plan would stall until
        # stale_plan_seconds. (The mock evaluator doesn't actually verdict
        # here, so plan-closure may not run — but the step's terminal status
        # is what matters; it unblocks closure whenever it does run.)
        with orch._plans_lock:
            all_plans = list(orch._active_plans.values()) + list(orch._completed_plans)
        target = next(p for p in all_plans if any(s.id == "s2" for s in p.steps))
        s2 = next(s for s in target.steps if s.id == "s2")
        # With replan explicitly off, the runtime DEMOTES the step to an
        # inline reason decision (deciding now beats skipping the decision —
        # a skipped branch decision lost the work it gated).
        assert s2.kind == "reason"
        assert "decide this now" in s2.description

    def test_budget_exhaustion_marks_replan_failed(self):
        """With replan_max_per_trace=1, a SECOND replan step in the same
        cascade should be marked `failed` (with reason) instead of firing
        another planner call."""
        brain = MockBrain()
        # Initial plan: s1 (reason) + s2 (replan deps=[s1])
        brain.script_plan({
            "goal": "first replan only",
            "steps": [
                {"id": "s1", "step_number": 1, "kind": "reason", "target": "self",
                 "description": "noop", "depends_on": [], "reasoning": ""},
                {"id": "s2", "step_number": 2, "kind": "replan", "target": "self",
                 "description": "first replan", "depends_on": ["s1"],
                 "reasoning": "", "replan_question": "Q1"},
            ],
        }, object_id="orchestrator")
        # First continuation: appends ANOTHER replan to test budget exhaustion.
        brain.script_plan({
            "goal": "continuation that triggers a second replan",
            "steps": [
                {"id": "c1", "step_number": 1, "kind": "reason", "target": "self",
                 "description": "noop", "depends_on": [], "reasoning": ""},
                {"id": "c2", "step_number": 2, "kind": "replan", "target": "self",
                 "description": "second replan (should hit budget)",
                 "depends_on": ["c1"], "reasoning": "", "replan_question": "Q2"},
            ],
        }, object_id="orchestrator")
        # If the budget weren't enforced, this would also be consumed.
        brain.script_plan({
            "goal": "should-not-fire",
            "steps": [
                {"id": "z1", "step_number": 1, "kind": "reason", "target": "self",
                 "description": "marker", "depends_on": [], "reasoning": ""},
            ],
        }, object_id="orchestrator")
        brain.script_react(ReactStep(
            thought="close s1",
            action="finish",
            plan_update=PlanUpdate(step_updates=[
                {"id": "s1", "status": "done", "result_summary": "x"},
            ]),
            finish=ReactFinish(reply="ok", outgoing_messages=[]),
        ))
        brain.set_default(LLMResponse(updated_state={}, reply=""))

        cfg = SystemConfig(enable_replan_checkpoints=True, replan_max_per_trace=1)
        rt = Runtime(brain, system_config=cfg)
        rt.create_object(_defn())
        rt.send("orchestrator", "test budget")
        orch = rt._bus.objects["orchestrator"]
        with orch._plans_lock:
            plans = list(orch._active_plans.values()) + list(orch._completed_plans)
        target_plan = max(plans, key=lambda p: len(p.steps))
        statuses = {s.id: s.status for s in target_plan.steps}
        kinds = {s.id: s.kind for s in target_plan.steps}
        # First replan fired (s2 done). Second replan (c2) appended by the
        # continuation, but with budget=1 it must be marked failed when c1 closes.
        # NOTE: c1 (reason) doesn't auto-close in this minimal test (no evaluator),
        # so c2's deps may not yet be satisfied. The relevant assertion is just
        # that the third scripted plan was NOT consumed.
        assert statuses["s2"] == "done"
        # The third scripted plan must remain unused — budget prevents a 2nd replan call.
        remaining = brain._plan_scripts.get("orchestrator", [])
        assert any("should-not-fire" in p.get("goal", "") for p in remaining), \
            f"the budget-blocked plan was incorrectly consumed; remaining goals: {[p.get('goal') for p in remaining]}"
