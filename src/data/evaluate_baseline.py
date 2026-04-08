"""
Baseline evaluation runner — OpenClaw multi-agent comparison for the LNL experiment.

Runs the same TestCases as evaluate.py but uses OpenClaw agents — one agent
per LNL-object — instead of the LNL runtime. Each object is exported as a
separate OpenClaw agent workspace. Steps, modifications, and events are routed
to the correct agent by recipient/target field.

Requires:
    - OpenClaw daemon running (openclaw gateway status)
    - openclaw-sdk installed (pip install openclaw-sdk)

Usage:
    python -m src.data.evaluate_baseline \\
        -i outputs/data/zapier/20260322_010211/test_cases.jsonl \\
        --runs 3
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

from src.data.schema import (
    EvalSummary,
    EventResult,
    ModificationResult,
    ObjectDef,
    TestCase,
    TestCaseResult,
)
from src.data.mock_server import MockServer, merge_tc_mock_tools, resolve_mock_configs
from src.data.utils import (
    infer_provider,
    load_jsonl,
)
from src.lnl.openclaw_export import (
    export_workflow_from_objects,
    reset_agent_state,
)

# ── OpenClaw agent configuration ─────────────────────────────────────────────

_OAUTH_PROVIDER_MAP = {
    "openai": "openai-codex",
}


def _resolve_openclaw_provider(agent_id: str, provider: str) -> str:
    """Detect which provider the agent is authenticated for."""
    oauth_alias = _OAUTH_PROVIDER_MAP.get(provider)
    if oauth_alias is None:
        return provider

    auth_path = (
        Path.home()
        / ".openclaw"
        / "agents"
        / agent_id
        / "agent"
        / "auth-profiles.json"
    )
    if not auth_path.exists():
        return provider

    import json as _json
    try:
        data = _json.loads(auth_path.read_text())
    except Exception:
        return provider

    configured = {p.split(":")[0] for p in data.get("profiles", {}).keys()}
    if oauth_alias in configured and provider not in configured:
        return oauth_alias
    return provider


async def _configure_openclaw_agents(
    objects: list[ObjectDef],
    provider: str,
    model: str,
    openclaw_home: Path,
    gateway_url: Optional[str],
) -> str:
    """Register all objects as agents in the OpenClaw daemon config.

    Returns the effective provider used (may switch to OAuth variant).
    """
    import json
    import json5
    from openclaw_sdk import OpenClawClient

    # Resolve provider from the first object (all share same auth)
    effective_provider = _resolve_openclaw_provider(
        objects[0].object_id if objects else "main", provider
    )

    connect_kwargs: dict[str, Any] = {}
    if gateway_url:
        connect_kwargs["gateway_ws_url"] = gateway_url

    object_ids = {obj.object_id for obj in objects}

    async with await OpenClawClient.connect(**connect_kwargs) as client:
        result = await client.config_mgr.get()
        raw = result.get("raw", "{}")
        base_hash = result.get("hash")
        config = json5.loads(raw)
        agents_cfg = config.setdefault("agents", {})
        lst = agents_cfg.setdefault("list", [])

        # Remove old entries for these object IDs (will be replaced below)
        lst = [a for a in lst if a.get("id") not in object_ids]

        for obj in objects:
            lst.append({
                "id": obj.object_id,
                "name": obj.object_id.replace("-", " ").title(),
                "workspace": str(openclaw_home / f"workspace-{obj.object_id}"),
                "agentDir": str(openclaw_home / "agents" / obj.object_id / "agent"),
                "model": {"primary": f"{effective_provider}/{model}"},
            })

        agents_cfg["list"] = lst
        config["tools"] = {
            "agentToAgent": {
                "enabled": True,
                "allow": list(object_ids),
            }
        }

        await client.config_mgr.patch(json.dumps(config, indent=2), base_hash=base_hash)

    return effective_provider


# ── Timestamp parsing ────────────────────────────────────────────────────────

def parse_when(when: str) -> int:
    """Convert 'W02-1T10:30' → ordinal minutes for sorting."""
    week_part, time_part = when.split("T")
    w, d = week_part.lstrip("W").split("-")
    h, m = time_part.split(":")
    return (int(w) * 7 + int(d)) * 1440 + int(h) * 60 + int(m)


# ── Evidence gathering ───────────────────────────────────────────────────────

def gather_evidence(
    content: str,
    tool_calls: Optional[list[dict]] = None,
    state_content: str = "",
) -> str:
    """Collect observable evidence from an OpenClaw agent response.

    Args:
        content: The agent's text response (plain text from OpenClaw).
        tool_calls: Accumulated mock tool call log for this run window.
        state_content: Contents of the agent's state.md after the message,
                       reflecting decisions the agent persisted during execution.
    """
    parts: list[str] = []

    if content.strip():
        parts.append(f"Response:\n{content.strip()}")

    if state_content.strip():
        parts.append(f"Updated state:\n{state_content.strip()}")

    if tool_calls:
        lines = []
        for call in tool_calls:
            if call.get("is_callback"):
                lines.append(f"  - [{call['method']}] {call['result']}")
            else:
                lines.append(f"  - {call['method']}({json.dumps(call.get('args', {}))}) → {call['result']}")
        parts.append("Tool calls:\n" + "\n".join(lines))

    return "\n\n".join(parts) if parts else "(no observable output)"


# ── OpenClaw agent wrapper ───────────────────────────────────────────────────

class OpenClawAgent:
    """Wraps the OpenClaw SDK for single-message execution against a named agent."""

    def __init__(self, agent_id: str, gateway_url: Optional[str] = None):
        self._agent_id = agent_id
        self._gateway_url = gateway_url
        self._session_counter = 0

    async def _execute_single_message(self, content: str) -> dict[str, Any]:
        """Send one message to this agent in a fresh session. Returns {content, latency_ms}."""
        from openclaw_sdk import OpenClawClient

        connect_kwargs: dict[str, Any] = {}
        if self._gateway_url:
            connect_kwargs["gateway_ws_url"] = self._gateway_url

        self._session_counter += 1
        session_name = f"eval-{self._agent_id}-{self._session_counter}"

        t0 = time.time()
        async with await OpenClawClient.connect(**connect_kwargs) as client:
            agent = client.get_agent(self._agent_id, session_name=session_name)
            result = await agent.execute(content)
        latency_ms = (time.time() - t0) * 1000

        return {
            "content": result.content if result.success else f"(error: {result.content})",
            "latency_ms": latency_ms,
        }

    def send_message(self, content: str) -> dict[str, Any]:
        """Synchronous wrapper: send one message to this agent."""
        return asyncio.run(self._execute_single_message(content))


# ── Prior context ────────────────────────────────────────────────────────────

def _read_prior_context(tc: TestCase, openclaw_home: Path) -> str:
    """Read state.md for all objects as prior-state context for the judge.

    Equivalent to evaluate.py's _format_prior_state(rt): gives the judge a
    snapshot of what each agent knew before the current event fired.
    """
    lines = ["=== PRIOR STATE ==="]
    for obj in tc.objects:
        ws = openclaw_home / f"workspace-{obj.object_id}"
        state_file = ws / "state.md"
        if state_file.exists():
            text = state_file.read_text().strip()
            if text:
                lines.append(f"[{obj.object_id}]:\n{text}")
    return "\n\n".join(lines)


# ── Tool trigger matching ────────────────────────────────────────────────────

def _tool_call_matches(match: dict[str, str], args: dict) -> bool:
    """Return True if all match conditions pass (empty dict always passes)."""
    for key, pattern in match.items():
        if not re.search(pattern, str(args.get(key, "")), re.IGNORECASE):
            return False
    return True


# ── Core execution ───────────────────────────────────────────────────────────

def _execute_test_case_inner(
    tc: TestCase,
    agents: dict[str, OpenClawAgent],
    openclaw_home: Path,
    harness,
    mock_server: Optional["MockServer"] = None,
    verbose: bool = False,
    steps_only: bool = False,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Run a single TestCase against per-object OpenClaw agents and return results.

    Routes each step/event to the correct agent by recipient/target field.
    Resets each agent's state.md to its initial state before processing.
    """
    # Reset state.md for all objects before this run
    for obj in tc.objects:
        reset_agent_state(obj.object_id, obj.state_description, openclaw_home)

    # Build trigger map: event_id → list of triggered events (test-case schema)
    trigger_map: dict[str, list[Any]] = {}
    for evt in tc.events:
        if evt.triggered_by:
            trigger_map.setdefault(evt.triggered_by, []).append(evt)

    # Build tool trigger map: tool_name → MockToolDef, for in-process trigger dispatch.
    # Mirrors MockInProcessExecutor's trigger mechanism in evaluate.py:
    # when a tool call fires and its match conditions pass, the evaluator synchronously
    # sends trigger.message_template to trigger.target_object_id — no /hooks/wake needed.
    tool_trigger_map = {t.tool_name: t for t in tc.mock_tools if t.triggers}

    # Build ordered message list
    messages: list[dict[str, Any]] = []

    for i, step in enumerate(tc.steps):
        messages.append({
            "kind": "step",
            "index": i,
            "target": step.target,
            "content": f"[Event from {step.source}]: {step.text}",
            "expect": step.expect,
        })

    timeline: list[tuple[int, str, Any]] = []
    for mod in tc.modifications:
        timeline.append((parse_when(mod.when), "mod", mod))
    for evt in tc.events:
        if evt.triggered_by is None:
            timeline.append((parse_when(evt.when), "event", evt))
    timeline.sort(key=lambda x: x[0])

    for _, kind, item in timeline:
        if kind == "mod":
            messages.append({
                "kind": "mod",
                "item": item,
                "target": item.target,
                "content": f"[Administrative instruction at {item.when}]: {item.intent}",
            })
        else:
            messages.append({
                "kind": "event",
                "item": item,
                "target": item.recipient,
                "content": f"[Event from {item.source} at {item.when}]: {item.input}",
            })
            for triggered in trigger_map.get(item.id, []):
                messages.append({
                    "kind": "event",
                    "item": triggered,
                    "target": triggered.recipient,
                    "content": f"[Event from {triggered.source} (triggered by {item.id})]: {triggered.input}",
                })

    event_results: list[EventResult] = []
    mod_results: list[ModificationResult] = []
    run_mock_log: list[dict] = []  # accumulated tool call evidence across all messages this run
    prior_context: str = ""

    for msg in messages:
        # After steps phase, return early if steps_only mode
        if steps_only and msg["kind"] != "step":
            break

        target_id = msg["target"]
        agent = agents.get(target_id)
        if agent is None:
            if verbose:
                print(f"  Warning: no agent for target {target_id!r}, skipping")
            continue

        # Configure mock server with the correct session key before each message so
        # callbacks and orchestration reactions can wake the right OpenClaw session.
        if mock_server:
            next_session_key = f"eval-{target_id}-{agent._session_counter + 1}"
            mock_server.configure(next_session_key)

        result = agent.send_message(msg["content"])
        content = result["content"]
        latency_ms = result["latency_ms"]

        if mock_server:
            time.sleep(0.3)
            new_calls = mock_server.get_log()
            run_mock_log.extend(new_calls)

            # Synchronously dispatch events triggered by tool calls in this message.
            # This mirrors MockInProcessExecutor.execute() in evaluate.py: instead of
            # relying on /hooks/wake (which fires after the session closes), we dispatch
            # the triggered message directly to the correct agent right now.
            for call in new_calls:
                if call.get("is_callback") or call.get("is_orchestration"):
                    continue
                tool_def = tool_trigger_map.get(call["method"])
                if tool_def is None:
                    continue
                if not _tool_call_matches(tool_def.match, call.get("args", {})):
                    continue
                for trigger in tool_def.triggers:
                    tgt_agent = agents.get(trigger.target_object_id)
                    if tgt_agent is None:
                        if verbose:
                            print(f"  Warning: trigger target {trigger.target_object_id!r} not in agents, skipping")
                        continue
                    try:
                        trigger_msg = trigger.message_template.format(**call.get("args", {}))
                    except KeyError:
                        trigger_msg = trigger.message_template
                    triggered_content = f"[Event from {trigger.source}]: {trigger_msg}"
                    if verbose:
                        print(f"  [TRIGGER→{trigger.target_object_id}] {triggered_content[:120]}")
                    next_key = f"eval-{trigger.target_object_id}-{tgt_agent._session_counter + 1}"
                    mock_server.configure(next_key)
                    tgt_agent.send_message(triggered_content)
                    time.sleep(0.3)
                    run_mock_log.extend(mock_server.get_log())

        # Read the target agent's post-execution state.md — reflects decisions the
        # agent persisted during this session (e.g. "approved", updated queue, etc.)
        target_state_file = openclaw_home / f"workspace-{target_id}" / "state.md"
        post_event_state = target_state_file.read_text().strip() if target_state_file.exists() else ""

        if verbose:
            kind_label = {"step": "STEP", "mod": "MOD", "event": "EVENT"}.get(msg["kind"], "?")
            print(f"\n{'─'*60}")
            print(f"[{kind_label}→{target_id}] {msg['content'][:120]}")
            print(f"  Agent: {content[:300]}")
            if post_event_state:
                print(f"  State: {post_event_state[:200]}")

        if msg["kind"] == "step":
            expect = msg["expect"]
            if expect is not None:
                evidence = gather_evidence(
                    content,
                    tool_calls=run_mock_log if mock_server else None,
                    state_content=post_event_state,
                )
                passed, reasoning, _votes = harness.evaluate_assertion(expect.action, evidence, prior_context)
                if verbose:
                    print(f"  Expected: {expect.action}")
                    print(f"  {'✓ PASS' if passed else '✗ FAIL'}: {reasoning[:200]}")
                event_results.append(EventResult(
                    event_id=f"S{msg['index']+1:03d}",
                    passed=passed,
                    reasoning=reasoning,
                    expected=expect.action,
                    latency_ms=latency_ms,
                ))
            prior_context = _read_prior_context(tc, openclaw_home)

        elif msg["kind"] == "mod":
            mod_results.append(ModificationResult(
                mod_id=msg["item"].id,
                latency_ms=latency_ms,
            ))
            prior_context = _read_prior_context(tc, openclaw_home)

        else:  # event
            item = msg["item"]
            if item.expect is not None:
                evidence = gather_evidence(
                    content,
                    tool_calls=run_mock_log if mock_server else None,
                    state_content=post_event_state,
                )
                passed, reasoning, _votes = harness.evaluate_assertion(item.expect.action, evidence, prior_context)
                if verbose:
                    print(f"  Expected: {item.expect.action}")
                    print(f"  {'✓ PASS' if passed else '✗ FAIL'}: {reasoning[:200]}")
                event_results.append(EventResult(
                    event_id=item.id,
                    passed=passed,
                    reasoning=reasoning,
                    expected=item.expect.action,
                    latency_ms=latency_ms,
                ))
            prior_context = _read_prior_context(tc, openclaw_home)

    return event_results, mod_results


