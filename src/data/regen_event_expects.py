"""Stage 2 surgical: regenerate event expect.action / expect.reason in samples.

Reuses generate_samples._rewrite_event_expectations to rewrite event expects
without touching modifications, inputs, timestamps, or roles. Use after the
write_expectations prompt rule changes (e.g., adding exhaustive-category
terminal-state checks, applying mod effects).

Usage:
    python -m src.data.regen_event_expects \\
        --samples outputs/.../workflows-mods.jsonl \\
        --workflows outputs/.../workflows.jsonl \\
        --provider azure --model gpt-5.4 \\
        --workers 12
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from src.data.generate_samples import _rewrite_event_expectations
from src.data.llm import create_llm
from src.data.schema import Sample, Workflow
from src.data.utils import load_jsonl

load_dotenv()


def main_with_args(args: argparse.Namespace) -> int:
    samples: list[Sample] = load_jsonl(args.samples, Sample)
    workflows: list[Workflow] = load_jsonl(args.workflows, Workflow)
    wf_by_id = {w.id: w for w in workflows}

    if args.filter:
        ids = set(args.filter)
        samples = [s for s in samples if s.id in ids]
    if args.limit:
        samples = samples[: args.limit]
    if not samples:
        print("No samples to process.", file=sys.stderr)
        return 1

    output_path = args.output or args.samples

    llm = create_llm(provider=args.provider, model=args.model, temperature=args.temperature)

    updated: dict[str, Sample] = {}

    def _process(s: Sample) -> Sample:
        wf = wf_by_id.get(s.sample_id)
        if wf is None:
            print(f"  ✗ {s.id}: parent workflow '{s.sample_id}' not found — skipping", file=sys.stderr)
            return s
        _rewrite_event_expectations(llm, s, wf)
        return s

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_process, s): s for s in samples}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # pragma: no cover
                print(f"  ✗ {s.id}: rewrite crashed — {exc}", file=sys.stderr)
                updated[s.id] = s
                continue
            updated[s.id] = result
            print(f"  ✓ {result.id}  ({len(result.events)} events refreshed)")

    # If writing in-place, preserve any samples we didn't touch (filter / limit).
    if output_path == args.samples:
        all_samples: list[Sample] = load_jsonl(args.samples, Sample)
        out_list = [updated.get(s.id, s) for s in all_samples]
    else:
        out_list = [updated[s.id] for s in samples if s.id in updated]

    with open(output_path, "w") as f:
        for s in out_list:
            f.write(s.model_dump_json() + "\n")

    print(f"\nWrote {len(out_list)} samples to {output_path}")
    print(f"Regenerated event expects for: {len(updated)}")
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
    parser.add_argument("--provider", default=os.environ.get("LLM_PROVIDER", "azure"))
    parser.add_argument("--model", default="gpt-5.4")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--filter", nargs="+", default=None)
    args = parser.parse_args()
    sys.exit(main_with_args(args))


if __name__ == "__main__":
    main()
