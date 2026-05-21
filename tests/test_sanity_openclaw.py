"""
OpenClaw sanity test — verifies the Docker worker is healthy and agents can
process messages in both single-agent and multi-agent modes.

Lighter-weight than test_openclaw_integration.py: only checks that agents
respond without errors and that agent-to-agent routing fires in multi-agent
mode. Does NOT assert specific tool-call arguments.

Requires:
    - Docker pool running: ./docker/start-pool.sh
    - Worker 1 at ws://localhost:19789 (gateway) + http://localhost:19888 (mock)
    - OPENAI_API_KEY in environment

Run:
    pytest tests/test_sanity_openclaw.py -v -s
    pytest tests/test_sanity_openclaw.py -v -s --worker 2
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

from src.data.schema import ObjectDef, PeerDecl, Step, Sample
from src.data.mock_server import merge_tc_mock_tools, resolve_mock_configs
from src.lnl.openclaw_export import (
    export_single_agent_workspace,
    export_workflow_from_objects,
    reset_agent_state,
    reset_single_agent_state,
    rewrite_agents_md,
)
from src.data.evaluate_baseline import (
    OpenClawAgent,
    RemoteMockServer,
    WorkerConfig,
    _load_pool_config,
    _openclaw_connect_kwargs,
    _wait_for_gateway,
    _wait_mock_quiescence,
    _write_worker_config,
)

# ── Constants ──────────────────────────────────────────────────────────────────

POOL_CONFIG = ROOT / "docker" / "worker-pool.yaml"
TC_PATH = ROOT / "outputs" / "data" / "zapier" / "20260411_zapier_clean" / "workflows-mods.jsonl"
AGENT_MODEL = "gpt-5.4-mini"
AGENT_PROVIDER = "openai"
SINGLE_AGENT_ID = "lnl-sanity"
TC_INDEX = 0  # web-clipper: chrome-extension → content-curator → slack-content-channel

# ── Inline 2-agent TC: lead capture ───────────────────────────────────────────
# Distilled from lead-capture-temporal-TC001 (outputs/data/zapier/20260421_zapier_fixed/).
# Topology: contact-form → lead-capture-policy → zapier_tables_create_record + email_send
# The downstream write objects (leads-table, sales-notification) are collapsed into
# direct tool calls inside lead-capture-policy, keeping the chain to 2 agents.

LEAD_CAPTURE_TC = Sample(
    id="lead-capture-sanity",
    name="Lead Capture 2-agent sanity",
    domain="general",
    source_type="Zapier/Workflow Logic",
    link="",
    objects=[
        ObjectDef(
            object_id="contact-form",
            role="Entry-point form that captures lead submissions.",
            state_description="Active. Standard contact form configuration.",
            behavior=(
                "When a user submits the Contact Us form, forward the full lead payload "
                "(Full Name, Email Address, Phone Number, Company Name, Message) to "
                "lead-capture-policy."
            ),
            peers=[PeerDecl(
                object_id="lead-capture-policy",
                relationship="Forward complete lead submission payload when a new form submission is received",
            )],
            event_sources=["external system: Zapier Interfaces form submission"],
        ),
        ObjectDef(
            object_id="lead-capture-policy",
            role=(
                "Business logic object that validates lead fields, stores new leads in the "
                "Zapier Tables leads table, and emails the Sales Manager."
            ),
            state_description="Active. Leads Zapier table URL: https://tables.zapier.com/app/tables/t/leads.",
            behavior=(
                "When a lead submission arrives, confirm Full Name and Email are present. "
                "Then call zapier_tables_create_record to write the lead to the Leads table, "
                "and call email_send to notify the Sales Manager with the lead's Full Name, "
                "Email Address, Phone Number, Company Name, and Message."
            ),
            peers=[],
        ),
    ],
    steps=[
        Step(
            text=(
                "A user submits the Contact Us form: Full Name: Sarah Mitchell, "
                "Email Address: sarah.mitchell@techcorp.com, Phone: +1-415-555-0192, "
                "Company: TechCorp Solutions, "
                "Message: 'We are interested in your enterprise pricing and integration options.'"
            ),
            target="contact-form",
            source="Zapier Interfaces form submission",
        ),
    ],
    modifications=[],
    events=[],
    mock_tools=[],
)


# ── CLI option ─────────────────────────────────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption("--worker", type=int, default=1, help="Pool worker to use (default: 1)")


# ── Shared session fixtures ────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def worker_num(request):
    return request.config.getoption("--worker", default=1)


@pytest.fixture(scope="session")
def worker(worker_num) -> WorkerConfig:
    workers = _load_pool_config(POOL_CONFIG)
    assert 1 <= worker_num <= len(workers), f"--worker must be 1-{len(workers)}"
    return workers[worker_num - 1]


@pytest.fixture(scope="session")
def tc() -> Sample:
    with open(TC_PATH) as f:
        lines = [l for l in f if l.strip()]
    return Sample(**json.loads(lines[TC_INDEX]))


@pytest.fixture(scope="session")
def mock(worker) -> RemoteMockServer:
    m = RemoteMockServer(worker.mock_server_url)
    m.wait_ready(timeout=30.0)
    return m


@pytest.fixture(scope="session")
def tc_mock_script(tc):
    script = resolve_mock_configs(tc)
    if script is None:
        return None
    return merge_tc_mock_tools(script, tc.mock_tools)


# ── Helper: restart container and wait for ready ───────────────────────────────

def _restart_and_wait(worker: WorkerConfig) -> None:
    if worker.container_name:
        print(f"\n  Restarting {worker.container_name} …", flush=True)
        subprocess.run(
            ["docker", "restart", worker.container_name],
            check=True, capture_output=True, timeout=60,
        )

    RemoteMockServer(worker.mock_server_url).wait_ready(timeout=60.0)

    asyncio.run(_wait_for_gateway(
        gateway_url=worker.gateway_url,
        openclaw_home=worker.data_dir,
        timeout_s=60.0,
        stable_for_s=3.0,
    ))
    print("  Gateway ready.", flush=True)


# ── Helper: send one step via OpenClaw SDK ─────────────────────────────────────

async def _send_step(
    step_text: str,
    target_agent_id: str,
    worker: WorkerConfig,
    mock: RemoteMockServer,
    tc_mock_script: Any,
) -> tuple[str, list[dict]]:
    """Open a one-shot session, send step_text, return (response, tool_calls)."""
    from openclaw_sdk import OpenClawClient

    OpenClawAgent._global_counter += 1
    sname = f"sanity-{OpenClawAgent._global_counter}"

    if tc_mock_script is not None:
        mock._pending_mock_script = tc_mock_script
        mock.configure(f"agent:{target_agent_id}:{sname}", slot_id="default")

    async with await OpenClawClient.connect(
        **_openclaw_connect_kwargs(worker.gateway_url, worker.data_dir)
    ) as client:
        handle = client.get_agent(target_agent_id, session_name=sname)
        result = await handle.execute(step_text)
        content = result.content if result.success else f"(error: {result.content})"

    if tc_mock_script is not None:
        await _wait_mock_quiescence(mock, slot_id="default")
    calls = mock.get_log(slot_id="default") if tc_mock_script is not None else []
    return content, calls


# ── Single-agent setup ─────────────────────────────────────────────────────────

@pytest.fixture(scope="class")
def single_agent_gateway(tc, worker):
    """Configure worker for single-agent mode and yield openclaw_home."""
    openclaw_home = worker.data_dir
    container_home = Path(worker.container_home)

    export_single_agent_workspace(tc.objects, openclaw_home, agent_id=SINGLE_AGENT_ID, force=True)
    reset_single_agent_state(tc.objects, openclaw_home, agent_id=SINGLE_AGENT_ID)

    _restart_and_wait(worker)

    all_ids = {SINGLE_AGENT_ID}
    _write_worker_config(worker, all_ids, AGENT_PROVIDER, AGENT_MODEL, single_agent_id=SINGLE_AGENT_ID)

    asyncio.run(_wait_for_gateway(
        gateway_url=worker.gateway_url,
        openclaw_home=openclaw_home,
        timeout_s=30.0,
        stable_for_s=3.0,
    ))
    print("  Single-agent gateway configured.", flush=True)
    yield openclaw_home


# ── Multi-agent setup ──────────────────────────────────────────────────────────

@pytest.fixture(scope="class")
def multi_agent_gateway(tc, worker):
    """Configure worker for multi-agent mode and yield openclaw_home."""
    openclaw_home = worker.data_dir
    container_home = Path(worker.container_home)

    export_workflow_from_objects(tc.objects, openclaw_home, force=True, write_config=False)
    for obj in tc.objects:
        reset_agent_state(obj.object_id, obj.state_description, openclaw_home)

    OpenClawAgent._global_counter += 1
    sname = f"sanity-{OpenClawAgent._global_counter}"
    rewrite_agents_md(tc.objects, openclaw_home, sname)

    _restart_and_wait(worker)

    all_ids = {obj.object_id for obj in tc.objects}
    _write_worker_config(worker, all_ids, AGENT_PROVIDER, AGENT_MODEL, single_agent_id=None)

    asyncio.run(_wait_for_gateway(
        gateway_url=worker.gateway_url,
        openclaw_home=openclaw_home,
        timeout_s=30.0,
        stable_for_s=3.0,
    ))
    print("  Multi-agent gateway configured.", flush=True)
    yield openclaw_home


# ── Test classes ───────────────────────────────────────────────────────────────

class TestSanityGateway:
    """Basic liveness checks — no agent invocation."""

    def test_mock_server_health(self, mock):
        resp = httpx.get(f"{mock._url}/health", timeout=5)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_gateway_health(self, worker):
        ws_url = worker.gateway_url
        http_url = ws_url.replace("ws://", "http://").replace("wss://", "https://")
        resp = httpx.get(f"{http_url}/health", timeout=5)
        assert resp.status_code == 200


class TestSanitySingleAgent:
    """Verify the combined single-agent can receive and respond to a step."""

    def test_agent_responds(self, tc, worker, mock, tc_mock_script, single_agent_gateway):
        step = tc.steps[0]
        step_text = f"[Event from {step.source}]: {step.text}"

        reset_single_agent_state(tc.objects, single_agent_gateway, agent_id=SINGLE_AGENT_ID)

        response, calls = asyncio.run(_send_step(
            step_text, SINGLE_AGENT_ID, worker, mock, tc_mock_script,
        ))

        print(f"\n[single-agent] Response: {response[:300]}", flush=True)
        if calls:
            methods = [c["method"] for c in calls]
            print(f"[single-agent] Tool calls: {methods}", flush=True)

        assert "(error:" not in response, f"Agent returned an error: {response[:300]}"
        assert len(response.strip()) > 0, "Agent returned an empty response"


class TestSanityMultiAgent:
    """Verify agent-to-agent routing works across the object chain."""

    def _run_step(
        self, step_idx: int, tc, worker, mock, tc_mock_script, multi_agent_gateway
    ) -> tuple[str, list[dict]]:
        step = tc.steps[step_idx]
        step_text = f"[Event from {step.source}]: {step.text}"

        for obj in tc.objects:
            reset_agent_state(obj.object_id, obj.state_description, multi_agent_gateway)

        OpenClawAgent._global_counter += 1
        sname = f"sanity-{OpenClawAgent._global_counter}"
        rewrite_agents_md(tc.objects, multi_agent_gateway, sname)

        return asyncio.run(_send_step(step_text, step.target, worker, mock, tc_mock_script))

    def test_step1_agent_responds(self, tc, worker, mock, tc_mock_script, multi_agent_gateway):
        response, calls = self._run_step(0, tc, worker, mock, tc_mock_script, multi_agent_gateway)

        print(f"\n[multi-agent step1] Response: {response[:300]}", flush=True)
        if calls:
            methods = [c["method"] for c in calls]
            print(f"[multi-agent step1] Tool calls: {methods}", flush=True)

        assert "(error:" not in response, f"Agent returned an error: {response[:300]}"
        assert len(response.strip()) > 0, "Agent returned an empty response"

    def test_step1_downstream_agent_invoked(self, tc, worker, mock, tc_mock_script, multi_agent_gateway):
        """A2A routing: step 1 targets chrome-extension, which should invoke content-curator."""
        if tc_mock_script is None:
            pytest.skip("No mock script available — cannot verify tool calls")

        response, calls = self._run_step(0, tc, worker, mock, tc_mock_script, multi_agent_gateway)
        methods = [c["method"] for c in calls]

        print(f"\n[multi-agent A2A] Tool calls: {methods}", flush=True)
        print(f"[multi-agent A2A] Response: {response[:300]}", flush=True)

        assert len(calls) > 0, (
            f"No tool calls recorded — expected content-curator to fire a downstream tool.\n"
            f"Response: {response[:400]}"
        )


# ── Lead-capture fixtures ──────────────────────────────────────────────────────

@pytest.fixture(scope="class")
def lc_mock_script():
    """Resolve mock script for the inline 2-agent lead-capture TC."""
    script = resolve_mock_configs(LEAD_CAPTURE_TC)
    assert script is not None, "Expected zapier + email mock configs to resolve"
    return script


@pytest.fixture(scope="class")
def lead_capture_gateway(worker):
    """Configure worker for the 2-agent lead-capture TC (multi-agent mode)."""
    openclaw_home = worker.data_dir

    export_workflow_from_objects(LEAD_CAPTURE_TC.objects, openclaw_home, force=True, write_config=False)
    for obj in LEAD_CAPTURE_TC.objects:
        reset_agent_state(obj.object_id, obj.state_description, openclaw_home)

    OpenClawAgent._global_counter += 1
    rewrite_agents_md(LEAD_CAPTURE_TC.objects, openclaw_home, f"sanity-{OpenClawAgent._global_counter}")

    _restart_and_wait(worker)

    all_ids = {obj.object_id for obj in LEAD_CAPTURE_TC.objects}
    _write_worker_config(worker, all_ids, AGENT_PROVIDER, AGENT_MODEL, single_agent_id=None)

    asyncio.run(_wait_for_gateway(
        gateway_url=worker.gateway_url,
        openclaw_home=openclaw_home,
        timeout_s=30.0,
        stable_for_s=3.0,
    ))
    print("  Lead-capture gateway configured.", flush=True)
    yield openclaw_home


class TestSanityLeadCapture:
    """
    2-agent lead-capture sanity test derived from lead-capture-temporal-TC001.

    Topology: contact-form --[A2A]--> lead-capture-policy
                                             ├── zapier_tables_create_record  (store lead)
                                             └── email_send                   (notify Sales Manager)
    """

    def _run(self, worker, mock, lc_mock_script, lead_capture_gateway) -> tuple[str, list[dict]]:
        openclaw_home = lead_capture_gateway
        step = LEAD_CAPTURE_TC.steps[0]
        step_text = f"[Event from {step.source}]: {step.text}"

        for obj in LEAD_CAPTURE_TC.objects:
            reset_agent_state(obj.object_id, obj.state_description, openclaw_home)

        OpenClawAgent._global_counter += 1
        sname = f"sanity-{OpenClawAgent._global_counter}"
        rewrite_agents_md(LEAD_CAPTURE_TC.objects, openclaw_home, sname)

        return asyncio.run(_send_step(step_text, step.target, worker, mock, lc_mock_script))

    def test_agent_responds(self, worker, mock, lc_mock_script, lead_capture_gateway):
        response, calls = self._run(worker, mock, lc_mock_script, lead_capture_gateway)
        methods = [c["method"] for c in calls]

        print(f"\n[lead-capture] Response: {response[:300]}", flush=True)
        print(f"[lead-capture] Tool calls: {methods}", flush=True)

        assert "(error:" not in response, f"Agent returned an error: {response[:300]}"
        assert len(response.strip()) > 0, "Agent returned an empty response"

    def test_lead_stored_in_zapier_table(self, worker, mock, lc_mock_script, lead_capture_gateway):
        response, calls = self._run(worker, mock, lc_mock_script, lead_capture_gateway)
        methods = [c["method"] for c in calls]

        print(f"\n[lead-capture] Tool calls: {methods}", flush=True)
        assert "zapier_tables_create_record" in methods, (
            f"Expected zapier_tables_create_record to fire.\n"
            f"Tool calls: {methods}\nResponse: {response[:300]}"
        )

    def test_sales_manager_emailed(self, worker, mock, lc_mock_script, lead_capture_gateway):
        response, calls = self._run(worker, mock, lc_mock_script, lead_capture_gateway)
        methods = [c["method"] for c in calls]

        print(f"\n[lead-capture] Tool calls: {methods}", flush=True)
        assert "email_send" in methods, (
            f"Expected email_send to fire.\n"
            f"Tool calls: {methods}\nResponse: {response[:300]}"
        )
