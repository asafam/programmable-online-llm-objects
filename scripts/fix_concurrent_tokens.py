#!/usr/bin/env python3
"""Retroactively fix inflated input/output_tokens in concurrent-group events.

In evaluate.py, _record_event_result received the full batch result list from
dispatch_many/send_many and summed ALL events' tokens for EVERY event in the
batch. At conc=N, each event was attributed N× its true cost.

Fix: for events with role in {pre_mod, post_mod, irrelevant} in files with
conc > 1, divide input_tokens and output_tokens by the concurrency level.
Step events and solo-mod events are unaffected (they fire outside the batch).

Usage:
    python scripts/fix_concurrent_tokens.py [exp_dir]
    python scripts/fix_concurrent_tokens.py --dry-run [exp_dir]
"""

import argparse
import json
import re
import shutil
from pathlib import Path

CONCURRENT_ROLES = {"pre_mod", "post_mod", "irrelevant"}
STEP_ID = re.compile(r"^S\d+$")
PATTERN = re.compile(r"exp_(lnl|baseline)_(\d+)mod_conc(\d+)\.jsonl$")


def fix_file(path: Path, conc: int, dry_run: bool) -> dict:
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))

    n_fixed = 0
    for rec in records:
        if "tc_id" not in rec:
            continue
        for evt in rec.get("events", []):
            eid  = evt.get("event_id", "")
            role = "step" if STEP_ID.match(eid) else evt.get("role")
            if role not in CONCURRENT_ROLES:
                continue
            for key in ("input_tokens", "output_tokens"):
                v = evt.get(key)
                if v:
                    evt[key] = v // conc
                    n_fixed += 1

    if not dry_run and n_fixed > 0:
        backup = path.with_suffix(".jsonl.tok_bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        with open(path, "w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    return {"n_fixed": n_fixed, "n_records": len(records)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("exp_dir", nargs="?",
                        default="outputs/data/zapier/20260421_zapier_fixed/runs/experiments",
                        help="Directory containing exp_*.jsonl files")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing files")
    args = parser.parse_args()

    exp_dir = Path(args.exp_dir)
    if not exp_dir.exists():
        print(f"Directory not found: {exp_dir}")
        return

    files = sorted(exp_dir.glob("exp_*.jsonl"))
    total_fixed = 0

    for path in files:
        # Skip backups
        if ".tok_bak" in path.name or ".bak" in path.name:
            continue
        m = PATTERN.match(path.name)
        if not m:
            continue
        conc = int(m.group(3))
        if conc <= 1:
            print(f"  {path.name}: conc=1, skipping (no inflation)")
            continue

        result = fix_file(path, conc, args.dry_run)
        tag = "(dry-run) " if args.dry_run else ""
        action = "would fix" if args.dry_run else "fixed"
        print(f"  {path.name}: conc={conc}  {tag}{action} {result['n_fixed']} token fields "
              f"(÷{conc}) across {result['n_records']} records")
        total_fixed += result["n_fixed"]

    print()
    if args.dry_run:
        print(f"Dry run complete — {total_fixed} token fields would be updated.")
    else:
        print(f"Done — {total_fixed} token fields updated. Originals backed up as *.jsonl.tok_bak")


if __name__ == "__main__":
    main()
