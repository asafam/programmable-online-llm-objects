"""
Re-evaluation runner — re-judge existing run artifacts with a different judge.

Reads raw execution artifacts from a _runs.jsonl file (produced by evaluate.py)
and re-runs only the LLM-as-judge step, without re-executing the LNL runtime.

Use cases:
  - Re-judge with a different model or judge panel
  - Re-judge after correcting event expectations in test_cases.jsonl
  - Add a second opinion judge to an existing evaluation run

Usage:
    # Re-judge runs file with a different judge model
    python -m src.data.re_evaluate \\
        --from-runs outputs/.../test_cases_runs_20260408_120000.jsonl \\
        --judge-model gpt-4o

    # Re-judge via eval file (auto-locates runs file via runs_path in RunConfig)
    python -m src.data.re_evaluate \\
        --from-eval outputs/.../test_cases_eval_20260408_120000.jsonl \\
        --judge-model claude-opus-4-6

    # Re-judge with updated expectations
    python -m src.data.re_evaluate \\
        --from-runs outputs/.../test_cases_runs_20260408_120000.jsonl \\
        --test-cases outputs/.../test_cases.jsonl \\
        --judge-model gpt-4o
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

from src.data.schema import (
    EvalSummary,
    EventResult,
    ModificationResult,
    RawEventData,
    RawTestCaseResult,
    RunConfig,
    TestCase,
    TestCaseResult,
)
from src.data.utils import infer_provider, load_jsonl


# ── Judge factory (mirrors evaluate.py) ──────────────────────────────────────

def _make_judge(provider: str, model: str):
    if provider == "openai":
        from src.lnl.judge import OpenAIJudge
        return OpenAIJudge(model=model)
    elif provider == "google":
        from src.lnl.judge import GeminiJudge
        return GeminiJudge(model=model)
    else:
        from src.lnl.judge import AnthropicJudge
        return AnthropicJudge(model=model)


def _parse_judge_spec(spec: str) -> tuple[str, str]:
    if "/" in spec:
        provider, model = spec.split("/", 1)
    else:
        model = spec
        provider = infer_provider(model)
    return provider, model


def _build_judge(args: argparse.Namespace):
    llm_judge_specs: list[str] = getattr(args, "llm_judge", None) or []
    if llm_judge_specs:
        parsed = [_parse_judge_spec(s) for s in llm_judge_specs]
    elif getattr(args, "judge_model", None):
        jp = getattr(args, "judge_provider", None) or infer_provider(args.judge_model)
        parsed = [(jp, args.judge_model)]
    else:
        print("Error: specify --judge-model or --llm-judge", file=sys.stderr)
        sys.exit(1)

    single_judges = [_make_judge(p, m) for p, m in parsed]
    if len(single_judges) == 1:
        return single_judges[0], parsed
    from src.lnl.judge import PanelJudge
    labels = [f"{p}/{m}" for p, m in parsed]
    return PanelJudge(single_judges, judge_labels=labels), parsed


# ── Runs file loading ─────────────────────────────────────────────────────────

def _load_runs(runs_path: Path) -> list[RawTestCaseResult]:
    runs: list[RawTestCaseResult] = []
    for line in runs_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if data.get("record_type") == "raw_run":
                runs.append(RawTestCaseResult.model_validate(data))
        except Exception:
            pass
    return runs


def _locate_runs_path(args: argparse.Namespace) -> Path:
    """Resolve the runs file path from CLI args.

    Accepts either --from-runs (direct) or --from-eval (reads runs_path from RunConfig).
    """
    if getattr(args, "from_runs", None):
        p = args.from_runs
        if not p.exists():
            print(f"Error: runs file not found: {p}", file=sys.stderr)
            sys.exit(1)
        return p

    eval_path: Path = args.from_eval
    if not eval_path.exists():
        print(f"Error: eval file not found: {eval_path}", file=sys.stderr)
        sys.exit(1)

    # Look for runs_path in the RunConfig record (first line)
    for line in eval_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if data.get("record_type") == "run_config" and data.get("runs_path"):
                runs_path = Path(data["runs_path"])
                if runs_path.exists():
                    return runs_path
                print(
                    f"Error: runs_path in eval file points to a missing file: {runs_path}\n"
                    f"Run the original evaluate.py to regenerate, or pass --from-runs directly.",
                    file=sys.stderr,
                )
                sys.exit(1)
        except Exception:
            pass

    # Fall back to sibling _runs_ file derived from eval filename
    from src.data.evaluate import runs_path_from_eval_path
    candidate = runs_path_from_eval_path(eval_path)
    if candidate.exists():
        return candidate

    print(
        f"Error: could not locate runs file for {eval_path}.\n"
        f"Pass --from-runs <path> explicitly, or re-run evaluate.py to generate one.",
        file=sys.stderr,
    )
    sys.exit(1)


# ── Expectation override ──────────────────────────────────────────────────────

def _build_expectation_map(tc_path: Path) -> "dict[str, dict[str, str]]":
    """Return {tc_id: {event_id: updated_expected}} from a test cases file."""
    test_cases: list[TestCase] = load_jsonl(tc_path, TestCase)
    result: dict[str, dict[str, str]] = {}
    for tc in test_cases:
        mapping: dict[str, str] = {}
        for i, step in enumerate(tc.steps):
            if step.expect:
                mapping[f"S{i+1:03d}"] = step.expect.action
        for evt in tc.events:
            if evt.expect:
                mapping[evt.id] = evt.expect.action
        result[tc.id] = mapping
    return result


# ── Summary (duplicated from evaluate.py to avoid coupling) ──────────────────

def _compute_summary(results: list[TestCaseResult]) -> EvalSummary:
    import re
    import statistics

    def mean(vals):
        return sum(vals) / len(vals) if vals else 0.0

    all_events = [e for r in results for e in r.events]
    all_mods = [m for r in results for m in r.modifications]
    total_runs = len(results)
    total_test_cases = len({r.tc_id for r in results})

    # Step events (S\d+): deduplicate by sample_id (first TC per sample only)
    _step_re = re.compile(r"^S\d+$")
    seen_samples: set[str] = set()
    pass_rates: list[float] = []
    for r in results:
        effective = [e for e in r.events if not _step_re.match(e.event_id)]
        sample_key = r.sample_id or r.tc_id
        if sample_key not in seen_samples:
            seen_samples.add(sample_key)
            step_evts = [e for e in r.events if _step_re.match(e.event_id)]
            effective = step_evts + effective
        if effective:
            pass_rates.append(sum(1 for e in effective if e.passed) / len(effective))

    mean_pass_rate = mean(pass_rates) if pass_rates else 0.0
    by_tc: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.pass_rate is not None:
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


# ── Main runner ───────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> Path:
    runs_path = _locate_runs_path(args)
    raw_runs = _load_runs(runs_path)
    if not raw_runs:
        print(f"Error: no raw_run records found in {runs_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(raw_runs)} run records from {runs_path}")

    judge, parsed_judges = _build_judge(args)
    from src.lnl.benchmark import BenchmarkHarness
    harness = BenchmarkHarness(brain=None, judge=judge)

    # Expectation overrides from updated test cases
    expect_map: dict[str, dict[str, str]] = {}
    if getattr(args, "test_cases", None):
        expect_map = _build_expectation_map(args.test_cases)
        print(f"Loaded expectation overrides for {len(expect_map)} test cases from {args.test_cases}")

    if len(parsed_judges) == 1:
        judge_label = f"{parsed_judges[0][0]}/{parsed_judges[0][1]}"
    else:
        judge_label = f"panel({len(parsed_judges)}): " + ", ".join(f"{p}/{m}" for p, m in parsed_judges)

    # Output path
    if args.output is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = runs_path.parent / runs_path.name.replace("_runs_", f"_rejudged_{ts}_", 1)
        if "_runs_" not in runs_path.name:
            args.output = runs_path.parent / f"{runs_path.stem}_rejudged_{ts}.jsonl"

    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(f"Output: {args.output}")
    print(f"Judge:  {judge_label}")

    workers: int = getattr(args, "workers", 1)
    write_lock = threading.Lock()
    all_tc_results: list[TestCaseResult] = []

    run_config = RunConfig(
        timestamp=datetime.now().isoformat(),
        input_path=str(runs_path),
        output_path=str(args.output),
        runs_path=str(runs_path),
        model="",
        provider="",
        judge_model=parsed_judges[0][1],
        judge_provider=parsed_judges[0][0],
        judge_specs=[f"{p}/{m}" for p, m in parsed_judges] if len(parsed_judges) > 1 else [],
        runs=1,
        workers=workers,
        timeout_s=None,
        seed=None,
        steps_only=False,
        max_chain_depth=0,
        is_continuation=False,
    )

    def _rejudge_one(raw: RawTestCaseResult) -> TestCaseResult:
        tc_expect = expect_map.get(raw.tc_id, {})
        event_results: list[EventResult] = []

        for raw_evt in raw.events:
            condition = tc_expect.get(raw_evt.event_id, raw_evt.expected)
            passed, reasoning, votes = harness.evaluate_assertion(
                condition, raw_evt.evidence, raw_evt.prior_context
            )
            event_results.append(EventResult(
                event_id=raw_evt.event_id,
                passed=passed,
                reasoning=reasoning,
                expected=condition,
                input_tokens=raw_evt.input_tokens,
                output_tokens=raw_evt.output_tokens,
                latency_ms=raw_evt.latency_ms,
                judge_votes=votes if len(votes) > 1 else [],
            ))

        pass_rate = (
            sum(1 for e in event_results if e.passed) / len(event_results)
            if event_results else None
        )
        return TestCaseResult(
            tc_id=raw.tc_id,
            sample_id=raw.sample_id,
            tc_index=raw.tc_index,
            seed=raw.seed,
            name=raw.name,
            domain=raw.domain,
            run_index=raw.run_index,
            events=event_results,
            modifications=raw.modifications,
            pass_rate=pass_rate,
        )

    with open(args.output, "w") as f:
        f.write(run_config.model_dump_json() + "\n")
        f.flush()
        with tqdm(total=len(raw_runs), unit="run", desc="Re-judging") as pbar:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(_rejudge_one, raw): raw for raw in raw_runs}
                for future in as_completed(futures):
                    raw = futures[future]
                    try:
                        tc_result = future.result()
                        passed_n = sum(1 for e in tc_result.events if e.passed)
                        total_n = len(tc_result.events)
                        rate_str = (
                            f"{tc_result.pass_rate:.0%}"
                            if tc_result.pass_rate is not None
                            else "N/A"
                        )
                        tqdm.write(f"  {raw.tc_id} run={raw.run_index} → pass={passed_n}/{total_n} ({rate_str})")
                        with write_lock:
                            f.write(tc_result.model_dump_json() + "\n")
                            f.flush()
                            all_tc_results.append(tc_result)
                    except Exception as e:
                        tqdm.write(f"FAILED {raw.tc_id} run={raw.run_index}: {e}", file=sys.stderr)
                    pbar.update(1)

    summary = _compute_summary(all_tc_results)
    with open(args.output, "a") as f:
        f.write(summary.model_dump_json() + "\n")

    print()
    print(f"Complete. Output: {args.output}")
    print(f"Mean pass rate: {summary.mean_pass_rate:.3f}  std: {summary.pass_rate_std:.3f}")
    return args.output


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Re-judge existing run artifacts with a different LLM judge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Re-judge with a different model
  python -m src.data.re_evaluate --from-runs outputs/.../test_cases_runs_20260408_120000.jsonl --judge-model gpt-4o

  # Locate runs via eval file
  python -m src.data.re_evaluate --from-eval outputs/.../test_cases_eval_20260408_120000.jsonl --judge-model claude-opus-4-6

  # Re-judge with updated expectations
  python -m src.data.re_evaluate --from-runs outputs/.../test_cases_runs_20260408_120000.jsonl \\
      --test-cases outputs/.../test_cases.jsonl --judge-model gpt-4o

  # Panel judge
  python -m src.data.re_evaluate --from-runs outputs/.../runs.jsonl \\
      --llm-judge gpt-4o --llm-judge claude-sonnet-4-6
""",
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--from-runs",
        type=Path,
        metavar="JSONL",
        help="Path to a _runs.jsonl artifact file produced by evaluate.py",
    )
    source.add_argument(
        "--from-eval",
        type=Path,
        metavar="JSONL",
        help="Path to an _eval.jsonl file; the linked _runs.jsonl is located automatically via RunConfig.runs_path",
    )

    parser.add_argument(
        "--test-cases",
        type=Path,
        default=None,
        metavar="JSONL",
        help="Optional updated test_cases.jsonl — event expectations are re-read from here, overriding the values embedded in the runs file",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Output JSONL path (default: derived from runs file name)",
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help="Model for LLM-as-judge. Required unless --llm-judge is set.",
    )
    parser.add_argument(
        "--judge-provider",
        choices=["openai", "anthropic", "google"],
        default=None,
        help="Provider for judge model (inferred from judge-model if not specified)",
    )
    parser.add_argument(
        "--llm-judge",
        action="append",
        default=None,
        metavar="[PROVIDER/]MODEL",
        help=(
            "Judge model spec (can be repeated for a multi-judge panel). "
            "Format: 'model' (provider inferred) or 'provider/model'. "
            "Example: --llm-judge gpt-4o --llm-judge claude-sonnet-4-6"
        ),
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=1,
        help="Number of parallel workers (default: 1)",
    )
    return parser


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
