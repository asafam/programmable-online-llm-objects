"""Benchmark harness — load scenarios, run them, evaluate with LLM-as-judge."""
from __future__ import annotations

import json
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .brain import LLMBrain
from .judge import LLMJudge, SubstringJudge
from .mocks import MockRegistry
from .runtime import Runtime
from .types import LLMResponse, Message, MessageType, ProcessingResult


@dataclass
class Assertion:
    """A single assertion to evaluate."""
    type: str  # state, bus_log, mock_recording, reply
    target: str  # object_id or service name
    condition: str  # natural language condition


@dataclass
class ScenarioStep:
    """A single step in a scenario."""
    action: str  # send, event, broadcast, modify, advance
    target: str = ""
    content: str = ""
    sender: str = "__user__"
    modifications: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scenario:
    """A complete benchmark scenario."""
    name: str
    objects_dir: Path
    steps: list[ScenarioStep] = field(default_factory=list)
    assertions: list[Assertion] = field(default_factory=list)
    mocks_config: Optional[dict] = None


@dataclass
class AssertionResult:
    """Result of evaluating a single assertion."""
    assertion: Assertion
    passed: bool
    actual: str
    reasoning: str = ""


@dataclass
class ScenarioResult:
    """Result of running a complete scenario."""
    name: str
    assertion_results: list[AssertionResult] = field(default_factory=list)
    processing_results: list[ProcessingResult] = field(default_factory=list)
    pass_rate: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    max_chain_depth: int = 0


class BenchmarkHarness:
    """Loads and runs benchmark scenarios."""

    def __init__(
        self,
        brain: Optional[LLMBrain] = None,
        judge: Optional[LLMJudge] = None,
    ) -> None:
        self._brain = brain
        self._judge: LLMJudge = judge or SubstringJudge()

    def load_scenario(self, path: str | Path) -> Scenario:
        """Load a scenario from a directory."""
        path = Path(path)
        scenario_yaml = path / "scenario.yaml"
        if not scenario_yaml.exists():
            raise FileNotFoundError(f"Missing scenario.yaml in {path}")

        with open(scenario_yaml) as f:
            config = yaml.safe_load(f)

        objects_dir = path / "objects"

        steps = []
        for step_cfg in config.get("steps", []):
            steps.append(ScenarioStep(
                action=step_cfg["action"],
                target=step_cfg.get("target", ""),
                content=step_cfg.get("content", ""),
                sender=step_cfg.get("sender", "__user__"),
                modifications=step_cfg.get("modifications", {}),
            ))

        assertions = []
        for a_cfg in config.get("assertions", []):
            assertions.append(Assertion(
                type=a_cfg["type"],
                target=a_cfg["target"],
                condition=a_cfg["condition"],
            ))

        mocks_config = None
        mocks_yaml = path / "mocks.yaml"
        if mocks_yaml.exists():
            with open(mocks_yaml) as f:
                mocks_config = yaml.safe_load(f)

        return Scenario(
            name=config.get("name", path.name),
            objects_dir=objects_dir,
            steps=steps,
            assertions=assertions,
            mocks_config=mocks_config,
        )

    def run_scenario(self, scenario: Scenario) -> ScenarioResult:
        """Run a scenario and evaluate assertions."""
        if self._brain is None:
            raise ValueError("BenchmarkHarness requires a brain to run scenarios")
        rt = Runtime(self._brain)
        registry = MockRegistry()

        # Setup mocks
        if scenario.mocks_config:
            for svc_cfg in scenario.mocks_config.get("services", []):
                svc = registry.add_service(svc_cfg["name"])
                for key, val in svc_cfg.get("state", {}).items():
                    svc.set_state(key, val)
                for evt in svc_cfg.get("events", []):
                    registry.schedule_event(
                        step=evt["step"],
                        target=evt["target"],
                        content=evt["content"],
                    )

        # Load objects
        if scenario.objects_dir.exists():
            rt.load_directory(scenario.objects_dir)

        all_results: list[ProcessingResult] = []
        max_depth = 0

        # Execute steps
        for step in scenario.steps:
            if step.action == "send":
                results = rt.send(step.target, step.content, sender=step.sender)
                all_results.extend(results)
            elif step.action == "event":
                results = rt.inject_event(step.target, step.content, source=step.sender)
                all_results.extend(results)
            elif step.action == "broadcast":
                results = rt.broadcast(step.content, sender=step.sender)
                all_results.extend(results)
            elif step.action == "modify":
                rt.modify(step.target, **step.modifications)
            elif step.action == "advance":
                due_events = registry.advance()
                for evt in due_events:
                    results = rt.inject_event(evt.target, evt.content)
                    all_results.extend(results)

        # Evaluate assertions
        assertion_results = []
        for assertion in scenario.assertions:
            actual = self._gather_actual(assertion, rt, registry, all_results)
            passed, reasoning = self.evaluate_assertion(assertion.condition, actual)
            assertion_results.append(AssertionResult(
                assertion=assertion,
                passed=passed,
                actual=actual,
                reasoning=reasoning,
            ))

        # Compute metrics
        total_in = sum(r.metrics.input_tokens for r in all_results if r.metrics)
        total_out = sum(r.metrics.output_tokens for r in all_results if r.metrics)
        passed_count = sum(1 for ar in assertion_results if ar.passed)
        pass_rate = passed_count / len(assertion_results) if assertion_results else 1.0

        return ScenarioResult(
            name=scenario.name,
            assertion_results=assertion_results,
            processing_results=all_results,
            pass_rate=pass_rate,
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            max_chain_depth=max_depth,
        )

    def run_directory(self, path: str | Path) -> list[ScenarioResult]:
        """Run all scenarios in a directory."""
        path = Path(path)
        results = []
        for scenario_dir in sorted(path.iterdir()):
            if scenario_dir.is_dir() and (scenario_dir / "scenario.yaml").exists():
                scenario = self.load_scenario(scenario_dir)
                results.append(self.run_scenario(scenario))
        return results

    def evaluate_assertion(
        self, condition: str, actual: str, context: str = ""
    ) -> tuple[bool, str, list[dict]]:
        """Evaluate an assertion condition against actual evidence using the judge.

        Returns (passed, reasoning, votes). votes is a list of per-judge dicts
        when a PanelJudge is used, or a single-entry list for single judges.
        """
        return self._judge.evaluate_with_votes(condition, actual, context)

    def _gather_actual(
        self,
        assertion: Assertion,
        rt: Runtime,
        registry: MockRegistry,
        results: list[ProcessingResult],
    ) -> str:
        """Gather the actual value for an assertion."""
        if assertion.type == "state":
            try:
                state = rt.state(assertion.target)
                return json.dumps(state)
            except KeyError:
                return f"Object '{assertion.target}' not found"

        elif assertion.type == "reply":
            replies = [
                r.reply for r in results
                if r.object_id == assertion.target
            ]
            return "\n".join(replies) if replies else "(no replies)"

        elif assertion.type == "bus_log":
            log_entries = []
            for entry in rt.message_log:
                log_entries.append(
                    f"{entry.message.sender} -> {entry.message.recipient}: "
                    f"{entry.message.content[:100]} [delivered={entry.delivered}]"
                )
            return "\n".join(log_entries) if log_entries else "(empty log)"

        elif assertion.type == "mock_recording":
            svc = registry.get_service(assertion.target)
            if svc is None:
                return f"Service '{assertion.target}' not found"
            recs = svc.recordings
            lines = [f"{r.method}({r.args})" for r in recs]
            return "\n".join(lines) if lines else "(no recordings)"

        return "(unknown assertion type)"
