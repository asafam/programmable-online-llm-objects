"""
Baseline evaluation runner — OpenClaw single-agent comparison for the LNL experiment.

Runs the same TestCases as evaluate.py but uses a single OpenClaw agent
(one conversation session) instead of the multi-object LNL runtime.
The agent receives all object definitions as context and processes steps,
modifications, and events sequentially.

Requires:
    - OpenClaw daemon running (openclaw gateway status)
    - openclaw-sdk installed (pip install openclaw-sdk)

Usage:
    python -m src.data.evaluate_baseline \
        -i outputs/data/zapier/20260322_010211/test_cases.jsonl \
        --runs 3
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

load_dotenv()

from src.data.schema import (
    EvalSummary,
    EventResult,
    MockScript,
    ModificationResult,
    ObjectDef,
    TestCase,
    TestCaseResult,
)
from src.data.mock_server import MockServer, resolve_mock_configs, resolve_orchestration
from src.data.utils import (
    add_common_args,
    infer_provider,
    load_jsonl,
    print_run_info,
)


# ── Prompt building ──────────────────────────────────────────────────────────

_PROMPT_CONFIG: Optional[dict] = None


def _load_prompt_config() -> dict:
    global _PROMPT_CONFIG
    if _PROMPT_CONFIG is None:
        config_path = (
            Path(__file__).parent.parent.parent
            / "config"
            / "prompts"
            / "baseline"
            / "agent.yaml"
        )
        with open(config_path) as f:
            _PROMPT_CONFIG = yaml.safe_load(f)
    return _PROMPT_CONFIG


def _format_components(objects: list[ObjectDef]) -> str:
    """Format all object definitions into a single components description."""
    parts: list[str] = []
    for obj in objects:
        lines = [
            f"### {obj.object_id}",
            f"**Role:** {obj.role}",
            f"**Behavior:** {obj.behavior}",
        ]
        if obj.peers:
            lines.append("**Peers:**")
            for p in obj.peers:
                lines.append(f"  - {p.object_id}: {p.relationship}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _format_seed_data(objects: list[ObjectDef]) -> str:
    """Collect and format seed_data from all objects into a single reference block."""
    combined = {obj.object_id: obj.seed_data for obj in objects if obj.seed_data}
    return json.dumps(combined, indent=2) if combined else "(none)"


def build_system_prompt(tc: TestCase) -> str:
    """Build the single-agent system prompt from a TestCase."""
    config = _load_prompt_config()
    template = config["system_prompt"]

    return template.format(
        workflow_name=tc.name,
        components=_format_components(tc.objects),
        seed_data=_format_seed_data(tc.objects),
        current_state="(empty)",
    )


# ── Timestamp parsing ────────────────────────────────────────────────────────

def parse_when(when: str) -> int:
    """Convert 'W02-1T10:30' → ordinal minutes for sorting."""
    week_part, time_part = when.split("T")
    w, d = week_part.lstrip("W").split("-")
    h, m = time_part.split(":")
    return (int(w) * 7 + int(d)) * 1440 + int(h) * 60 + int(m)


# ── Evidence gathering ───────────────────────────────────────────────────────

def gather_evidence(content: str, tool_calls: Optional[list[dict]] = None) -> str:
    """Collect observable evidence from an OpenClaw agent response.

    Tries to parse as JSON (structured response); falls back to raw text.
    Optionally appends tool call records from the MockServer.
    """
    try:
        data = json.loads(content)
        parts: list[str] = []

        reply = data.get("reply", "").strip()
        if reply:
            parts.append(f"Reply: {reply}")

        state = data.get("updated_state", {})
        if state:
            parts.append(f"State:\n{json.dumps(state, indent=2)}")

        if tool_calls:
            lines = []
            for tc in tool_calls:
                if tc.get("is_callback"):
                    lines.append(f"  - [{tc['method']}] {tc['result']}")
                else:
                    lines.append(f"  - {tc['method']}({json.dumps(tc.get('args', {}))}) → {tc['result']}")
            parts.append("Tool calls:\n" + "\n".join(lines))

        return "\n\n".join(parts) if parts else content
    except (json.JSONDecodeError, AttributeError):
        # OpenClaw returned plain text — use as-is
        return content if content.strip() else "(no observable output)"


# ── OpenClaw agent wrapper ───────────────────────────────────────────────────

class OpenClawAgent:
    """Wraps the OpenClaw SDK for sequential message execution."""

    def __init__(self, agent_id: str = "lnl-baseline", gateway_url: Optional[str] = None):
        self._agent_id = agent_id
        self._gateway_url = gateway_url
        self._session_counter = 0

    async def _execute_session(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """Run a full session: send system prompt + messages, collect results.

        Returns list of {content, latency_ms} dicts, one per message.
        """
        from openclaw_sdk import OpenClawClient

        connect_kwargs: dict[str, Any] = {}
        if self._gateway_url:
            connect_kwargs["gateway_ws_url"] = self._gateway_url

        self._session_counter += 1
        session_name = f"eval-{self._session_counter}"

        results = []
        async with OpenClawClient.connect(**connect_kwargs) as client:
            agent = client.get_agent(self._agent_id, session_name=session_name)

            # Prime with system prompt
            t0 = time.time()
            init_result = await agent.execute(
                f"[SYSTEM INSTRUCTIONS — follow these for the entire conversation]\n\n{system_prompt}\n\n"
                f"Acknowledge by replying with a JSON object: "
                f'{{"reasoning": "understood", "updated_state": {{}}, "reply": "Ready."}}'
            )
            init_latency = (time.time() - t0) * 1000

            # Send each message
            for msg in messages:
                t0 = time.time()
                result = await agent.execute(msg["content"])
                latency_ms = (time.time() - t0) * 1000
                results.append({
                    "content": result.content if result.success else f"(error: {result.content})",
                    "latency_ms": latency_ms,
                })

        return results

    def run_session(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """Synchronous wrapper around _execute_session."""
        return asyncio.run(self._execute_session(system_prompt, messages))


# ── Core execution ───────────────────────────────────────────────────────────

def _execute_test_case_inner(
    tc: TestCase,
    agent: OpenClawAgent,
    harness,
    mock_server: Optional["MockServer"] = None,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Run a single TestCase against an OpenClaw agent and return results."""

    sys_prompt = build_system_prompt(tc)

    # Build the ordered list of messages to send
    messages: list[dict[str, Any]] = []

    # Steps
    for i, step in enumerate(tc.steps):
        messages.append({
            "kind": "step",
            "index": i,
            "content": f"[Event from {step.target}]: {step.text}",
            "expect": step.expect,
        })

    # Timeline (mods + events sorted by when)
    timeline: list[tuple[int, str, Any]] = []
    for mod in tc.modifications:
        timeline.append((parse_when(mod.when), "mod", mod))
    for evt in tc.events:
        timeline.append((parse_when(evt.when), "event", evt))
    timeline.sort(key=lambda x: x[0])

    for _, kind, item in timeline:
        if kind == "mod":
            messages.append({
                "kind": "mod",
                "item": item,
                "content": f"[Administrative instruction at {item.when}]: {item.intent}",
            })
        else:
            # Skip pre-scripted injection for events handled by orchestration
            if mock_server and item.triggered_by is not None:
                continue
            messages.append({
                "kind": "event",
                "item": item,
                "content": f"[Event from {item.source} at {item.when} to {item.recipient}]: {item.input}",
            })

    # Execute all messages through OpenClaw
    openclaw_messages = [{"content": m["content"]} for m in messages]

    if mock_server:
        mock_server.configure(agent._session_counter + 1)

    results = agent.run_session(sys_prompt, openclaw_messages)

    # Collect MockServer call log (if active)
    mock_log: list[dict] = []
    if mock_server:
        # Brief wait for any pending orchestration reactions to fire
        import time as _time
        _time.sleep(0.5)
        mock_log = mock_server.get_log()

    # Map results back to event/mod results
    event_results: list[EventResult] = []
    mod_results: list[ModificationResult] = []

    for msg_meta, result in zip(messages, results):
        content = result["content"]
        latency_ms = result["latency_ms"]

        if msg_meta["kind"] == "step":
            expect = msg_meta["expect"]
            if expect is not None:
                evidence = gather_evidence(content, tool_calls=mock_log if mock_server else None)
                passed, reasoning = harness.evaluate_assertion(expect.action, evidence)
                event_results.append(EventResult(
                    event_id=f"S{msg_meta['index']+1:03d}",
                    passed=passed,
                    reasoning=reasoning,
                    latency_ms=latency_ms,
                ))

        elif msg_meta["kind"] == "mod":
            mod_results.append(ModificationResult(
                mod_id=msg_meta["item"].id,
                latency_ms=latency_ms,
            ))

        else:  # pre-scripted event (no triggered_by)
            item = msg_meta["item"]
            evidence = gather_evidence(content, tool_calls=mock_log if mock_server else None)
            passed, reasoning = harness.evaluate_assertion(item.expect.action, evidence)
            event_results.append(EventResult(
                event_id=item.id,
                passed=passed,
                reasoning=reasoning,
                latency_ms=latency_ms,
            ))

    # Evaluate orchestration-triggered events against the full mock log as evidence
    if mock_server:
        orchestration_evidence = gather_evidence("{}", tool_calls=mock_log)
        for evt in tc.events:
            if evt.triggered_by is None:
                continue
            # Check whether the expected reaction appears in the log
            reaction_log = [
                e for e in mock_log
                if e.get("is_orchestration") and evt.source in e.get("method", "")
            ]
            evidence = gather_evidence("{}", tool_calls=reaction_log) if reaction_log else "(no orchestration reaction fired)"
            passed, reasoning = harness.evaluate_assertion(evt.expect.action, evidence)
            event_results.append(EventResult(
                event_id=evt.id,
                passed=passed,
                reasoning=reasoning,
            ))

    return event_results, mod_results


