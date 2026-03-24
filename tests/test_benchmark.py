"""Tests for BenchmarkHarness (Phase 6)."""
import pytest
from pathlib import Path

from src.lnl import LLMResponse, MockBrain
from src.lnl.benchmark import (
    Assertion,
    BenchmarkHarness,
    Scenario,
    ScenarioStep,
)


@pytest.fixture
def brain():
    b = MockBrain()
    b.set_default(LLMResponse(updated_state={"status": "processed"}, reply="ok"))
    return b


@pytest.fixture
def harness(brain):
    return BenchmarkHarness(brain, judge=brain)


class TestLoadScenario:
    def test_load_from_directory(self, harness, tmp_path):
        objects_dir = tmp_path / "objects"
        objects_dir.mkdir()
        (objects_dir / "worker.md").write_text("# Worker\n\n## Role\n\nDoes work.\n")

        scenario_yaml = tmp_path / "scenario.yaml"
        scenario_yaml.write_text(
            "name: test-scenario\n"
            "steps:\n"
            "  - action: send\n"
            "    target: worker\n"
            "    content: hello\n"
            "assertions:\n"
            "  - type: state\n"
            "    target: worker\n"
            "    condition: processed\n"
        )

        scenario = harness.load_scenario(tmp_path)
        assert scenario.name == "test-scenario"
        assert len(scenario.steps) == 1
        assert len(scenario.assertions) == 1

    def test_missing_scenario_yaml_raises(self, harness, tmp_path):
        with pytest.raises(FileNotFoundError):
            harness.load_scenario(tmp_path)


class TestRunScenario:
    def test_basic_scenario(self, harness, tmp_path):
        objects_dir = tmp_path / "objects"
        objects_dir.mkdir()
        (objects_dir / "worker.md").write_text("# Worker\n\n## Role\n\nDoes work.\n")

        scenario = Scenario(
            name="basic",
            objects_dir=objects_dir,
            steps=[
                ScenarioStep(action="send", target="worker", content="hello"),
            ],
            assertions=[
                Assertion(type="state", target="worker", condition="processed"),
            ],
        )

        result = harness.run_scenario(scenario)
        assert result.name == "basic"
        assert len(result.assertion_results) == 1
        assert result.assertion_results[0].passed is True
        assert result.pass_rate == 1.0

    def test_failing_assertion(self, harness, tmp_path):
        objects_dir = tmp_path / "objects"
        objects_dir.mkdir()
        (objects_dir / "worker.md").write_text("# Worker\n\n## Role\n\nDoes work.\n")

        scenario = Scenario(
            name="fail",
            objects_dir=objects_dir,
            steps=[
                ScenarioStep(action="send", target="worker", content="hello"),
            ],
            assertions=[
                Assertion(type="state", target="worker", condition="nonexistent-value"),
            ],
        )

        result = harness.run_scenario(scenario)
        assert result.assertion_results[0].passed is False

    def test_reply_assertion(self, harness, tmp_path):
        objects_dir = tmp_path / "objects"
        objects_dir.mkdir()
        (objects_dir / "worker.md").write_text("# Worker\n\n## Role\n\nDoes work.\n")

        scenario = Scenario(
            name="reply",
            objects_dir=objects_dir,
            steps=[
                ScenarioStep(action="send", target="worker", content="hello"),
            ],
            assertions=[
                Assertion(type="reply", target="worker", condition="ok"),
            ],
        )

        result = harness.run_scenario(scenario)
        assert result.assertion_results[0].passed is True

    def test_modify_step(self, brain, tmp_path):
        brain.set_default(LLMResponse(updated_state={"status": "has state"}, reply="ok"))
        harness = BenchmarkHarness(brain, judge=brain)

        objects_dir = tmp_path / "objects"
        objects_dir.mkdir()
        (objects_dir / "worker.md").write_text("# Worker\n\n## Role\n\nDoes work.\n")

        scenario = Scenario(
            name="modify",
            objects_dir=objects_dir,
            steps=[
                ScenarioStep(action="send", target="worker", content="init"),
                ScenarioStep(action="modify", target="worker", modifications={"role": "New role."}),
                ScenarioStep(action="send", target="worker", content="after modify"),
            ],
            assertions=[
                Assertion(type="state", target="worker", condition="has state"),
            ],
        )

        result = harness.run_scenario(scenario)
        assert result.assertion_results[0].passed is True


class TestRunDirectory:
    def test_runs_all_scenarios(self, harness, tmp_path):
        for name in ["scenario-a", "scenario-b"]:
            d = tmp_path / name
            d.mkdir()
            obj_dir = d / "objects"
            obj_dir.mkdir()
            (obj_dir / "worker.md").write_text("# Worker\n\n## Role\n\nDoes work.\n")
            (d / "scenario.yaml").write_text(
                f"name: {name}\n"
                "steps:\n"
                "  - action: send\n"
                "    target: worker\n"
                "    content: hello\n"
                "assertions:\n"
                "  - type: reply\n"
                "    target: worker\n"
                "    condition: ok\n"
            )

        results = harness.run_directory(tmp_path)
        assert len(results) == 2
        assert all(r.pass_rate == 1.0 for r in results)


class TestMocksIntegration:
    def test_advance_fires_events(self, brain, tmp_path):
        brain.set_default(LLMResponse(updated_state={"status": "event received"}, reply="ok"))
        harness = BenchmarkHarness(brain, judge=brain)

        objects_dir = tmp_path / "objects"
        objects_dir.mkdir()
        (objects_dir / "sensor.md").write_text("# Sensor\n\n## Role\n\nReads sensors.\n")

        scenario = Scenario(
            name="events",
            objects_dir=objects_dir,
            steps=[
                ScenarioStep(action="advance"),  # step 1
                ScenarioStep(action="advance"),  # step 2 — fires event
            ],
            assertions=[
                Assertion(type="state", target="sensor", condition="event received"),
            ],
            mocks_config={
                "services": [{
                    "name": "env",
                    "state": {"temp": 25},
                    "events": [{
                        "step": 2,
                        "target": "sensor",
                        "content": "temperature=30",
                    }],
                }],
            },
        )

        result = harness.run_scenario(scenario)
        assert result.assertion_results[0].passed is True
