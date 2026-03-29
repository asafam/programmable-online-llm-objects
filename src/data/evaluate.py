"""
Evaluation runner — Stage 3 of the data pipeline.

Executes TestCases against the LNL runtime, judges outcomes with an LLM, and
reports correctness and cost metrics.

Usage:
    python -m src.data.evaluate \\
        -i outputs/data/zapier/20260322_120000/test_cases.jsonl \\
        --runs 3 \\
        --model gpt-4o --judge-model claude-sonnet-4-6
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

from src.data.schema import (
    EvalSummary,
    EventResult,
    MockConfig,
    MockToolDef,
    ModificationResult,
    TestCase,
    TestCaseResult,
    to_lnl_definition,
)
from src.data.utils import (
    add_common_args,
    infer_provider,
    load_jsonl,
    print_run_info,
)


# ── Mock config loading ────────────────────────────────────────────────────────

def load_mock_config(path: Path) -> MockConfig:
    """Load a MockConfig from a YAML file."""
    data = yaml.safe_load(path.read_text())
    return MockConfig.model_validate(data)


def merge_mock_tools(
    global_tools: list[MockToolDef],
    tc_tools: list[MockToolDef],
) -> list[MockToolDef]:
    """Merge two tool lists. Right side wins on tool_name collision."""
    merged = {t.tool_name: t for t in global_tools}
    merged.update({t.tool_name: t for t in tc_tools})
    return list(merged.values())


def _derive_tools_from_skills(tc: "TestCase") -> list[MockToolDef]:
    """Derive MockToolDef entries from skills declared on each object.

    Skills expose internal capabilities as callable tools so they appear in
    the {tools} system-prompt section. The LLM can invoke them via tool_calls;
    if no scripted mock overrides them, PassthroughExecutor handles the call
    and logs it for judge evidence.

    This is the lowest-priority layer — per-event triggered_by derivation,
    --mock-config files, and tc.mock_tools all override these entries.
    """
    seen: set[str] = set()
    result: list[MockToolDef] = []
    for obj in tc.objects:
        for skill in obj.skills:
            if skill in seen:
                continue
            seen.add(skill)
            label = skill.replace("-", " ").replace("_", " ")
            result.append(MockToolDef(
                tool_name=skill,
                description=f"Perform {label}.",
                arguments_schema={"type": "object", "additionalProperties": True},
                response_template=f"[mock] {skill} completed.",
            ))
    return result


def _build_trigger_map(tc: "TestCase") -> "dict[str, list]":
    """Build a map from event_id → list of events triggered by that event."""
    trigger_map: dict[str, list] = {}
    for event in tc.events:
        if event.triggered_by:
            trigger_map.setdefault(event.triggered_by, []).append(event)
    return trigger_map


# ── Timestamp parsing ──────────────────────────────────────────────────────────

def parse_when(when: str) -> int:
    """Convert 'W02-1T10:30' → ordinal minutes for sorting."""
    week_part, time_part = when.split("T")
    w, d = week_part.lstrip("W").split("-")
    h, m = time_part.split(":")
    return (int(w) * 7 + int(d)) * 1440 + int(h) * 60 + int(m)


# ── Evidence gathering ─────────────────────────────────────────────────────────

def gather_evidence(
    rt,
    results,
    recipient: str,
    bus_messages: "list | None" = None,
    tool_calls: "list[list[dict]] | None" = None,
) -> str:
    """Collect observable evidence after an event for the LLM judge.

    Evidence is structured in two sections:
    - THIS EVENT: everything that happened during this invocation —
      tool calls made (primary), bus messages exchanged, external actions,
      and replies. Tool calls are authoritative; external_actions are legacy.
    - OBJECT STATES: each object's updated_state after the event
      (may be empty; use THIS EVENT as authoritative evidence).
    """
    this_event_parts: list[str] = []

    # Tool calls (primary evidence when mock tools are in use)
    if tool_calls:
        flat = [entry for per_ex in tool_calls for entry in per_ex]
        if flat:
            lines = []
            for entry in flat:
                idx = entry.get("call_index", "?")
                line = f"  [{entry['tool']}] call#{idx} {json.dumps(entry['arguments'])}"
                if "response" in entry:
                    line += f"\n    ← {entry['response'][:100]}"
                for t in entry.get("triggered", []):
                    line += f"\n    → dispatched to [{t['target']}]: {t['message'][:120]}"
                lines.append(line)
            this_event_parts.append("Tool calls:\n" + "\n".join(lines))

    # Bus messages: the full message flow visible in --debug-messages
    if bus_messages:
        lines = []
        for ml in bus_messages:
            msg = ml.message
            arrow = "↩" if msg.type.value == "reply" else "→"
            sender = f"[{msg.sender}]" if msg.type.value == "event" else msg.sender
            lines.append(f"  {sender} {arrow} {msg.recipient} ({msg.type.value}): {msg.content[:200]}")
        this_event_parts.append("Message bus activity:\n" + "\n".join(lines))

    # External actions declared by objects (email.send, slack.send_message, etc.)
    ext_lines = []
    for r in results:
        for ea in getattr(r, "external_actions", []):
            params_str = json.dumps(ea.params) if ea.params else "{}"
            ext_lines.append(f"  [{r.object_id}] {ea.system}.{ea.action}: {ea.content[:300]} params={params_str}")
    if ext_lines:
        this_event_parts.append("External actions:\n" + "\n".join(ext_lines))

    # Replies from the chain triggered by this event
    replies = [r for r in results if r.reply and str(r.reply).strip()]
    if replies:
        this_event_parts.append("Replies:\n" + "\n".join(f"  [{r.object_id}]: {r.reply}" for r in replies))

    # Object states after this event (what each object recorded/knows)
    state_parts: list[str] = []
    for obj_id, obj in rt._bus.objects.items():
        state = obj.state
        if isinstance(state, dict):
            state_str = json.dumps(state, indent=2) if state else "(empty)"
        else:
            state_str = str(state).strip() or "(empty)"
        state_parts.append(f"  [{obj_id}]:\n{state_str}")

    sections: list[str] = []
    if this_event_parts:
        sections.append("=== THIS EVENT ===\n" + "\n\n".join(this_event_parts))
    if state_parts:
        sections.append("=== OBJECT STATES ===\n" + "\n\n".join(state_parts))

    return "\n\n".join(sections) if sections else "(no observable state)"


# ── Core execution ─────────────────────────────────────────────────────────────

def _print_message(msg) -> None:
    """Print a message exchange between LLM-objects."""
    arrow = "↩" if msg.type.value == "reply" else "→"
    content = msg.content[:120].replace("\n", " ")
    # Mark external event sources clearly to distinguish from LLM-object IDs
    sender = f"[{msg.sender}]" if msg.type.value == "event" else msg.sender
    print(f"      {sender} {arrow} {msg.recipient} ({msg.type.value}): {content}")


def _execute_test_case_inner(
    tc: TestCase,
    brain,
    harness,
    debug_messages: bool = False,
    timeout_s: Optional[float] = None,
    steps_only: bool = False,
    max_chain_depth: int = 20,
    global_mock_tools: "list[MockToolDef] | None" = None,
    progress_callback=None,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Run a single TestCase and return event + modification results."""
    from src.lnl.gateway import EventGateway
    from src.lnl.runtime import Runtime
    from src.lnl.tools import CodeExecutor, MockInProcessExecutor, PassthroughExecutor, ToolRegistry

    # 1. Build ToolRegistry — two-layer priority merge (lowest → highest):
    #    --mock-config global < tc.mock_tools
    #
    # Skills are intentionally NOT included here — they are internal computations
    # (the generate_samples prompt marks them as "purely internal, no external system
    # calls"). Registering them as tools would tell the LLM to call them externally.
    # Triggered events are dispatched directly by the harness (no mock tool needed).
    final_mock_tools = merge_mock_tools(global_mock_tools or [], tc.mock_tools)

    # Only enable the tool machinery (and LLM_RESPONSE_SCHEMA_WITH_TOOLS) when there
    # are actual mock tools to wire up. Without mock tools, pass tool_registry=None so
    # objects use LLM_RESPONSE_SCHEMA (no tool_calls field) and the LLM won't try to
    # call peer objects as tools via the PassthroughExecutor fallback.
    mock_executors: list = []
    passthrough = PassthroughExecutor()
    tool_registry: "ToolRegistry | None" = None

    if final_mock_tools:
        tool_registry = ToolRegistry()
        tool_registry.register("execute_code", CodeExecutor())
        tool_registry.register_fallback(passthrough)
        for mock_tool in final_mock_tools:
            executor = MockInProcessExecutor(mock_tool)
            tool_registry.register(mock_tool.tool_name, executor, spec=executor.spec)
            mock_executors.append(executor)

    # 2. Create Runtime and EventGateway — synchronous mode (no rt.start()).
    # The evaluator dispatches one event at a time and waits; the background
    # run-loop is not needed and would break _run_with_timeout (ThreadPoolExecutor
    # shutdown(wait=True) hangs when the foreground thread is blocked on item.done).
    rt = Runtime(brain, strict_peers=False, tool_registry=tool_registry, max_chain_depth=max_chain_depth)
    if debug_messages and progress_callback:
        rt.set_message_listener(lambda msg: (_print_message(msg), progress_callback(msg)))
    elif debug_messages:
        rt.set_message_listener(_print_message)
    elif progress_callback:
        rt.set_message_listener(progress_callback)
    gw = EventGateway(rt)

    for obj_def in tc.objects:
        rt.create_object(to_lnl_definition(obj_def))

    trigger_map = _build_trigger_map(tc)
    return _run_test_case_timeline(
        tc, rt, gw, harness,
        timeout_s=timeout_s,
        steps_only=steps_only,
        mock_executors=mock_executors + [passthrough] if final_mock_tools else [],
        trigger_map=trigger_map,
    )


