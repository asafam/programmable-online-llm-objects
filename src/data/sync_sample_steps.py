"""Sync Sample.steps from workflows.jsonl into workflows-mods.jsonl.

Each Sample in workflows-mods.jsonl carries its own copy of the parent
Workflow's `steps[]`. When workflows.jsonl is updated (e.g. by
regen_expects.py rewriting step expects), those Sample copies stay stale.

This script copies the freshest `steps[]` (with their `expect` fields)
from each workflow into every Sample whose `sample_id` matches that
workflow. Pure data move — no LLM calls.

Usage:
    python -m src.data.sync_sample_steps \\
        --samples outputs/.../workflows-mods.jsonl \\
        --workflows outputs/.../workflows.jsonl
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.data.schema import Sample, Workflow
from src.data.utils import load_jsonl


def main_with_args(args: argparse.Namespace) -> int:
    samples: list[Sample] = load_jsonl(args.samples, Sample)
    workflows: list[Workflow] = load_jsonl(args.workflows, Workflow)
    wf_by_id: dict[str, Workflow] = {w.id: w for w in workflows}

    updated = 0
    missing_parent = 0
    for s in samples:
        wf = wf_by_id.get(s.sample_id)
        if wf is None:
            missing_parent += 1
            continue
        # Deep-copy steps so future mutations don't share references
        s.steps = [Sample.model_fields["steps"].annotation.__args__[0].model_validate(st.model_dump())
                   for st in wf.steps]
        updated += 1

    output_path = args.output or args.samples
    with open(output_path, "w") as f:
        for s in samples:
            f.write(s.model_dump_json() + "\n")

    print(f"Updated steps on {updated} sample(s); {missing_parent} sample(s) without parent workflow.")
    print(f"Wrote {len(samples)} samples to {output_path}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--samples", required=True, type=Path,
                        help="Path to samples JSONL (Sample records). In-place unless --output set.")
    parser.add_argument("--workflows", required=True, type=Path,
                        help="Path to workflows JSONL (parent Workflow records).")
    parser.add_argument("--output", type=Path, default=None,
                        help="Write to this path instead of editing samples in-place.")
    args = parser.parse_args()
    sys.exit(main_with_args(args))


if __name__ == "__main__":
    main()
