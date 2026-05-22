"""
Stage 3: Generate seed data (mock tools) and expectations jointly.

Reads test cases (from Stage 2) and their matching samples (from Stage 1),
then for each sample group (all mod-type variants):
  1. Collects all event inputs from ALL variants + step texts — one rich context.
  2. Generates mock tool data ONCE per sample, shared across all variants.
  3. Writes event expectations per TC using that exact mock data.

Processing at sample granularity (not per TC) avoids redundant LLM calls:
80 samples × N tools instead of 480 TCs × N tools.

Continuation: the output file is written incrementally per sample. Interrupted
runs resume from the last completed sample (TCs with mock_tools + expectations
are skipped).

Usage:
    python -m src.data.generate_seed \\
        --input outputs/my-run/samples.jsonl \\
        --workflows outputs/my-run/workflows.jsonl

    # Force regeneration of all samples
    python -m src.data.generate_seed \\
        --input outputs/my-run/samples.jsonl \\
        --workflows outputs/my-run/workflows.jsonl --force

    # Write to a separate output file (non-destructive)
    python -m src.data.generate_seed \\
        --input outputs/my-run/samples.jsonl \\
        --workflows outputs/my-run/workflows.jsonl \\
        -o outputs/my-run/test_cases_seeded.jsonl
"""
from __future__ import annotations

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

from src.data.schema import Workflow, Sample
from src.data.generate_workflows import _generate_mock_tool_data, _DATA_TOOL_RE
from src.data.generate_samples import _rewrite_event_expectations
from src.data.llm import create_llm
from src.data.utils import (
    add_common_args,
    infer_provider,
    print_run_info,
)


def generate_seed(llm, test_case: Sample, sample: Workflow) -> None:
    """Generate mock_tools and expectations for one TC in one consistent pass.

    Context = step texts + THIS TC's own event inputs only. Each TC is an
    independent unit at eval time, so its mock data reflects exactly what
    its own events need — no cross-variant contamination.

    Tool discovery (union, not replacement):
      - Pre-existing tools in tc.tools (from retrofit/discover passes)
      - Regex-detected tools from object behavior descriptions
    Mutates test_case in place.
    """
    step_texts = [s.text for s in sample.steps if s.text]
    event_texts = [e.input for e in test_case.events if getattr(e, "input", None)]
    all_texts = step_texts + event_texts

    # Build tool map: existing tools first, then regex-detected additions
    tool_map: dict[str, str] = {}  # tool_name → description
    for tool in test_case.tools:
        tool_map[tool.tool_name] = tool.description
    for obj in sample.objects:
        match = _DATA_TOOL_RE.search(obj.behavior or "")
        if match:
            tool_map.setdefault(
                match.group(1),
                (obj.state_description or "").strip() or obj.role,
            )

    mock_tools = []
    for tool_name, description in tool_map.items():
        tool = _generate_mock_tool_data(llm, tool_name, description, all_texts)
        if tool:
            mock_tools.append(tool)
    test_case.tools = mock_tools

    _rewrite_event_expectations(llm, test_case, sample)


def _needs_seed(tc: Sample, force: bool) -> bool:
    if force:
        return True
    # Expectations are the reliable completion signal — a TC with no data-lookup
    # objects legitimately has empty mock_tools, so we don't require them here.
    return not all(e.expect is not None for e in tc.events)


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage 3: Generate seed data and expectations for test cases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.data.generate_seed \\
      --input outputs/my-run/samples.jsonl \\
      --workflows outputs/my-run/workflows.jsonl

  python -m src.data.generate_seed \\
      --input outputs/my-run/samples.jsonl \\
      --workflows outputs/my-run/workflows.jsonl --force
""",
    )
    parser.add_argument(
        "--input", "-i",
        type=Path,
        required=True,
        help="Path to test cases JSONL file (output from Stage 2)",
    )
    parser.add_argument(
        "--workflows",
        type=Path,
        required=True,
        help="Path to samples JSONL file (output from Stage 1)",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Output JSONL path (default: overwrites input file in place)",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=1,
        help="Number of parallel sample workers (default: 1). One worker per sample group.",
    )
    add_common_args(parser)
    return parser


def run(args: argparse.Namespace) -> Path:
    if args.provider is None:
        args.provider = infer_provider(args.model)

    output_path = args.output if args.output else args.input

    if not args.input.exists():
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)
    if not args.workflows.exists():
        print(f"Error: samples file not found: {args.workflows}", file=sys.stderr)
        sys.exit(1)

    # Load samples indexed by id
    samples_by_id: dict[str, Workflow] = {}
    with open(args.workflows) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            s = Workflow.model_validate_json(line)
            samples_by_id[s.id] = s

    # Load test cases, preserving original order
    test_cases: list[Sample] = []
    with open(args.input) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            test_cases.append(Sample.model_validate_json(line))

    if args.limit:
        test_cases = test_cases[: args.limit]

    pending = [tc for tc in test_cases if _needs_seed(tc, args.force)]
    already_done = len(test_cases) - len(pending)

    if not pending:
        print("All test cases already seeded. Use --force to regenerate.")
        return output_path

    if already_done:
        print(f"Resuming: {already_done}/{len(test_cases)} already done, {len(pending)} remaining")
    else:
        print(f"Seeding {len(pending)} test cases")

    print_run_info(args.provider, args.model, args.seed, {
        "Workers": str(getattr(args, "workers", 1)),
    })

    llm = create_llm(
        provider=args.provider,
        model=args.model,
        temperature=args.temperature,
        seed=args.seed,
    )

    workers: int = getattr(args, "workers", 1)
    success_count = 0
    fail_count = 0
    write_lock = threading.Lock()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Write initial snapshot so the file exists for incremental flushes
    with open(output_path, "w") as f:
        for tc in test_cases:
            f.write(tc.model_dump_json() + "\n")

    def _seed_one(tc: Sample) -> bool:
        sample_id = tc.id.rsplit("-TC", 1)[0]
        sample = samples_by_id.get(sample_id)
        if sample is None:
            tqdm.write(f"  Warning: no sample found for {tc.id}", file=sys.stderr)
            return False
        generate_seed(llm, tc, sample)
        # Success = expectations written for all events (mock_tools may be empty
        # for TCs with no data-lookup objects, which is valid)
        return all(e.expect is not None for e in tc.events)

    def _flush() -> None:
        """Overwrite output with current in-memory state (called after each TC completes)."""
        with write_lock:
            with open(output_path, "w") as f:
                for tc in test_cases:
                    f.write(tc.model_dump_json() + "\n")

    with tqdm(total=len(pending), unit="tc", desc="Seeding") as pbar:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_seed_one, tc): tc for tc in pending}
            for future in as_completed(futures):
                tc = futures[future]
                try:
                    ok = future.result()
                    _flush()
                except Exception as e:
                    tqdm.write(f"  FAILED {tc.id}: {e}", file=sys.stderr)
                    ok = False
                if ok:
                    success_count += 1
                else:
                    fail_count += 1
                pbar.update(1)

    print()
    print(f"Complete. Output: {output_path}")
    print(f"Seeded: {success_count} (failed: {fail_count})")
    return output_path


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