def execute_test_case(
    tc: TestCase,
    agents: dict[str, OpenClawAgent],
    openclaw_home: Path,
    harness,
    timeout_s: Optional[float] = None,
    mock_server: Optional[MockServer] = None,
    verbose: bool = False,
    steps_only: bool = False,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Run a single TestCase with an optional wall-clock timeout."""
    if timeout_s is None:
        return _execute_test_case_inner(tc, agents, openclaw_home, harness,
                                        mock_server=mock_server, verbose=verbose,
                                        steps_only=steps_only)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_execute_test_case_inner, tc, agents, openclaw_home,
                                 harness, mock_server, verbose, steps_only)
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            future.cancel()
            event_results = [
                EventResult(event_id=evt.id, passed=False, reasoning=f"Timeout after {timeout_s}s")
                for evt in tc.events
            ]
            mod_results = [ModificationResult(mod_id=mod.id) for mod in tc.modifications]
            return event_results, mod_results


# ── Output path ──────────────────────────────────────────────────────────────

def default_output_path(input_path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return input_path.parent / "runs" / f"{input_path.stem}_baseline_{ts}.jsonl"


# ── Summary ──────────────────────────────────────────────────────────────────

_STEP_EVENT_ID = re.compile(r"^S\d+$")


def _compute_summary(results: list[TestCaseResult]) -> EvalSummary:
    """Compute aggregate metrics across all test case results.

    Step events (id matching S\\d+) are deduplicated by sample_id: only the first
    TC variant per sample contributes step results to the summary. All TC variants
    contribute their modification and timeline event results.
    """
    all_mods = [m for r in results for m in r.modifications]
    total_runs = len(results)
    total_test_cases = len({r.tc_id for r in results})

    def mean(vals):
        return sum(vals) / len(vals) if vals else 0.0

    # Identify the first TC seen per sample_id — that TC's step results are canonical.
    first_tc_per_sample: dict[str, str] = {}
    for r in results:
        sid = r.sample_id or r.tc_id
        if sid not in first_tc_per_sample:
            first_tc_per_sample[sid] = r.tc_id
    base_tc_ids = set(first_tc_per_sample.values())

    all_events: list[EventResult] = []
    pass_rates: list[float] = []
    for r in results:
        is_base = r.tc_id in base_tc_ids
        effective = [
            e for e in r.events
            if is_base or not _STEP_EVENT_ID.match(e.event_id)
        ]
        all_events.extend(effective)
        if effective:
            pass_rates.append(sum(1 for e in effective if e.passed) / len(effective))

    mean_pass_rate = mean(pass_rates) if pass_rates else 0.0

    by_tc: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.pass_rate is not None:
            by_tc[r.tc_id].append(r.pass_rate)
    per_tc_stds = [statistics.stdev(rates) for rates in by_tc.values() if len(rates) > 1]
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

    if getattr(args, "tc", None):
        selected: list[TestCase] = []
        for selector in args.tc:
            if selector.isdigit():
                idx = int(selector) - 1
                if idx < 0 or idx >= len(test_cases):
                    print(f"Error: --tc {selector} out of range", file=sys.stderr)
                    sys.exit(1)
                selected.append(test_cases[idx])
            else:
                matched = [tc for tc in test_cases if tc.id == selector]
                if not matched:
                    print(f"Error: --tc {selector!r} not found", file=sys.stderr)
                    sys.exit(1)
                selected.extend(matched)
        test_cases = selected
    elif args.limit:
        test_cases = test_cases[: args.limit]

    # When running steps-only, deduplicate by sample_id (same as evaluate.py)
    if getattr(args, "steps_only", False):
        seen_step_samples: set[str] = set()
        deduped: list[TestCase] = []
        for tc in test_cases:
            key = tc.sample_id or tc.id
            if key not in seen_step_samples:
                seen_step_samples.add(key)
                deduped.append(tc)
        if len(deduped) < len(test_cases):
            print(
                f"  Steps-only mode: deduplicating by sample_id "
                f"({len(test_cases)} → {len(deduped)} test cases)"
            )
        test_cases = deduped

    timeout_s: Optional[float] = getattr(args, "timeout", None)
    openclaw_home: Path = Path(getattr(args, "openclaw_home", "~/.openclaw")).expanduser()

    agent_model: Optional[str] = getattr(args, "model", None)
    agent_provider: Optional[str] = getattr(args, "provider", None)
    if agent_model and not agent_provider:
        agent_provider = infer_provider(agent_model)

    judge_model = args.judge_model or agent_model or "gpt-4o"
    judge_provider = args.judge_provider or infer_provider(judge_model)

    print(f"Loaded {len(test_cases)} test cases from {args.input}")
    print(f"Mode: baseline (OpenClaw multi-agent)")
    print(f"OpenClaw home: {openclaw_home}")
    if agent_model:
        print(f"Agent model: {agent_provider}/{agent_model}")
    print(f"Judge: {judge_provider}/{judge_model}")
    print(f"Runs per test case: {args.runs}")
    print(f"Timeout per run: {timeout_s}s" if timeout_s else "Timeout: none")
    if args.gateway_url:
        print(f"Gateway: {args.gateway_url}")
    print()

    # Build MockServer (optional)
    mock_server: Optional[MockServer] = None
    if getattr(args, "mock_server", False):
        openclaw_http_url = getattr(args, "openclaw_http_url", "http://localhost:18789")
        mock_port = getattr(args, "mock_server_port", 18888)
        llm_mode = getattr(args, "mock_llm_mode", False)
        print(f"Mock server: enabled (port {mock_port}, {'LLM' if llm_mode else 'script'} mode)")
        mock_server = MockServer(
            openclaw_url=openclaw_http_url,
            port=mock_port,
            llm_mode=llm_mode,
        )
        mock_server.start()
        mock_server.wait_ready()
        print("Mock server: ready")


    if judge_provider == "openai":
        from src.lnl.judge import OpenAIJudge
        judge = OpenAIJudge(model=judge_model)
    elif judge_provider == "google":
        from src.lnl.judge import GeminiJudge
        judge = GeminiJudge(model=judge_model)
    else:
        from src.lnl.judge import AnthropicJudge
        judge = AnthropicJudge(model=judge_model)

    from src.lnl.benchmark import BenchmarkHarness
    harness = BenchmarkHarness(judge=judge)

    args.output.parent.mkdir(parents=True, exist_ok=True)

    all_tc_results: list[TestCaseResult] = []
    seen_samples: set[str] = set()
    effective_provider = agent_provider or "openai"

    with open(args.output, "w") as f:
        for tc_idx, tc in enumerate(test_cases):
            # Export + register agents when object structure is new (dedup by sample_id)
            if tc.sample_id not in seen_samples:
                print(f"  Exporting agents for sample {tc.sample_id!r} "
                      f"({len(tc.objects)} objects: {[o.object_id for o in tc.objects]})")
                export_workflow_from_objects(tc.objects, openclaw_home, force=True)

                if agent_model or agent_provider:
                    effective_provider = asyncio.run(_configure_openclaw_agents(
                        tc.objects,
                        agent_provider or "openai",
                        agent_model or "gpt-4o",
                        openclaw_home,
                        args.gateway_url,
                    ))
                    print(f"  Configured agents: {effective_provider}/{agent_model or 'gpt-4o'}")

                seen_samples.add(tc.sample_id)

            # Build per-object agents dict (fresh per TC so session counters reset)
            agents = {
                obj.object_id: OpenClawAgent(obj.object_id, gateway_url=args.gateway_url)
                for obj in tc.objects
            }

            if mock_server is not None:
                tc_mock_script = resolve_mock_configs(tc)
                tc_mock_script = merge_tc_mock_tools(tc_mock_script, tc.mock_tools)
                mock_server._state.mock_script = tc_mock_script

            for run_idx in range(args.runs):
                label = f"{tc.id} run={run_idx}"
                print(f"  Evaluating {label} ...", end=" ", flush=True)
                try:
                    event_results, mod_results = execute_test_case(
                        tc, agents, openclaw_home, harness, timeout_s,
                        mock_server=mock_server,
                        verbose=getattr(args, "verbose", False),
                        steps_only=getattr(args, "steps_only", False),
                    )
                    pass_rate = (
                        sum(1 for e in event_results if e.passed) / len(event_results)
                        if event_results else None
                    )
                    tc_result = TestCaseResult(
                        tc_id=tc.id,
                        sample_id=tc.sample_id,
                        tc_index=tc_idx,
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
                    rate_str = f"{pass_rate:.2f}" if pass_rate is not None else "N/A"
                    print(f"pass_rate={rate_str}")
                except Exception as e:
                    print(f"FAILED: {e}", file=sys.stderr)

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
        description="Baseline evaluation: OpenClaw multi-agent comparison for LNL experiment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.data.evaluate_baseline -i outputs/data/zapier/20260322_010211/test_cases.jsonl
  python -m src.data.evaluate_baseline -i test_cases.jsonl --runs 3 --model gpt-4o
""",
    )
    parser.add_argument("--input", "-i", type=Path, required=True,
                        help="Path to test cases JSONL file")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Output JSONL path (default: {stem}_baseline.jsonl next to input)")
    parser.add_argument("--runs", type=int, default=1,
                        help="Number of runs per test case (default: 1)")
    parser.add_argument("--timeout", type=float, default=120.0, metavar="SECONDS",
                        help="Wall-clock timeout per test case run (default: 120)")
    parser.add_argument("--model", "-m", default=None, metavar="MODEL",
                        help="Model for OpenClaw agents (e.g. gpt-4o). Provider inferred from name.")
    parser.add_argument("--provider", "-p", choices=["openai", "anthropic", "google"], default=None,
                        help="LLM provider (overrides inference from --model)")
    parser.add_argument("--verbose", "-v", action="store_true", default=False,
                        help="Print each message and agent response with per-event pass/fail")
    parser.add_argument("--gateway-url", default=None,
                        help="OpenClaw gateway WebSocket URL (default: auto-detect localhost:18789)")
    parser.add_argument("--openclaw-home", default="~/.openclaw",
                        help="Root OpenClaw directory for agent workspaces (default: ~/.openclaw)")
    parser.add_argument("--judge-model", default=None,
                        help="Model for LLM-as-judge (default: same as --model)")
    parser.add_argument("--judge-provider", choices=["openai", "anthropic", "google"], default=None,
                        help="Provider for judge model (inferred from --judge-model if not specified)")
    parser.add_argument("--limit", "-n", type=int, default=None,
                        help="Process only the first N test cases")
    parser.add_argument("--tc", nargs="+", metavar="INDEX_OR_ID",
                        help="Run specific test cases by 1-based index or ID. Overrides --limit.")
    parser.add_argument("--mock-server", action="store_true", default=False,
                        help="Enable mock external system integration (Slack, Email, Jira, etc.)")
    parser.add_argument("--mock-server-port", type=int, default=18888,
                        help="Port for the mock server (default: 18888)")
    parser.add_argument("--mock-llm-mode", action="store_true", default=False,
                        help="Use LLM to generate mock responses instead of YAML scripts")
    parser.add_argument("--steps-only", action="store_true", default=False,
                        help="Run only the steps phase (no modifications/events). "
                             "Deduplicates by sample_id.")
    parser.add_argument("--openclaw-http-url", default="http://localhost:18789",
                        help="OpenClaw gateway HTTP URL for callback injection (default: http://localhost:18789)")
    return parser


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
