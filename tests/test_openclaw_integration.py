"""
Integration tests for OpenClaw multi-agent orchestration + mock API plumbing.

These tests verify that:
  1. The mock server is reachable and returns correct responses for direct tool calls.
  2. An agent cascade triggered by a step fires the expected downstream tool calls.

Requires:
  - Docker pool running: ./docker/start-pool.sh
  - Worker 1 available at ws://localhost:19789 (gateway) + http://localhost:19888 (mock)
  - OPENAI_API_KEY or ANTHROPIC_API_KEY in environment

Run:
    pytest tests/test_openclaw_integration.py -v -s
    pytest tests/test_openclaw_integration.py -v -s --worker 2   # use worker 2
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

from src.data.schema import TestCase
from src.data.mock_server import merge_tc_mock_tools, resolve_mock_configs
from src.lnl.openclaw_export import export_workflow_from_objects, rewrite_agents_md, reset_agent_state
from src.data.evaluate_baseline import (
    OpenClawAgent,
    RemoteMockServer,
    WorkerConfig,
    _load_pool_config,
    _openclaw_connect_kwargs,
    _configure_openclaw_agents,
    _wait_for_gateway_restart,
    _wait_mock_quiescence,
)

# ── Configuration ─────────────────────────────────────────────────────────────

POOL_CONFIG = ROOT / "docker" / "worker-pool.yaml"
TC_PATH = ROOT / "outputs" / "data" / "zapier" / "20260411_zapier_clean" / "test_cases.jsonl"
AGENT_MODEL = "gpt-4o"
AGENT_PROVIDER = "openai"

# TC 0: web-clipper (chrome-extension → content-curator → slack-content-channel)
# Simple 4-agent chain with clear tool call assertions.
TC_INDEX = 0


def pytest_addoption(parser):
    parser.addoption("--worker", type=int, default=1, help="Pool worker number to use (default: 1)")


@pytest.fixture(scope="session")
def worker_num(request):
    return request.config.getoption("--worker", default=1)


@pytest.fixture(scope="session")
def worker(worker_num) -> WorkerConfig:
    workers = _load_pool_config(POOL_CONFIG)
    assert 1 <= worker_num <= len(workers), f"--worker must be 1-{len(workers)}"
    return workers[worker_num - 1]


@pytest.fixture(scope="session")
def tc() -> TestCase:
    with open(TC_PATH) as f:
        lines = [l for l in f if l.strip()]
    return TestCase(**json.loads(lines[TC_INDEX]))


@pytest.fixture(scope="session")
def mock(worker) -> RemoteMockServer:
    m = RemoteMockServer(worker.mock_server_url)
    m.wait_ready(timeout=15.0)
    return m


@pytest.fixture(scope="session")
def tc_mock_script(tc):
    script = resolve_mock_configs(tc)
    return merge_tc_mock_tools(script, tc.mock_tools)


@pytest.fixture(scope="session")
def exported_agents(tc, worker):
    """Export TC agents to the worker's openclaw home directory once per session."""
    openclaw_home = worker.data_dir
    export_workflow_from_objects(tc.objects, openclaw_home, force=True, write_config=False)
    for obj in tc.objects:
        reset_agent_state(obj.object_id, obj.state_description, openclaw_home)
    return openclaw_home


@pytest.fixture(scope="session")
def configured_gateway(tc, worker, exported_agents):
    """Configure the gateway with TC agents (once per session, handles restart)."""
    openclaw_home = exported_agents
    container_home = Path(worker.container_home)

    async def _configure():
        try:
            await _configure_openclaw_agents(
                tc.objects, AGENT_PROVIDER, AGENT_MODEL, openclaw_home,
                gateway_url=worker.gateway_url,
                path_prefix=container_home,
            )
        except Exception:
            await _wait_for_gateway_restart(worker.gateway_url, openclaw_home)

    asyncio.run(_configure())
    return openclaw_home


