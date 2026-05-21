"""
Repair test case issues in an existing samples.jsonl file.

Fixes:
  1. triggered_by=<modification_id>  →  triggered_by=None
     (triggered_by must reference a sibling event ID, never a modification ID)

  2. missing_step_data  →  re-run write_expectations for affected TCs
     (re-generates event expectations using the fixed prompt that prohibits
      hallucinated channel names)

Usage:
    python -m src.data.repair_test_cases \\
        --input outputs/data/zapier/ITER4/samples.jsonl \\
        --model claude-sonnet-4-6 \\
        --workers 4
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

from src.data.schema import Workflow, Sample, Workflows
from src.data.validate_test_cases import find_trigger_reference_errors, find_missing_step_data
from src.data.llm import create_llm
from src.data.utils import infer_provider, add_common_args
from src.data.generate_test_cases import _rewrite_event_expectations


# ── Fix 1: triggered_by patch (deterministic, no LLM) ────────────────────────

def _fix_triggered_by(tc: Sample) -> bool:
    """Clear triggered_by values that reference non-event IDs. Returns True if any change made."""
    event_ids = {e.id for e in tc.events}
    changed = False
    for evt in tc.events:
        if evt.triggered_by is not None and (
            evt.triggered_by not in event_ids or evt.triggered_by == evt.id
        ):
            evt.triggered_by = None
            changed = True
    return changed


# ── Fix 2: re-run expectations (LLM) ─────────────────────────────────────────

def _needs_expectation_rewrite(tc: Sample) -> bool:
    return bool(find_missing_step_data(tc)) or any(e.expect is None for e in tc.events)


# ── Workflow loader ─────────────────────────────────────────────────────────────

def _load_samples(workflows_path: Path) -> dict[str, Workflow]:
    samples: dict[str, Workflow] = {}
    if not workflows_path.exists():
        return samples
    with open(workflows_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if "id" not in d:
                continue
            s = Workflow.model_validate(d)
            samples[s.id] = s
    return samples


# ── Main repair loop ──────────────────────────────────────────────────────────

def repair(
    input_path: Path,
    workflows_path: Path | None,
    model: str,
    provider: str,
    workers: int,
    dry_run: bool,
) -> None:
    # Load all lines preserving original order and non-TC records
    raw_lines: list[str] = []
    tcs: list[tuple[int, Sample]] = []  # (line_index, tc)

    with open(input_path) as f:
        for i, line in enumerate(f):
            line = line.rstrip("\n")
            raw_lines.append(line)
            if not line:
                continue
            d = json.loads(line)
            if "record_type" in d or "tc_id" in d:
                continue  # skip run_config and result records
            try:
                tc = Sample.model_validate(d)
                tcs.append((i, tc))
            except Exception:
                pass  # non-TC lines (run_config etc.)

    print(f"Loaded {len(tcs)} test cases from {input_path}")

    # Load samples for expectations rewrite
    samples: dict[str, Workflow] = {}
    if workflows_path:
        samples = _load_samples(workflows_path)
        print(f"Loaded {len(samples)} samples from {workflows_path}")

    # Phase 1: deterministic triggered_by fix
    triggered_fixed = 0
    for _, tc in tcs:
        if _fix_triggered_by(tc):
            triggered_fixed += 1
    print(f"Phase 1: fixed triggered_by in {triggered_fixed} TCs")

    # Phase 2: expectations rewrite for missing_step_data
    needs_rewrite = [(i, tc) for i, tc in tcs if _needs_expectation_rewrite(tc)]
    print(f"Phase 2: {len(needs_rewrite)} TCs need expectations rewrite")

    if needs_rewrite and not dry_run:
        llm = create_llm(model=model, provider=provider)

        def _rewrite_one(args: tuple[int, Sample]) -> tuple[int, Sample]:
            idx, tc = args
            sample = samples.get(tc.sample_id)
            if sample is None:
                # Build a minimal Workflow from the TC itself
                sample = Workflow(
                    id=tc.sample_id or tc.id,
                    name=tc.name,
                    domain=tc.domain,
                    source_type=tc.source_type,
                    link=tc.link,
                    raw_steps=[s.text for s in tc.steps],
                    objects=tc.objects,
                    steps=tc.steps,
                    mock_tools=tc.mock_tools,
                )
            _rewrite_event_expectations(llm, tc, sample)
            return idx, tc

        rewrite_fixed = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_rewrite_one, item): item for item in needs_rewrite}
            for fut in tqdm(as_completed(futures), total=len(futures), desc="Rewriting expectations"):
                try:
                    idx, tc = fut.result()
                    # Patch back into raw_lines
                    raw_lines[idx] = tc.model_dump_json()
                    rewrite_fixed += 1
                except Exception as e:
                    orig_idx, orig_tc = futures[fut]
                    print(f"  WARN: rewrite failed for {orig_tc.id}: {e}", file=sys.stderr)

        print(f"Phase 2: rewrote expectations for {rewrite_fixed} TCs")

    # Phase 1 patched TCs: update raw_lines for triggered_by changes
    # (do this after phase 2 so both fixes are captured in the final write)
    for i, tc in tcs:
        if raw_lines[i]:
            try:
                d = json.loads(raw_lines[i])
                if "record_type" not in d and "tc_id" not in d:
                    raw_lines[i] = tc.model_dump_json()
            except Exception:
                pass

    if dry_run:
        print("Dry run — no changes written.")
        return

    # Deduplicate: if the file accumulated duplicate TC IDs across runs,
    # keep only the last occurrence (most recently written = repaired version).
    seen_ids: set[str] = set()
    deduped: list[str] = []
    for line in reversed(raw_lines):
        if not line:
            continue
        try:
            d = json.loads(line)
            if "record_type" in d or "tc_id" in d:
                deduped.insert(0, line)
                continue
            tc_id = d.get("id", "")
            if tc_id and tc_id not in seen_ids:
                seen_ids.add(tc_id)
                deduped.insert(0, line)
        except Exception:
            deduped.insert(0, line)

    output_path = input_path
    with open(output_path, "w") as f:
        for line in deduped:
            f.write(line + "\n")

    print(f"Written to {output_path}")

    # Re-validate to confirm fixes
    remaining_trigger = sum(1 for _, tc in tcs if find_trigger_reference_errors(tc))
    remaining_step = sum(1 for _, tc in tcs if find_missing_step_data(tc))
    print(f"\nAfter repair:")
    print(f"  trigger_reference_errors: {remaining_trigger} TCs remaining")
    print(f"  missing_step_data:        {remaining_step} TCs remaining")


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", required=True, help="Path to samples.jsonl")
    p.add_argument("--workflows", default=None, help="Path to workflows.jsonl (used to build full context for expectations rewrite)")
    p.add_argument("--model", default="claude-sonnet-4-6")
    p.add_argument("--provider", default=None)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    provider = args.provider or infer_provider(args.model)
    repair(
        input_path=Path(args.input),
        workflows_path=Path(args.workflows) if args.workflows else None,
        model=args.model,
        provider=provider,
        workers=args.workers,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
