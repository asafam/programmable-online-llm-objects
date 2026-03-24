"""
Evaluation runner — Stage 3 of the data pipeline.

Executes TestCases against the LNL runtime, judges outcomes with an LLM, and
reports correctness and cost metrics.

Usage:
    python -m src.data.evaluate \\
        -i outputs/data/zapier/20260322_120000/test_cases.jsonl \\
        --runs 3 \\
        --model claude-sonnet-4-6
"""
from __future__ import annotations

import argparse
import concurrent.futures
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from src.data.schema import (
    EvalSummary,
    EventResult,
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


# ── Timestamp parsing ──────────────────────────────────────────────────────────

def parse_when(when: str) -> int:
    """Convert 'W02-1T10:30' → ordinal minutes for sorting."""
    week_part, time_part = when.split("T")
    w, d = week_part.lstrip("W").split("-")
    h, m = time_part.split(":")
    return (int(w) * 7 + int(d)) * 1440 + int(h) * 60 + int(m)


# ── Evidence gathering ─────────────────────────────────────────────────────────

def gather_evidence(rt, results, recipient: str) -> str:
    """Collect observable evidence after an event for the LLM judge."""
    parts: list[str] = []

    # Replies from the chain triggered by this event
    replies = [r.reply for r in results if r.reply.strip()]
    if replies:
        parts.append("Replies:\n" + "\n".join(f"  [{r.object_id}]: {r.reply}" for r in results if r.reply.strip()))

    # State of all objects (captures write-service audit trails)
    for obj_id, obj in rt._bus.objects.items():
        state = obj.state.strip()
        if state:
            parts.append(f"State of [{obj_id}]:\n{state}")

    return "\n\n".join(parts) if parts else "(no observable state)"


# ── Core execution ─────────────────────────────────────────────────────────────

def _execute_test_case_inner(
    tc: TestCase,
    brain,
    harness,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Run a single TestCase and return event + modification results."""
    from src.lnl.gateway import EventGateway
    from src.lnl.runtime import Runtime
    from src.lnl.tools import CodeExecutor, ToolRegistry

    # 1. Create Runtime, EventGateway, and start the live environment
    tool_registry = ToolRegistry()
    tool_registry.register("execute_code", CodeExecutor())

    rt = Runtime(brain, strict_peers=False, tool_registry=tool_registry)
    gw = EventGateway(rt)

    for obj_def in tc.objects:
        rt.create_object(to_lnl_definition(obj_def))

    # Start the runtime — objects are now live instances
    rt.start()

    try:
        return _run_test_case_timeline(tc, rt, gw, harness)
    finally:
        rt.stop()


def _run_test_case_timeline(
    tc: TestCase,
    rt,
    gw,
    harness,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Execute steps and timeline events against a live runtime."""
    event_results: list[EventResult] = []
    mod_results: list[ModificationResult] = []

    # 2. Run steps — initialize state and assert default (no-modification) behavior
    for i, step in enumerate(tc.steps):
        t0 = time.monotonic()
        results = gw.dispatch(step.target, step.text)
        latency_ms = (time.monotonic() - t0) * 1000

        if step.expect is not None:
            in_tok = sum(r.metrics.input_tokens for r in results if r.metrics)
            out_tok = sum(r.metrics.output_tokens for r in results if r.metrics)
            evidence = gather_evidence(rt, results, step.target)
            passed, reasoning = harness.evaluate_assertion(step.expect.action, evidence)
            event_results.append(EventResult(
                event_id=f"S{i+1:03d}",
                passed=passed,
                reasoning=reasoning,
                input_tokens=in_tok,
                output_tokens=out_tok,
                latency_ms=latency_ms,
            ))

    # 3. Build sorted timeline: tag each item with its type and when-ordinal
    timeline: list[tuple[int, str, object]] = []
    for mod in tc.modifications:
        timeline.append((parse_when(mod.when), "mod", mod))
    for evt in tc.events:
        timeline.append((parse_when(evt.when), "event", evt))
    timeline.sort(key=lambda x: x[0])

    for _, kind, item in timeline:
        if kind == "mod":
            t0 = time.monotonic()
            results = rt.send(item.target, item.intent, sender=item.source)
            latency_ms = (time.monotonic() - t0) * 1000
            in_tok = sum(r.metrics.input_tokens for r in results if r.metrics)
            out_tok = sum(r.metrics.output_tokens for r in results if r.metrics)
            mod_results.append(ModificationResult(
                mod_id=item.id,
                input_tokens=in_tok,
                output_tokens=out_tok,
                latency_ms=latency_ms,
            ))

        else:  # event
            t0 = time.monotonic()
            if item.call_type == "send_event":
                results = gw.dispatch(item.recipient, item.input, source=item.source)
            else:
                results = rt.send(item.recipient, item.input, sender=item.source)
            latency_ms = (time.monotonic() - t0) * 1000

            in_tok = sum(r.metrics.input_tokens for r in results if r.metrics)
            out_tok = sum(r.metrics.output_tokens for r in results if r.metrics)

            evidence = gather_evidence(rt, results, item.recipient)
            condition = item.expect.action
            passed, reasoning = harness.evaluate_assertion(condition, evidence)

            event_results.append(EventResult(
                event_id=item.id,
                passed=passed,
                reasoning=reasoning,
                input_tokens=in_tok,
                output_tokens=out_tok,
                latency_ms=latency_ms,
            ))

    return event_results, mod_results


def execute_test_case(
    tc: TestCase,
    brain,
    harness,
    timeout_s: Optional[float] = None,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Run a single TestCase with an optional wall-clock timeout (seconds).

    If the timeout is exceeded, all pending events are marked as failed and
    pending modifications are recorded with zero cost.
    """
    if timeout_s is None:
        return _execute_test_case_inner(tc, brain, harness)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_execute_test_case_inner, tc, brain, harness)
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            future.cancel()
            # Mark all events as failed, record mods with zero cost
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


# ── Output path ────────────────────────────────────────────────────────────────

def default_output_path(input_path: Path) -> Path:
    return input_path.parent / f"{input_path.stem}_eval.jsonl"


# ── Main runner ────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> Path:
    """Run evaluation. Returns the output path."""
    if args.output is None:
        args.output = default_output_path(args.input)

    if args.provider is None:
        args.provider = infer_provider(args.model)

    if not args.input.exists():
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    test_cases = load_jsonl(args.input, TestCase)

    if args.limit:
        test_cases = test_cases[: args.limit]

    timeout_s: Optional[float] = getattr(args, "timeout", None)

    print(f"Loaded {len(test_cases)} test cases from {args.input}")
    print_run_info(
        args.provider,
        args.model,
        getattr(args, "seed", None),
        {
            "Runs per test case": str(args.runs),
            "Timeout per run": f"{timeout_s}s" if timeout_s else "none",
        },
    )

    # Build LNL brain and harness
    if args.provider == "openai":
        from src.lnl.brain import OpenAIBrain
        brain = OpenAIBrain(model=args.model)
    else:
        from src.lnl.brain import AnthropicBrain
        brain = AnthropicBrain(model=args.model)

    from src.lnl.benchmark import BenchmarkHarness
    harness = BenchmarkHarness(brain=brain)

    args.output.parent.mkdir(parents=True, exist_ok=True)

    all_tc_results: list[TestCaseResult] = []

    with open(args.output, "w") as f:
        for tc in test_cases:
            for run_idx in range(args.runs):
                label = f"{tc.id} run={run_idx}"
                print(f"  Evaluating {label} ...", end=" ", flush=True)
                try:
                    event_results, mod_results = execute_test_case(tc, brain, harness, timeout_s)
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
        default=120.0,
        metavar="SECONDS",
        help="Wall-clock timeout per test case run; exceeded runs are marked as failed (default: 120)",
    )
    add_common_args(parser)
    return parser


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