def execute_test_case(
    tc: TestCase,
    agent: OpenClawAgent,
    harness,
    timeout_s: Optional[float] = None,
    mock_server: Optional[MockServer] = None,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Run a single TestCase with an optional wall-clock timeout."""
    if timeout_s is None:
        return _execute_test_case_inner(tc, agent, harness, mock_server=mock_server)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_execute_test_case_inner, tc, agent, harness, mock_server)
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            future.cancel()
            event_results = [
                EventResult(
                    event_id=evt.id,
                    passed=False,
                    reasoning=f"Timeout after {timeout_s}s",
                )
                for evt in tc.events
            ]
            mod_results = [
                ModificationResult(mod_id=mod.id)
                for mod in tc.modifications
            ]
            return event_results, mod_results


# ── Output path ──────────────────────────────────────────────────────────────

def default_output_path(input_path: Path) -> Path:
    return input_path.parent / f"{input_path.stem}_baseline.jsonl"


# ── Summary ──────────────────────────────────────────────────────────────────

def _compute_summary(results: list[TestCaseResult]) -> EvalSummary:
    """Compute aggregate metrics across all test case results."""
    all_events = [e for r in results for e in r.events]
    all_mods = [m for r in results for m in r.modifications]
    total_runs = len(results)
    total_test_cases = len({r.tc_id for r in results})

    def mean(vals):
        return sum(vals) / len(vals) if vals else 0.0

    pass_rates = [r.pass_rate for r in results]
    mean_pass_rate = mean(pass_rates)

    by_tc: dict[str, list[float]] = defaultdict(list)
    for r in results:
        by_tc[r.tc_id].append(r.pass_rate)
    per_tc_stds = [
        statistics.stdev(rates) for rates in by_tc.values() if len(rates) > 1
    ]
    pass_rate_std = mean(per_tc_stds)

    return EvalSummary(
        total_test_cases=total_test_cases,
        total_runs=total_runs,
        total_events=len(all_events),
        mean_pass_rate=mean_pass_rate,
        pass_rate_std=pass_rate_std,
        mean_event_input_tokens=mean([e.input_tokens for e in all_events]),
        mean_event_output_tokens=mean([e.output_tokens for e in all_events]),
        mean_event_latency_ms=mean([e.latency_ms for e in all_events]),
        mean_mod_input_tokens=mean([m.input_tokens for m in all_mods]),
        mean_mod_output_tokens=mean([m.output_tokens for m in all_mods]),
        mean_mod_latency_ms=mean([m.latency_ms for m in all_mods]),
    )


# ── Main runner ──────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> Path:
    """Run baseline evaluation. Returns the output path."""
    if args.output is None:
        args.output = default_output_path(args.input)

    if not args.input.exists():
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    test_cases = load_jsonl(args.input, TestCase)

    if args.limit:
        test_cases = test_cases[: args.limit]

    timeout_s: Optional[float] = getattr(args, "timeout", None)

    print(f"Loaded {len(test_cases)} test cases from {args.input}")
    print(f"Mode: baseline (OpenClaw single agent)")
    print(f"Agent ID: {args.agent_id}")
    print(f"Runs per test case: {args.runs}")
    print(f"Timeout per run: {timeout_s}s" if timeout_s else "Timeout: none")
    if args.gateway_url:
        print(f"Gateway: {args.gateway_url}")
    print()

    # Build OpenClaw agent
    agent = OpenClawAgent(
        agent_id=args.agent_id,
        gateway_url=args.gateway_url,
    )

    # Build MockServer (optional)
    mock_server: Optional[MockServer] = None
    if getattr(args, "mock_server", False):
        openclaw_http_url = getattr(args, "openclaw_http_url", "http://localhost:18789")
        mock_port = getattr(args, "mock_server_port", 18888)
        llm_mode = getattr(args, "mock_llm_mode", False)
        print(f"Mock server: enabled (port {mock_port}, {'LLM' if llm_mode else 'script'} mode)")
        # Script will be resolved per test case; start with no script loaded
        mock_server = MockServer(
            openclaw_url=openclaw_http_url,
            port=mock_port,
            llm_mode=llm_mode,
        )
        mock_server.start()
        mock_server.wait_ready()
        print("Mock server: ready")

    # Judge
    judge_provider = args.judge_provider or "openai"
    judge_model = args.judge_model or "gpt-4o-mini"
    if judge_provider == "openai":
        from src.lnl.judge import OpenAIJudge
        judge = OpenAIJudge(model=judge_model)
    else:
        from src.lnl.judge import AnthropicJudge
        judge = AnthropicJudge(model=judge_model)

    from src.lnl.benchmark import BenchmarkHarness
    harness = BenchmarkHarness(judge=judge)

    args.output.parent.mkdir(parents=True, exist_ok=True)

    all_tc_results: list[TestCaseResult] = []

    with open(args.output, "w") as f:
        for tc in test_cases:
            # Load mock config for this test case (if mock server is active)
            if mock_server is not None:
                tc_mock_script = resolve_mock_configs(tc)
                mock_server._state.mock_script = tc_mock_script
                tc_orchestration = resolve_orchestration(tc)
                mock_server._state.orchestration_script = tc_orchestration

            for run_idx in range(args.runs):
                label = f"{tc.id} run={run_idx}"
                print(f"  Evaluating {label} ...", end=" ", flush=True)
                try:
                    event_results, mod_results = execute_test_case(
                        tc, agent, harness, timeout_s, mock_server=mock_server
                    )
                    pass_rate = (
                        sum(1 for e in event_results if e.passed) / len(event_results)
                        if event_results else 1.0
                    )
                    tc_result = TestCaseResult(
                        tc_id=tc.id,
                        name=tc.name,
                        domain=tc.domain,
                        run_index=run_idx,
                        events=event_results,
                        modifications=mod_results,
                        pass_rate=pass_rate,
                    )
                    f.write(tc_result.model_dump_json() + "\n")
                    f.flush()
                    all_tc_results.append(tc_result)
                    print(f"pass_rate={pass_rate:.2f}")
                except Exception as e:
                    print(f"FAILED: {e}", file=sys.stderr)

    # Write summary
    summary = _compute_summary(all_tc_results)
    with open(args.output, "a") as f:
        f.write(summary.model_dump_json() + "\n")

    if mock_server is not None:
        mock_server.stop()

    print()
    print(f"Complete. Output: {args.output}")
    print(f"Mean pass rate: {summary.mean_pass_rate:.3f}  std: {summary.pass_rate_std:.3f}")
    return args.output


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Baseline evaluation: OpenClaw single-agent comparison for LNL experiment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.data.evaluate_baseline -i outputs/data/zapier/20260322_010211/test_cases.jsonl
  python -m src.data.evaluate_baseline -i test_cases.jsonl --runs 3 --agent-id my-bot
""",
    )
    parser.add_argument(
        "--input", "-i",
        type=Path,
        required=True,
        help="Path to test cases JSONL file",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Output JSONL path (default: {stem}_baseline.jsonl next to input)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Number of runs per test case (default: 1)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help="Wall-clock timeout per test case run (default: 120)",
    )
    parser.add_argument(
        "--agent-id",
        default="lnl-baseline",
        help="OpenClaw agent ID to use (default: lnl-baseline)",
    )
    parser.add_argument(
        "--gateway-url",
        default=None,
        help="OpenClaw gateway WebSocket URL (default: auto-detect localhost:18789)",
    )
    parser.add_argument(
        "--judge-provider",
        choices=["openai", "anthropic"],
        default="openai",
        help="LLM provider for the judge (default: openai)",
    )
    parser.add_argument(
        "--judge-model",
        default="gpt-4o-mini",
        help="Model for the judge (default: gpt-4o-mini)",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=None,
        help="Process only the first N test cases",
    )
    parser.add_argument(
        "--mock-server",
        action="store_true",
        default=False,
        help="Enable mock external system integration (Slack, Email, Jira, etc.)",
    )
    parser.add_argument(
        "--mock-server-port",
        type=int,
        default=18888,
        help="Port for the mock server (default: 18888)",
    )
    parser.add_argument(
        "--mock-llm-mode",
        action="store_true",
        default=False,
        help="Use LLM to generate mock responses instead of YAML scripts",
    )
    parser.add_argument(
        "--openclaw-http-url",
        default="http://localhost:18789",
        help="OpenClaw gateway HTTP URL for callback injection (default: http://localhost:18789)",
    )
    return parser


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
