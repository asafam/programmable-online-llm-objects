"""Tests for the DAG planner mode (concurrent step dispatch).

DAG mode is an opt-in alternative to the default sequential planner. It:
  - Selects `planner_dag.yaml` instead of `planner_sequential.yaml` so the planner prompt
    encourages independent steps to carry empty `depends_on`.
  - Annotates the executor's `active_plan` rendering with a `ready:` header and
    `READY` tags so the executor fans out all ready steps in one finish.
  - Injects a DAG-mode addendum after the active_plan block.

Sequential mode is unchanged and must render identically to pre-DAG output.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.lnl import (
    MockBrain,
    ObjectDefinition,
    PeerDeclaration,
)
from src.lnl.brain import (
    _active_plan_mode_note,
    _render_active_plan,
    build_system_prompt,
)
from src.lnl.runtime import Runtime, SystemConfig
from src.lnl.types import Plan, PlanStep


def _defn(object_id="obj", peers=None):
    return ObjectDefinition(
        object_id=object_id,
        role="A test object.",
        peers=peers or [],
    )


def _plan_fanout():
    """Plan with two independent dispatch steps (s1, s2) and a dependent s3."""
    return Plan(
        goal="fan out to two peers, then notify channel",
        status="active",
        steps=[
            PlanStep(id="s1", kind="tell", target="peer-a", description="d1",
                     depends_on=[], status="planned"),
            PlanStep(id="s2", kind="tell", target="peer-b", description="d2",
                     depends_on=[], status="planned"),
            PlanStep(id="s3", kind="tell", target="channel",
                     description="notify with s1 + s2 results",
                     depends_on=["s1", "s2"], status="planned"),
        ],
    )


# ── SystemConfig.load() ─────────────────────────────────────────────────────


class TestSystemConfigLoad:
    def test_default_is_sequential(self, tmp_path: Path):
        cfg_path = tmp_path / "system.yaml"
        cfg_path.write_text("heartbeat:\n  enabled: false\n")
        cfg = SystemConfig.load(cfg_path)
        assert cfg.planner_mode == "sequential"

    def test_dag_value_parsed(self, tmp_path: Path):
        cfg_path = tmp_path / "system.yaml"
        cfg_path.write_text("planner_mode: dag\n")
        cfg = SystemConfig.load(cfg_path)
        assert cfg.planner_mode == "dag"

    def test_uppercase_dag_normalized(self, tmp_path: Path):
        cfg_path = tmp_path / "system.yaml"
        cfg_path.write_text("planner_mode: DAG\n")
        cfg = SystemConfig.load(cfg_path)
        assert cfg.planner_mode == "dag"

    def test_unknown_value_falls_back_to_sequential(self, tmp_path: Path):
        cfg_path = tmp_path / "system.yaml"
        cfg_path.write_text("planner_mode: parallel-explicit\n")
        cfg = SystemConfig.load(cfg_path)
        assert cfg.planner_mode == "sequential"

    def test_missing_file_returns_default(self, tmp_path: Path):
        cfg = SystemConfig.load(tmp_path / "does-not-exist.yaml")
        assert cfg.planner_mode == "sequential"


# ── _render_active_plan ready-set annotation ────────────────────────────────


class TestRenderActivePlanDagMode:
    def test_sequential_rendering_omits_ready_header(self):
        rendered = _render_active_plan(_plan_fanout(), mode="sequential")
        assert "ready:" not in rendered
        assert "READY" not in rendered
        # Still renders steps with ids and deps.
        assert "s1:" in rendered
        assert "deps=['s1', 's2']" in rendered

    def test_dag_rendering_includes_ready_header(self):
        rendered = _render_active_plan(_plan_fanout(), mode="dag")
        # Both s1 and s2 are ready (empty deps); s3 is not (deps unmet).
        assert "ready: [s1, s2]" in rendered
        assert "s1: tell → peer-a  status=planned  READY" in rendered
        assert "s2: tell → peer-b  status=planned  READY" in rendered
        # s3 has unmet deps; must not be tagged READY.
        s3_line = next(ln for ln in rendered.splitlines() if ln.strip().startswith("s3:"))
        assert "READY" not in s3_line

    def test_dag_promotes_step_when_deps_complete(self):
        plan = _plan_fanout()
        # Mark s1 and s2 done — s3 should now be ready.
        plan.steps[0].status = "done"
        plan.steps[1].status = "done"
        rendered = _render_active_plan(plan, mode="dag")
        assert "ready: [s3]" in rendered
        s3_line = next(ln for ln in rendered.splitlines() if ln.strip().startswith("s3:"))
        assert "READY" in s3_line

    def test_dag_skipped_dep_counts_as_satisfied(self):
        plan = _plan_fanout()
        plan.steps[0].status = "skipped"
        plan.steps[1].status = "done"
        rendered = _render_active_plan(plan, mode="dag")
        assert "ready: [s3]" in rendered

    def test_dag_empty_ready_set_renders_none(self):
        plan = Plan(
            goal="all blocked",
            steps=[
                PlanStep(id="s1", kind="tell", target="p", description="d",
                         depends_on=["s2"], status="planned"),
                PlanStep(id="s2", kind="tell", target="p", description="d",
                         depends_on=["s1"], status="planned"),
            ],
        )
        rendered = _render_active_plan(plan, mode="dag")
        assert "ready: [(none)]" in rendered

    def test_none_plan_renders_none(self):
        assert _render_active_plan(None, mode="dag") == "(none)"
        assert _render_active_plan(None, mode="sequential") == "(none)"

    def test_sequential_byte_compat_with_pre_dag_format(self):
        """Regression guard: sequential mode rendering must equal what the
        renderer produced before the mode parameter existed."""
        plan = _plan_fanout()
        expected = textwrap.dedent("""\
            goal: fan out to two peers, then notify channel
            status: active
            steps:
              s1: tell → peer-a  status=planned
                  description: "d1"
              s2: tell → peer-b  status=planned
                  description: "d2"
              s3: tell → channel  status=planned  deps=['s1', 's2']
                  description: "notify with s1 + s2 results"
            """).rstrip()
        assert _render_active_plan(plan, mode="sequential") == expected


# ── _active_plan_mode_note + build_system_prompt ────────────────────────────


class TestExecutorPromptModeNote:
    def test_sequential_note_is_empty(self):
        assert _active_plan_mode_note("sequential") == ""

    def test_dag_note_mentions_ready_and_parallel(self):
        note = _active_plan_mode_note("dag")
        assert "DAG mode" in note
        assert "ready:" in note
        # The note must instruct the executor to fan out unconditional ready steps
        # in the same turn. Accept any of the historically-used phrasings.
        assert any(p in note for p in (
            "every UNCONDITIONAL ready step",
            "every READY step",
            "ALL ready",
        ))
        # And must remind the executor that step-id order is not dispatch order.
        assert "identifiers" in note or "not an order" in note

    def test_dag_note_addresses_conditional_steps(self):
        """Regression guard for the conditional-dispatch fix: the addendum must
        instruct the executor to skip conditional steps when the condition is
        false, not to dispatch every ready step blindly. Without this, plans
        with conditional dispatches (`if quantity <= threshold`) fire
        unconditionally and break workflows."""
        note = _active_plan_mode_note("dag")
        assert "skipped" in note or "skip" in note.lower()
        # Mentions at least one of the gating keywords
        assert any(kw in note for kw in ("if", "when", "only when", "unless"))

    def test_build_system_prompt_dag_includes_note(self):
        prompt = build_system_prompt(
            _defn("obj"),
            current_state={},
            active_plan=_plan_fanout(),
            planner_mode="dag",
        )
        assert "DAG mode" in prompt
        assert "ready: [s1, s2]" in prompt

    def test_build_system_prompt_sequential_omits_note(self):
        prompt = build_system_prompt(
            _defn("obj"),
            current_state={},
            active_plan=_plan_fanout(),
            planner_mode="sequential",
        )
        assert "DAG mode" not in prompt
        # The placeholder must be substituted away even though the note is empty.
        assert "{active_plan_mode_note}" not in prompt


# ── Runtime wiring: prompt file selection ───────────────────────────────────


class TestRuntimeDagWiring:
    def test_default_runtime_uses_sequential_planner_prompt(self):
        rt = Runtime(MockBrain())
        assert rt._planner_mode == "sequential"
        assert rt._planner_prompt_file == "planner_sequential.yaml"

    def test_dag_systemconfig_selects_dag_prompt(self):
        cfg = SystemConfig(planner_mode="dag")
        rt = Runtime(MockBrain(), system_config=cfg)
        assert rt._planner_mode == "dag"
        assert rt._planner_prompt_file == "planner_dag.yaml"

    def test_set_planner_mode_api_updates_prompt_file(self):
        rt = Runtime(MockBrain())
        rt.set_planner_mode("dag")
        assert rt._planner_mode == "dag"
        assert rt._planner_prompt_file == "planner_dag.yaml"
        rt.set_planner_mode("sequential")
        assert rt._planner_mode == "sequential"
        assert rt._planner_prompt_file == "planner_sequential.yaml"

    def test_set_planner_mode_unknown_value_falls_back(self):
        rt = Runtime(MockBrain())
        rt.set_planner_mode("speculative")
        assert rt._planner_mode == "sequential"

    def test_object_inherits_mode_from_runtime(self):
        cfg = SystemConfig(planner_mode="dag")
        rt = Runtime(MockBrain(), system_config=cfg)
        obj = rt.create_object(_defn("o", peers=[PeerDeclaration("p", "r")]))
        assert obj._planner_mode == "dag"
        assert obj._planner_prompt_file == "planner_dag.yaml"

    def test_planner_dag_yaml_file_exists_and_loads(self):
        """The DAG planner prompt file must exist and be readable as the planner
        config — otherwise DAG mode would fail at the first planner_call."""
        from src.lnl.brain import _load_prompt_config
        cfg = _load_prompt_config("planner_dag.yaml")
        assert "system_prompt" in cfg
        # Must reference the DAG framing language so we don't accidentally
        # ship an identical copy of the sequential planner.
        assert "DAG" in cfg["system_prompt"] or "parallel" in cfg["system_prompt"].lower()
