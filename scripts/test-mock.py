#!/usr/bin/env python3
"""
Diagnostic script: send a single message to a TC's entry agent via a Docker
pool worker and print every mock tool call that fires.

Usage:
    python scripts/test-mock.py --tc 0
    python scripts/test-mock.py --tc 0 --worker 1 --message "User clicked the Chrome extension on https://example.com"
    python scripts/test-mock.py --tc 0 --steps        # run all TC steps
    python scripts/test-mock.py --list-tcs             # show all available TCs

The script:
  1. Reads the pool config (docker/worker-pool.yaml) to find the gateway + mock URLs
  2. Exports the TC's agents to the worker's openclaw home
  3. Configures the agent model via the gateway
  4. Sends the message (or all steps if --steps)
  5. Waits for the agentToAgent cascade to quiesce
  6. Prints every tool call the mock server recorded
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

import httpx
import yaml

from src.data.schema import Sample
from src.data.mock_server import merge_tc_mock_tools, resolve_mock_configs
from src.lnl.openclaw_export import export_workflow_from_objects, rewrite_agents_md, reset_agent_state
from src.data.evaluate_baseline import (
    OpenClawAgent,
    RemoteMockServer,
    WorkerConfig,
    _load_pool_config,
    _openclaw_connect_kwargs,
    _configure_openclaw_agents,
    _wait_mock_quiescence,
)


def load_test_cases(path: Path) -> list[Sample]:
    tcs = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                tcs.append(Sample(**json.loads(line)))
    return tcs


def print_tool_calls(calls: list[dict]) -> None:
    if not calls:
        print("  (no tool calls recorded)")
        return
    for i, c in enumerate(calls, 1):
        args_str = json.dumps(c.get("args", {}), ensure_ascii=False)
        if len(args_str) > 120:
            args_str = args_str[:120] + "…"
        print(f"  [{i}] {c['method']}({args_str})")
        print(f"       → {c.get('result', '')[:100]}")


async def run(args: argparse.Namespace) -> None:
    # ── Load pool config ──────────────────────────────────────────────────────
    pool_path = ROOT / "docker" / "worker-pool.yaml"
    workers = _load_pool_config(pool_path)
    if args.worker < 1 or args.worker > len(workers):
        print(f"Error: --worker must be 1-{len(workers)}")
        sys.exit(1)
    worker = workers[args.worker - 1]
    print(f"Worker {args.worker}: gateway={worker.gateway_url}  mock={worker.mock_server_url}")

    # ── Load test cases ───────────────────────────────────────────────────────
    tc_path = ROOT / args.input
    tcs = load_test_cases(tc_path)

    if args.list_tcs:
        for i, tc in enumerate(tcs):
            objects_str = ", ".join(o.object_id for o in tc.objects)
            print(f"  [{i:3d}] {tc.id}  ({len(tc.objects)} agents: {objects_str})")
        return

    if args.tc < 0 or args.tc >= len(tcs):
        print(f"Error: --tc must be 0-{len(tcs) - 1}")
        sys.exit(1)
    tc = tcs[args.tc]
    print(f"\nTC [{args.tc}]: {tc.id}")
    print(f"Agents: {[o.object_id for o in tc.objects]}")

    # ── Set up remote mock server ─────────────────────────────────────────────
    mock = RemoteMockServer(worker.mock_server_url)
    mock.wait_ready()
    print(f"Mock server: ready")

    # Resolve and merge mock configs
    tc_mock_script = resolve_mock_configs(tc)
    tc_mock_script = merge_tc_mock_tools(tc_mock_script, tc.mock_tools)  # tc tools win on collision
    tool_names = [t.method for sys_def in (tc_mock_script.systems if tc_mock_script else []) for t in sys_def.tools]
    print(f"Mock tools: {tool_names or '(none)'}")

    # ── Export agents to worker ───────────────────────────────────────────────
    openclaw_home = worker.data_dir
    container_home = Path(worker.container_home)

    export_workflow_from_objects(tc.objects, openclaw_home, force=True, write_config=False)
    for obj in tc.objects:
        reset_agent_state(obj.object_id, obj.state_description, openclaw_home)
    print(f"Agents exported to: {openclaw_home}")

    # ── Connect and configure agents ──────────────────────────────────────────
    from openclaw_sdk import OpenClawClient

    provider = args.provider
    model = args.model

    print(f"\nConfiguring agents (model={provider}/{model}) …")
    from src.data.evaluate_baseline import _wait_for_gateway_restart
    try:
        await _configure_openclaw_agents(
            tc.objects, provider, model, openclaw_home,
            gateway_url=worker.gateway_url,
            path_prefix=container_home,
        )
    except Exception:
        # Gateway may disconnect mid-patch (expected on config change) —
        # wait for it to come back up before continuing.
        await _wait_for_gateway_restart(worker.gateway_url, openclaw_home)
    print("Gateway configured.")

    # ── Determine messages to send ────────────────────────────────────────────
    if args.steps:
        messages = [
            {"target": s.target, "text": f"[Event from {s.source}]: {s.text}"}
            for s in tc.steps
        ]
    elif args.message:
        entry = tc.steps[0].target if tc.steps else tc.objects[0].object_id
        messages = [{"target": args.target or entry, "text": args.message}]
    else:
        # Default: first step
        if not tc.steps:
            print("Error: TC has no steps. Use --message.")
            sys.exit(1)
        s = tc.steps[0]
        messages = [{"target": s.target, "text": f"[Event from {s.source}]: {s.text}"}]

    # ── Send messages ─────────────────────────────────────────────────────────
    async with await OpenClawClient.connect(
        **_openclaw_connect_kwargs(worker.gateway_url, openclaw_home)
    ) as client:

        for msg_idx, msg in enumerate(messages):
            target = msg["target"]
            text = msg["text"]

            # Fresh session per message
            OpenClawAgent._global_counter += 1
            sname = f"test-{OpenClawAgent._global_counter}"
            rewrite_agents_md(tc.objects, openclaw_home, sname)
            handle = client.get_agent(target, session_name=sname)

            # Configure mock server (set pending script before configure clears it)
            mock_key = f"agent:{target}:{sname}"
            mock._pending_mock_script = tc_mock_script
            mock.configure(mock_key, slot_id="default")

            print(f"\n{'─'*60}")
            print(f"Message {msg_idx + 1}/{len(messages)}")
            print(f"  target : {target}")
            print(f"  session: {mock_key}")
            print(f"  text   : {text[:120]}")
            print(f"{'─'*60}")

            t0 = time.time()
            result = await handle.execute(text)
            elapsed = time.time() - t0

            print(f"\nResponse ({elapsed:.1f}s):")
            content = result.content if result.success else f"(error: {result.content})"
            # Print first 400 chars
            print(f"  {content[:400]}")
            if len(content) > 400:
                print(f"  … ({len(content)} chars total)")

            # Wait for cascade
            await _wait_mock_quiescence(mock, slot_id="default")

            calls = mock.get_log(slot_id="default")
            print(f"\nTool calls ({len(calls)}):")
            print_tool_calls(calls)


def main() -> None:
    parser = argparse.ArgumentParser(description="Test mock tool orchestration against a Docker pool worker")
    parser.add_argument("--input", "-i",
                        default="outputs/data/zapier/20260411_zapier_clean/samples.jsonl",
                        help="Path to samples.jsonl (relative to repo root)")
    parser.add_argument("--tc", type=int, default=0, metavar="N",
                        help="Index of the test case to run (default: 0)")
    parser.add_argument("--worker", type=int, default=1, metavar="N",
                        help="Worker number to use (default: 1)")
    parser.add_argument("--model", default="gpt-4o",
                        help="Agent model (default: gpt-4o)")
    parser.add_argument("--provider", default="openai",
                        help="Agent provider (default: openai)")
    parser.add_argument("--message", default=None,
                        help="Custom message to send (instead of first step)")
    parser.add_argument("--target", default=None,
                        help="Target agent ID for --message (default: first step's target)")
    parser.add_argument("--steps", action="store_true",
                        help="Send all TC steps sequentially")
    parser.add_argument("--list-tcs", action="store_true",
                        help="List all test cases and exit")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
