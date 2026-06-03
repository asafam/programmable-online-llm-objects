"""Filter a workflows-mods JSONL down to a list of TC ids.

Usage:
  python scripts/subset_by_tc_id.py \
      --input outputs/data/zapier/async_subset/eval30.jsonl \
      --ids regression_ids.txt \
      --output regression_subset.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path, required=True)
    p.add_argument(
        "--ids",
        type=Path,
        required=True,
        help="file with one tc_id per line",
    )
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    wanted = {
        line.strip()
        for line in args.ids.read_text().splitlines()
        if line.strip()
    }
    if not wanted:
        print("no ids provided", file=sys.stderr)
        return 1

    n = 0
    with args.output.open("w") as fo:
        for raw in args.input.open():
            rec = json.loads(raw)
            if rec.get("id") in wanted:
                fo.write(raw)
                n += 1
    print(f"wrote {n}/{len(wanted)} → {args.output}")
    if n < len(wanted):
        missing = wanted - {json.loads(l)["id"] for l in args.output.open()}
        print(f"missing: {sorted(missing)[:10]} ...", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