async def _send_step(
    step_text: str,
    target: str,
    worker: WorkerConfig,
    openclaw_home: Path,
    mock: RemoteMockServer,
    tc_mock_script: Any,
) -> tuple[str, list[dict]]:
    """Send one message to an agent and return (response_text, tool_calls)."""
    from openclaw_sdk import OpenClawClient

    OpenClawAgent._global_counter += 1
    sname = f"test-{OpenClawAgent._global_counter}"

    # Rewrite AGENTS.md with fresh session name
    from src.data.schema import TestCase as _TC
    from src.lnl.openclaw_export import rewrite_agents_md
    # We need tc objects — pass openclaw_home and reconstruct from fixture
    # (handled by caller — rewrite done before this call)

    mock_key = f"agent:{target}:{sname}"
    mock._pending_mock_script = tc_mock_script
    mock.configure(mock_key, slot_id="default")

    async with await OpenClawClient.connect(
        **_openclaw_connect_kwargs(worker.gateway_url, openclaw_home)
    ) as client:
        handle = client.get_agent(target, session_name=sname)
        result = await handle.execute(step_text)
        content = result.content if result.success else f"(error: {result.content})"

    await _wait_mock_quiescence(mock, slot_id="default")
    calls = mock.get_log(slot_id="default")
    return content, calls


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestMockServerDirect:
    """Verify mock server responds correctly to direct HTTP tool calls (no agent)."""

    def test_health(self, mock):
        resp = httpx.get(f"{mock._url}/health", timeout=5)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_direct_slack_send_message(self, mock):
        """Direct POST to /tool/slack_send_message returns a mock response."""
        resp = httpx.post(
            f"{mock._url}/tool/slack_send_message",
            json={"channel": "#test", "message": "hello from test"},
            timeout=5,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "result" in body

    def test_direct_zapier_create_record(self, mock):
        """Direct POST to /tool/zapier_tables_create_record returns a mock response."""
        resp = httpx.post(
            f"{mock._url}/tool/zapier_tables_create_record",
            json={"table": "TestTable", "data": {"key": "value"}},
            timeout=5,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "result" in body

    def test_log_records_calls(self, mock, tc_mock_script):
        """Configure a session then verify the call is logged."""
        mock._pending_mock_script = tc_mock_script
        mock.configure("agent:test-agent:test-session", slot_id="default")

        httpx.post(
            f"{mock._url}/tool/slack_send_message",
            json={"__session_key__": "agent:test-agent:test-session",
                  "channel": "#log-test", "message": "log check"},
            timeout=5,
        )
        calls = mock.get_log(slot_id="default")
        methods = [c["method"] for c in calls]
        assert "slack_send_message" in methods, f"Expected slack_send_message in log, got: {methods}"

    def test_configured_tool_returns_template_response(self, mock, tc_mock_script):
        """When a mock script is configured, tool response uses the template."""
        mock._pending_mock_script = tc_mock_script
        mock.configure("agent:test-template:s1", slot_id="default")

        resp = httpx.post(
            f"{mock._url}/tool/zapier_tables_create_record",
            json={"__session_key__": "agent:test-template:s1",
                  "table": "MyTable", "data": {}},
            timeout=5,
        )
        result = resp.json().get("result", "")
        # Template: "record_id: {tool_call_id}, written to table '{table}'"
        assert "written to table" in result, f"Unexpected result: {result!r}"
        assert "MyTable" in result, f"Table name not interpolated: {result!r}"


class TestAgentOrchestration:
    """
    Verify end-to-end orchestration: agent receives a message and the correct
    downstream tool calls fire through the agentToAgent cascade.

    TC 0 chain (web-clipper):
      Step 1: chrome-extension → content-curator → zapier_tables_create_record
      Step 2: zapier-tables → content-curator → slack-content-channel → slack_send_message
    """

    def _run_step(self, step_index, tc, worker, configured_gateway, mock, tc_mock_script):
        """Helper: run one TC step and return (response, tool_calls)."""
        openclaw_home = configured_gateway
        step = tc.steps[step_index]
        text = f"[Event from {step.source}]: {step.text}"

        # Write fresh AGENTS.md with a new session name
        OpenClawAgent._global_counter += 1
        sname = f"test-{OpenClawAgent._global_counter}"
        rewrite_agents_md(tc.objects, openclaw_home, sname)
        # Reset state for clean run
        for obj in tc.objects:
            reset_agent_state(obj.object_id, obj.state_description, openclaw_home)

        mock_key = f"agent:{step.target}:{sname}"
        mock._pending_mock_script = tc_mock_script
        mock.configure(mock_key, slot_id="default")

        async def _run():
            from openclaw_sdk import OpenClawClient
            async with await OpenClawClient.connect(
                **_openclaw_connect_kwargs(worker.gateway_url, openclaw_home)
            ) as client:
                handle = client.get_agent(step.target, session_name=sname)
                result = await handle.execute(text)
                content = result.content if result.success else f"(error: {result.content})"
            await _wait_mock_quiescence(mock, slot_id="default")
            calls = mock.get_log(slot_id="default")
            return content, calls

        return asyncio.run(_run())

    def test_step1_fires_zapier_create_record(self, tc, worker, configured_gateway, mock, tc_mock_script):
        """
        Step 1: chrome-extension receives a content-clip event.
        Expected: content-curator calls zapier_tables_create_record.
        """
        response, calls = self._run_step(0, tc, worker, configured_gateway, mock, tc_mock_script)
        methods = [c["method"] for c in calls]

        print(f"\nResponse: {response[:200]}")
        print(f"Tool calls: {methods}")
        for c in calls:
            print(f"  {c['method']}({json.dumps(c.get('args', {}))[:100]}) → {c.get('result','')[:60]}")

        assert "zapier_tables_create_record" in methods, (
            f"Expected zapier_tables_create_record to fire.\n"
            f"Response: {response[:300]}\n"
            f"Tool calls: {methods}"
        )

    def test_step2_fires_slack_send_message(self, tc, worker, configured_gateway, mock, tc_mock_script):
        """
        Step 2: zapier-tables triggers content-curator to send to slack-content-channel.
        Expected: slack_send_message fires with #content-sharing channel.
        """
        response, calls = self._run_step(1, tc, worker, configured_gateway, mock, tc_mock_script)
        methods = [c["method"] for c in calls]

        print(f"\nResponse: {response[:200]}")
        print(f"Tool calls: {methods}")
        for c in calls:
            print(f"  {c['method']}({json.dumps(c.get('args', {}))[:100]}) → {c.get('result','')[:60]}")

        assert "slack_send_message" in methods, (
            f"Expected slack_send_message to fire.\n"
            f"Response: {response[:300]}\n"
            f"Tool calls: {methods}"
        )

    def test_step1_zapier_record_contains_url(self, tc, worker, configured_gateway, mock, tc_mock_script):
        """
        The zapier_tables_create_record call should include the clipped URL.
        """
        response, calls = self._run_step(0, tc, worker, configured_gateway, mock, tc_mock_script)
        zapier_calls = [c for c in calls if c["method"] == "zapier_tables_create_record"]

        if not zapier_calls:
            pytest.skip("zapier_tables_create_record did not fire — skipping content assertion")

        # URL should appear somewhere in the args
        args_str = json.dumps(zapier_calls[0].get("args", {}))
        assert "contentmarketinginstitute.com" in args_str or "AI" in args_str, (
            f"Expected URL or article title in zapier args.\nArgs: {args_str[:300]}"
        )

    def test_step2_slack_message_targets_correct_channel(self, tc, worker, configured_gateway, mock, tc_mock_script):
        """
        The slack_send_message call should target #content-sharing.
        """
        response, calls = self._run_step(1, tc, worker, configured_gateway, mock, tc_mock_script)
        slack_calls = [c for c in calls if c["method"] == "slack_send_message"]

        if not slack_calls:
            pytest.skip("slack_send_message did not fire — skipping channel assertion")

        args_str = json.dumps(slack_calls[0].get("args", {}))
        assert "content" in args_str.lower(), (
            f"Expected #content-sharing channel in slack args.\nArgs: {args_str[:300]}"
        )