def _run_with_timeout(fn, timeout_s: Optional[float]):
    """Run fn() with an optional per-step timeout. Returns (result, timed_out).

    Uses a daemon thread so an abandoned timed-out thread never blocks process exit.
    ThreadPoolExecutor is avoided because its shutdown(wait=True) hangs when the
    worker is blocked on I/O (e.g. waiting for an LLM API response).
    """
    if timeout_s is None:
        return fn(), False
    import threading
    result_box: list = [None]
    exc_box: list = [None]
    def worker():
        try:
            result_box[0] = fn()
        except Exception as e:
            exc_box[0] = e
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        return [], True
    if exc_box[0] is not None:
        raise exc_box[0]
    return result_box[0] or [], False


def _snapshot_logs(mock_executors: list) -> list[int]:
    return [len(ex.call_log) for ex in mock_executors]


def _new_tool_calls(mock_executors: list, snapshots: list[int]) -> list[list[dict]]:
    return [ex.call_log[s:] for ex, s in zip(mock_executors, snapshots)]


def _run_test_case_timeline(
    tc: TestCase,
    rt,
    gw,
    harness,
    timeout_s: Optional[float] = None,
    steps_only: bool = False,
    mock_executors: "list | None" = None,
    trigger_map: "dict[str, list] | None" = None,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Execute steps and timeline events against a live runtime."""
    event_results: list[EventResult] = []
    mod_results: list[ModificationResult] = []

    execs = mock_executors or []

    # 2. Run steps — initialize state and assert default (no-modification) behavior
    for i, step in enumerate(tc.steps):
        log_snapshot = len(rt.message_log)
        exec_snap = _snapshot_logs(execs)
        t0 = time.monotonic()
        step_payload = json.dumps({"system": step.source, "content": step.text})
        results, timed_out = _run_with_timeout(
            lambda s=step, p=step_payload: gw.dispatch(s.target, p, source=s.source), timeout_s,
        )
        latency_ms = (time.monotonic() - t0) * 1000

        if step.expect is not None:
            if timed_out:
                event_results.append(EventResult(
                    event_id=f"S{i+1:03d}",
                    passed=False,
                    reasoning=f"Timeout after {timeout_s}s",
                    expected=step.expect.action,
                    latency_ms=latency_ms,
                ))
            else:
                in_tok = sum(r.metrics.input_tokens for r in results if r.metrics)
                out_tok = sum(r.metrics.output_tokens for r in results if r.metrics)
                bus_msgs = rt.message_log[log_snapshot:]
                new_calls = _new_tool_calls(execs, exec_snap)
                evidence = gather_evidence(rt, results, step.target, bus_messages=bus_msgs, tool_calls=new_calls)
                condition = step.expect.action
                passed, reasoning = harness.evaluate_assertion(condition, evidence)
                event_results.append(EventResult(
                    event_id=f"S{i+1:03d}",
                    passed=passed,
                    reasoning=reasoning,
                    expected=condition,
                    evidence=evidence,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    latency_ms=latency_ms,
                ))

    if steps_only:
        return event_results, mod_results

    tmap = trigger_map or {}

    # 3. Build sorted timeline: tag each item with its type and when-ordinal.
    # Triggered events are dispatched as reactions after their parent fires —
    # exclude them from the main timeline ordering.
    timeline: list[tuple[int, str, object]] = []
    for mod in tc.modifications:
        timeline.append((parse_when(mod.when), "mod", mod))
    for evt in tc.events:
        if evt.triggered_by is None:
            timeline.append((parse_when(evt.when), "event", evt))
    timeline.sort(key=lambda x: x[0])

    def _dispatch_event(evt) -> tuple[list, bool, float]:
        """Dispatch a single event; return (results, timed_out, latency_ms)."""
        log_snap = len(rt.message_log)
        exec_s = _snapshot_logs(execs)
        t0 = time.monotonic()
        if evt.call_type == "send_event":
            payload = json.dumps({"system": evt.source, "content": evt.input})
            res, tout = _run_with_timeout(
                lambda e=evt, p=payload: gw.dispatch(e.recipient, p, source=e.source),
                timeout_s,
            )
        else:
            res, tout = _run_with_timeout(
                lambda e=evt: rt.send(e.recipient, e.input, sender=e.source),
                timeout_s,
            )
        lat = (time.monotonic() - t0) * 1000
        return res, tout, lat, log_snap, exec_s

    def _record_event_result(evt, res, timed_out, lat_ms, log_snap, exec_s):
        if timed_out:
            event_results.append(EventResult(
                event_id=evt.id,
                passed=False,
                reasoning=f"Timeout after {timeout_s}s",
                expected=evt.expect.action,
                latency_ms=lat_ms,
            ))
        else:
            in_tok = sum(r.metrics.input_tokens for r in res if r.metrics)
            out_tok = sum(r.metrics.output_tokens for r in res if r.metrics)
            bus_msgs = rt.message_log[log_snap:]
            new_calls = _new_tool_calls(execs, exec_s)
            evidence = gather_evidence(rt, res, evt.recipient, bus_messages=bus_msgs, tool_calls=new_calls)
            passed, reasoning = harness.evaluate_assertion(evt.expect.action, evidence)
            event_results.append(EventResult(
                event_id=evt.id,
                passed=passed,
                reasoning=reasoning,
                expected=evt.expect.action,
                evidence=evidence,
                input_tokens=in_tok,
                output_tokens=out_tok,
                latency_ms=lat_ms,
            ))

    for _, kind, item in timeline:
        if kind == "mod":
            t0 = time.monotonic()
            results, timed_out = _run_with_timeout(
                lambda it=item: rt.send(it.target, it.intent, sender=it.source),
                timeout_s,
            )
            latency_ms = (time.monotonic() - t0) * 1000
            if timed_out:
                mod_results.append(ModificationResult(mod_id=item.id, latency_ms=latency_ms))
            else:
                in_tok = sum(r.metrics.input_tokens for r in results if r.metrics)
                out_tok = sum(r.metrics.output_tokens for r in results if r.metrics)
                mod_results.append(ModificationResult(
                    mod_id=item.id,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    latency_ms=latency_ms,
                ))

        else:  # event
            res, tout, lat, log_snap, exec_s = _dispatch_event(item)
            _record_event_result(item, res, tout, lat, log_snap, exec_s)

            # Dispatch events triggered by this one (in declaration order)
            for triggered_evt in tmap.get(item.id, []):
                tr, tt, tl, tls, tes = _dispatch_event(triggered_evt)
                _record_event_result(triggered_evt, tr, tt, tl, tls, tes)

    return event_results, mod_results


def execute_test_case(
    tc: TestCase,
    brain,
    harness,
    timeout_s: Optional[float] = None,
    debug_messages: bool = False,
    steps_only: bool = False,
    max_chain_depth: int = 20,
    global_mock_tools: "list[MockToolDef] | None" = None,
    progress_callback=None,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Run a single TestCase with a per-event timeout (seconds).

    Each step, modification, and event gets its own timeout. If a single
    step times out, it is marked as failed and execution continues.

    Args:
        global_mock_tools: Shared mock tool definitions loaded from --mock-config files.
            Merged with tc.mock_tools; per-TestCase entries override shared ones.
        progress_callback: Optional callable(msg) invoked on every bus message delivery.
    """
    return _execute_test_case_inner(
        tc, brain, harness,
        debug_messages=debug_messages,
        timeout_s=timeout_s,
        steps_only=steps_only,
        max_chain_depth=max_chain_depth,
        global_mock_tools=global_mock_tools,
        progress_callback=progress_callback,
    )


# ── Output path ────────────────────────────────────────────────────────────────

def default_output_path(input_path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return input_path.parent / "runs" / f"{input_path.stem}_eval_{ts}.jsonl"


# ── Verbose output ────────────────────────────────────────────────────────────

def _print_event_result(ev, show_evidence: bool = False) -> None:
    """Print a single event result. Evidence is optional (verbose mode only)."""
    status = "PASS" if ev.passed else "FAIL"
    print(f"    [{status}] {ev.event_id}")
    print(f"      Expected: {ev.expected}")
    if show_evidence and ev.evidence:
        indented = ev.evidence.replace("\n", "\n        ")
        print(f"      Evidence: {indented}")
    print(f"      Judge:    {ev.reasoning}")
    print()


def _print_verbose(tc_result: TestCaseResult, show_evidence: bool = False) -> None:
    """Print per-event breakdown to console."""
    for ev in tc_result.events:
        _print_event_result(ev, show_evidence=show_evidence)


# ── Main runner ────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> Path:
    """Run evaluation. Returns the output path."""
    logging.basicConfig(level=logging.WARNING)

    if args.output is None:
        args.output = default_output_path(args.input)

    if args.provider is None:
        args.provider = infer_provider(args.model)

    if not args.input.exists():
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    test_cases = load_jsonl(args.input, TestCase)

    if getattr(args, "tc", None):
        # Select test cases by 1-based index or ID; preserve order of appearance in file
        selected: list[TestCase] = []
        for selector in args.tc:
            if selector.isdigit():
                idx = int(selector) - 1
                if idx < 0 or idx >= len(test_cases):
                    print(f"Error: --tc {selector} out of range (file has {len(test_cases)} test cases)", file=sys.stderr)
                    sys.exit(1)
                selected.append(test_cases[idx])
            else:
                matched = [tc for tc in test_cases if tc.id == selector]
                if not matched:
                    print(f"Error: --tc {selector!r} not found. Available IDs: {[tc.id for tc in test_cases[:5]]}...", file=sys.stderr)
                    sys.exit(1)
                selected.extend(matched)
        test_cases = selected
    elif args.limit:
        test_cases = test_cases[: args.limit]

    timeout_s: Optional[float] = getattr(args, "timeout", None)

    print(f"Loaded {len(test_cases)} test cases from {args.input}")
    # Use a dedicated judge model if specified; otherwise fall back to the object model.
    # SubstringJudge is insufficient for test cases with NL assertion conditions.
    judge_model = args.judge_model or args.model
    judge_provider = infer_provider(judge_model) if args.judge_model else args.provider
    extra_info = {
        "Runs per test case": str(args.runs),
        "Timeout per event": f"{timeout_s}s" if timeout_s else "none",
        "Judge": f"{judge_provider}/{judge_model}",
    }
    print_run_info(
        args.provider,
        args.model,
        getattr(args, "seed", None),
        extra_info,
    )

    # Build LNL brain (for objects) and judge (for assertions)
    def _make_brain(provider, model):
        if provider == "openai":
            from src.lnl.brain import OpenAIBrain
            return OpenAIBrain(model=model)
        else:
            from src.lnl.brain import AnthropicBrain
            return AnthropicBrain(model=model)

    def _make_judge(provider, model):
        if provider == "openai":
            from src.lnl.judge import OpenAIJudge
            return OpenAIJudge(model=model)
        else:
            from src.lnl.judge import AnthropicJudge
            return AnthropicJudge(model=model)

    brain = _make_brain(args.provider, args.model)
    judge = _make_judge(judge_provider, judge_model)

    from src.lnl.benchmark import BenchmarkHarness
    harness = BenchmarkHarness(brain=brain, judge=judge)

    # Load shared mock tool configs from --mock-config files
    global_mock_tools: list[MockToolDef] = []
    for mc_path in getattr(args, "mock_config", None) or []:
        mc = load_mock_config(mc_path)
        global_mock_tools.extend(mc.tools)
        print(f"  Loaded mock config: {mc_path} ({len(mc.tools)} tools)")

    args.output.parent.mkdir(parents=True, exist_ok=True)

    all_tc_results: list[TestCaseResult] = []
    total_runs = len(test_cases) * args.runs

    with open(args.output, "w") as f:
        with tqdm(total=total_runs, unit="run", desc="Evaluating") as pbar:
            for tc in test_cases:
                for run_idx in range(args.runs):
                    label = f"{tc.id} run={run_idx}" if args.runs > 1 else tc.id
                    pbar.set_postfix_str(label, refresh=True)
                    msg_count = [0]
                    tc_start = time.monotonic()

                    def _on_message(_msg, _label=label, _count=msg_count, _start=tc_start):
                        _count[0] += 1
                        elapsed = time.monotonic() - _start
                        pbar.set_postfix_str(
                            f"{_label}  msgs={_count[0]}  {elapsed:.0f}s", refresh=True
                        )

                    try:
                        event_results, mod_results = execute_test_case(
                            tc, brain, harness, timeout_s,
                            debug_messages=getattr(args, "debug_messages", False),
                            steps_only=getattr(args, "steps_only", False),
                            max_chain_depth=args.max_chain_depth,
                            global_mock_tools=global_mock_tools or None,
                            progress_callback=_on_message,
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
                        pbar.set_postfix_str(f"{label} pass={pass_rate:.2f}", refresh=True)
                        if args.verbose:
                            tqdm.write("")
                            _print_verbose(tc_result, show_evidence=True)
                    except Exception as e:
                        tqdm.write(f"FAILED {label}: {e}", file=sys.stderr)
                    pbar.update(1)

    # Write summary
    summary = _compute_summary(all_tc_results)
    with open(args.output, "a") as f:
        f.write(summary.model_dump_json() + "\n")

    print()
    print(f"Complete. Output: {args.output}")
    print(f"Mean pass rate: {summary.mean_pass_rate:.3f}  std: {summary.pass_rate_std:.3f}")
    return args.output


def _compute_summary(results: list[TestCaseResult]) -> EvalSummary:
    """Compute aggregate metrics across all test case results."""
    all_events = [e for r in results for e in r.events]
    all_mods = [m for r in results for m in r.modifications]
    total_runs = len(results)
    total_test_cases = len({r.tc_id for r in results})

    def mean(vals):
        return sum(vals) / len(vals) if vals else 0.0

    # Mean pass rate: average across all (tc, run) results
    pass_rates = [r.pass_rate for r in results]
    mean_pass_rate = mean(pass_rates)

    # Behavioral consistency: mean of per-TC std devs across runs.
    # Groups results by tc_id, computes std dev within each group, then averages.
    # Requires --runs > 1; returns 0.0 when each TC has only one run.
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


# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate test cases against the LNL runtime",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.data.evaluate -i outputs/data/zapier/20260322_120000/test_cases.jsonl
  python -m src.data.evaluate -i test_cases.jsonl --runs 3 --model claude-sonnet-4-6
  python -m src.data.evaluate -i test_cases.jsonl --model gpt-4o --judge-model claude-sonnet-4-6
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
        help="Output JSONL path (default: {stem}_eval.jsonl next to input)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Number of runs per test case for behavioral consistency (default: 1)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="Wall-clock timeout per step/event (not per test case); timed-out steps are marked failed and execution continues (default: 60)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Also print per-event evidence (expected/status/judge always shown)",
    )
    parser.add_argument(
        "--debug-messages",
        action="store_true",
        default=False,
        help="Print messages exchanged between LLM-objects during evaluation",
    )
    parser.add_argument(
        "--tc",
        nargs="+",
        default=None,
        metavar="N_OR_ID",
        help="Run specific test cases by 1-based index or ID (e.g. --tc 2 5 TC007). Overrides --limit.",
    )
    parser.add_argument(
        "--steps-only",
        action="store_true",
        default=False,
        help="Run only the steps (baseline behavior); skip modifications and events",
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help="Model for LLM-as-judge (default: same as --model). Provider is inferred from model name.",
    )
    parser.add_argument(
        "--judge-provider",
        choices=["openai", "anthropic"],
        default=None,
        help="Provider for judge model (inferred from judge-model if not specified)",
    )
    parser.add_argument(
        "--max-chain-depth",
        type=int,
        default=20,
        help="Max message chain depth per event (default: 20). Increase for workflows with many round-trips.",
    )
    parser.add_argument(
        "--mock-config",
        type=Path,
        action="append",
        default=None,
        metavar="YAML",
        help=(
            "YAML file with shared MockToolDef entries (can be specified multiple times). "
            "Loaded tools are merged with per-TestCase mock_tools; TestCase entries win on collision. "
            "Example: --mock-config config/mocks/lnl/email.yaml --mock-config config/mocks/lnl/slack.yaml"
        ),
    )
    add_common_args(parser)
    return parser


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
