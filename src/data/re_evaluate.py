"""
Re-evaluation runner — re-judge existing eval artifacts with a different judge.

Reads SampleResult records from a _eval.jsonl file (produced by evaluate.py),
extracts the evidence and prior_context stored in each EventResult, and re-runs
only the LLM-as-judge step without re-executing the LNL runtime.

Use cases:
  - Re-judge with a different model or judge panel
  - Re-judge after correcting event expectations in samples.jsonl
  - Add a second opinion judge to an existing evaluation run

Usage:
    # Re-judge with a different judge model
    python -m src.data.re_evaluate \\
        --from-eval outputs/.../test_cases_eval_20260410_113327.jsonl \\
        --judge-model gpt-4o

    # Re-judge with updated expectations
    python -m src.data.re_evaluate \\
        --from-eval outputs/.../test_cases_eval_20260410_113327.jsonl \\
        --samples outputs/.../samples.jsonl \\
        --judge-model claude-opus-4-6

    # Panel judge (multiple judges, majority vote)
    python -m src.data.re_evaluate \\
        --from-eval outputs/.../test_cases_eval_20260410_113327.jsonl \\
        --llm-judge gpt-4o --llm-judge claude-sonnet-4-6
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
    RunConfig,
    Sample,
    SampleResult,
)
from src.data.utils import infer_provider, load_jsonl


# ── Judge factory ─────────────────────────────────────────────────────────────

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


# ── Eval file loading ─────────────────────────────────────────────────────────

def _load_eval(eval_path: Path) -> list[SampleResult]:
    """Load SampleResult records from an _eval.jsonl file."""
    results: list[SampleResult] = []
    for line in eval_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            rt = data.get("record_type", "")
            if rt in ("run_config", "eval_summary"):
                continue
            # SampleResult records have tc_id + run_index but no record_type
            if "tc_id" in data and "run_index" in data and "events" in data:
                results.append(SampleResult.model_validate(data))
        except Exception:
            pass
    return results


# ── Expectation override ──────────────────────────────────────────────────────

def _build_expectation_map(tc_path: Path) -> "dict[str, dict[str, str]]":
    """Return {tc_id: {event_id: updated_expected}} from a test cases file."""
    test_cases: list[Sample] = load_jsonl(tc_path, Sample)
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


# ── Summary ───────────────────────────────────────────────────────────────────

def _compute_summary(results: list[SampleResult]) -> EvalSummary:
    import re
    import statistics

    def mean(vals):
        return sum(vals) / len(vals) if vals else 0.0

    all_mods = [m for r in results for m in r.modifications]
    total_runs = len(results)
    total_test_cases = len({r.tc_id for r in results})

    _step_re = re.compile(r"^S\d+$")
    seen_samples: set[str] = set()
    all_events: list = []
    pass_rates: list[float] = []
    for r in results:
        effective = [e for e in r.events if not _step_re.match(e.event_id)]
        sample_key = r.sample_id or r.tc_id
        if sample_key not in seen_samples:
            seen_samples.add(sample_key)
            step_evts = [e for e in r.events if _step_re.match(e.event_id)]
            effective = step_evts + effective
        all_events.extend(effective)
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

    step_events = [e for e in all_events if _step_re.match(e.event_id)]
    steps_pass_rate = (
        sum(1 for e in step_events if e.passed) / len(step_events) if step_events else None
    )

    inconclusive_tc_ids: set[str] = set()
    for r in results:
        step_evts = [e for e in r.events if _step_re.match(e.event_id)]
        if step_evts and any(not e.passed for e in step_evts):
            inconclusive_tc_ids.add(r.tc_id)

    conclusive_events = [
        e for r in results if r.tc_id not in inconclusive_tc_ids
        for e in r.events
    ]

    def _role_pass_rate(role_val):
        evts = [e for e in conclusive_events if e.role == role_val]
        return (sum(1 for e in evts if e.passed) / len(evts)) if evts else None

    mod_events = [e for e in conclusive_events if e.role in ("pre_mod", "post_mod", "irrelevant")]
    mod_pass_rate = (sum(1 for e in mod_events if e.passed) / len(mod_events)) if mod_events else None

    return EvalSummary(
        total_test_cases=total_test_cases,
        total_runs=total_runs,
        total_events=len(all_events),
        mean_pass_rate=mean_pass_rate,
        pass_rate_std=pass_rate_std,
        steps_pass_rate=steps_pass_rate,
        mod_pass_rate=mod_pass_rate,
        pre_mod_pass_rate=_role_pass_rate("pre_mod"),
        post_mod_pass_rate=_role_pass_rate("post_mod"),
        irrelevant_pass_rate=_role_pass_rate("irrelevant"),
        inconclusive_tcs=len(inconclusive_tc_ids),
        mean_event_input_tokens=mean([e.input_tokens for e in all_events]),
        mean_event_output_tokens=mean([e.output_tokens for e in all_events]),
        mean_event_latency_ms=mean([e.latency_ms for e in all_events]),
        mean_mod_input_tokens=mean([m.input_tokens for m in all_mods]),
        mean_mod_output_tokens=mean([m.output_tokens for m in all_mods]),
        mean_mod_latency_ms=mean([m.latency_ms for m in all_mods]),
    )


# ── Main runner ───────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> Path:
    eval_path: Path = args.from_eval
    if not eval_path.exists():
        print(f"Error: eval file not found: {eval_path}", file=sys.stderr)
        sys.exit(1)

    tc_results = _load_eval(eval_path)
    if not tc_results:
        print(f"Error: no SampleResult records found in {eval_path}", file=sys.stderr)
        sys.exit(1)

    # Check that evidence is present (old eval files pre-refactor won't have it)
    missing_evidence = sum(1 for r in tc_results for e in r.events if not e.evidence)
    if missing_evidence:
        print(
            f"Warning: {missing_evidence} events have no evidence stored. "
            f"These were produced before the eval storage refactor and cannot be re-judged. "
            f"Re-run evaluate.py to regenerate.",
            file=sys.stderr,
        )

    print(f"Loaded {len(tc_results)} run records from {eval_path}")

    judge, parsed_judges = _build_judge(args)
    from src.lnl.benchmark import BenchmarkHarness
    harness = BenchmarkHarness(brain=None, judge=judge)

    # Expectation overrides from updated test cases
    expect_map: dict[str, dict[str, str]] = {}
    if getattr(args, "test_cases", None):
        expect_map = _build_expectation_map(args.samples)
        print(f"Loaded expectation overrides for {len(expect_map)} test cases from {args.samples}")

    if len(parsed_judges) == 1:
        judge_label = f"{parsed_judges[0][0]}/{parsed_judges[0][1]}"
    else:
        judge_label = f"panel({len(parsed_judges)}): " + ", ".join(f"{p}/{m}" for p, m in parsed_judges)

    # Output path
    if args.output is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = eval_path.stem.replace("_eval_", "_rejudged_", 1)
        if "_eval_" not in eval_path.stem:
            stem = f"{eval_path.stem}_rejudged"
        args.output = eval_path.parent / f"{stem}_{ts}.jsonl"

    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(f"Output: {args.output}")
    print(f"Judge:  {judge_label}")

    workers: int = getattr(args, "workers", 1)
    write_lock = threading.Lock()
    all_tc_results: list[SampleResult] = []

    run_config = RunConfig(
        timestamp=datetime.now().isoformat(),
        input_path=str(eval_path),
        output_path=str(args.output),
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

    def _rejudge_one(orig: SampleResult) -> SampleResult:
        tc_expect = expect_map.get(orig.tc_id, {})
        event_results: list[EventResult] = []

        for evt in orig.events:
            if not evt.evidence:
                # No evidence stored — keep original verdict unchanged
                event_results.append(evt)
                continue

            condition = tc_expect.get(evt.event_id, evt.expected)
            passed, reasoning, votes, j_in_tok, j_out_tok = harness.evaluate_assertion(
                condition, evt.evidence, evt.prior_context
            )
            event_results.append(EventResult(
                event_id=evt.event_id,
                passed=passed,
                reasoning=reasoning,
                expected=condition,
                evidence=evt.evidence,
                prior_context=evt.prior_context,
                input_tokens=evt.input_tokens,
                output_tokens=evt.output_tokens,
                latency_ms=evt.latency_ms,
                judge_input_tokens=j_in_tok,
                judge_output_tokens=j_out_tok,
                judge_votes=votes,  # always stored — enables per-judge audit
            ))

        pass_rate = (
            sum(1 for e in event_results if e.passed) / len(event_results)
            if event_results else None
        )
        return SampleResult(
            tc_id=orig.tc_id,
            sample_id=orig.sample_id,
            tc_index=orig.tc_index,
            seed=orig.seed,
            name=orig.name,
            domain=orig.domain,
            run_index=orig.run_index,
            events=event_results,
            modifications=orig.modifications,
            pass_rate=pass_rate,
        )

    with open(args.output, "w") as f:
        f.write(run_config.model_dump_json() + "\n")
        f.flush()
        with tqdm(total=len(tc_results), unit="run", desc="Re-judging") as pbar:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(_rejudge_one, orig): orig for orig in tc_results}
                for future in as_completed(futures):
                    orig = futures[future]
                    try:
                        tc_result = future.result()
                        passed_n = sum(1 for e in tc_result.events if e.passed)
                        total_n = len(tc_result.events)
                        rate_str = (
                            f"{tc_result.pass_rate:.0%}"
                            if tc_result.pass_rate is not None
                            else "N/A"
                        )
                        tqdm.write(f"  {orig.tc_id} run={orig.run_index} → pass={passed_n}/{total_n} ({rate_str})")
                        with write_lock:
                            f.write(tc_result.model_dump_json() + "\n")
                            f.flush()
                            all_tc_results.append(tc_result)
                    except Exception as e:
                        tqdm.write(f"FAILED {orig.tc_id} run={orig.run_index}: {e}", file=sys.stderr)
                    pbar.update(1)

    summary = _compute_summary(all_tc_results)
    with open(args.output, "a") as f:
        f.write(summary.model_dump_json() + "\n")

    def _fmt(v) -> str:
        return f"{v:.3f}" if v is not None else "N/A"

    print()
    print(f"Complete. Output: {args.output}")
    print(f"Mean pass rate:      {_fmt(summary.mean_pass_rate)}  std: {_fmt(summary.pass_rate_std)}")
    print(f"Steps pass rate:     {_fmt(summary.steps_pass_rate)}")
    print(f"Pre-mod pass rate:   {_fmt(summary.pre_mod_pass_rate)}")
    print(f"Post-mod pass rate:  {_fmt(summary.post_mod_pass_rate)}")
    print(f"Irrelevant pass rate:{_fmt(summary.irrelevant_pass_rate)}")
    print(f"Inconclusive TCs:    {summary.inconclusive_tcs}")
    return args.output


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Re-judge existing eval artifacts with a different LLM judge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Re-judge with a different model
  python -m src.data.re_evaluate \\
      --from-eval outputs/.../test_cases_eval_20260410_113327.jsonl \\
      --judge-model gpt-4o

  # Re-judge with updated expectations
  python -m src.data.re_evaluate \\
      --from-eval outputs/.../test_cases_eval_20260410_113327.jsonl \\
      --samples outputs/.../samples.jsonl --judge-model claude-opus-4-6

  # Panel judge (majority vote across multiple models)
  python -m src.data.re_evaluate \\
      --from-eval outputs/.../test_cases_eval_20260410_113327.jsonl \\
      --llm-judge gpt-4o --llm-judge claude-sonnet-4-6
""",
    )

    parser.add_argument(
        "--from-eval",
        type=Path,
        required=True,
        metavar="JSONL",
        help="Path to a _eval.jsonl file produced by evaluate.py",
    )
    parser.add_argument(
        "--samples",
        type=Path,
        default=None,
        metavar="JSONL",
        help="Optional updated samples.jsonl — event expectations override the stored values",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Output JSONL path (default: derived from eval file name)",
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
