"""Derive a clean samples.jsonl from test_cases.jsonl.

For each sample_id, pick the test_case variant with the fewest peer-behavior
mismatches, reduce it to a Sample-shaped record (strip modifications/events,
restore sample_id as id), then fix remaining mismatches by adding missing
peers with a generic relationship description.

Output: <out_dir>/samples.jsonl and <out_dir>/derive_log.md
"""

from __future__ import annotations
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


def find_mismatches(record: dict) -> list[tuple[str, str]]:
    """Return [(source_object_id, referenced_but_undeclared_peer_id), ...]."""
    objs = {o["object_id"]: o for o in record.get("objects", [])}
    issues = []
    for obj in record.get("objects", []):
        oid = obj["object_id"]
        beh = obj.get("behavior", "").lower()
        peer_ids = [
            (p.get("object_id") if isinstance(p, dict) else p)
            for p in obj.get("peers", [])
        ]
        for other in objs:
            if other == oid:
                continue
            if re.search(r"\b" + re.escape(other) + r"\b", beh):
                if other not in peer_ids:
                    issues.append((oid, other))
    return issues


def to_sample(tc: dict) -> dict:
    """Convert a TestCase record to a Sample-shaped record.

    sample_id becomes id; modifications and events are dropped. raw_steps is
    empty (the source markdown that produced steps is not recoverable from
    test_cases.jsonl). flagged/flag_reasons default to False/[].
    """
    return {
        "id": tc.get("sample_id") or tc.get("id"),
        "name": tc.get("name", ""),
        "domain": tc.get("domain", ""),
        "source_type": tc.get("source_type", ""),
        "link": tc.get("link", ""),
        "raw_steps": [],
        "objects": tc.get("objects", []),
        "steps": tc.get("steps", []),
        "mock_tools": tc.get("mock_tools", []),
        "flagged": False,
        "flag_reasons": [],
    }


def fix_peers_in_place(sample: dict) -> int:
    """Add missing peer declarations for behavior-referenced object_ids.

    Returns the number of peers added.
    """
    objs_by_id = {o["object_id"]: o for o in sample["objects"]}
    added = 0
    for obj in sample["objects"]:
        oid = obj["object_id"]
        beh = obj.get("behavior", "").lower()
        existing_peers = obj.setdefault("peers", [])
        existing_ids = {
            (p.get("object_id") if isinstance(p, dict) else p)
            for p in existing_peers
        }
        for other in objs_by_id:
            if other == oid:
                continue
            if not re.search(r"\b" + re.escape(other) + r"\b", beh):
                continue
            if other in existing_ids:
                continue
            existing_peers.append({
                "object_id": other,
                "relationship": (
                    "Receives payload as described in this object's behavior "
                    "(auto-added by derive_clean_samples.py to repair "
                    "peer-behavior mismatch)."
                ),
            })
            existing_ids.add(other)
            added += 1
    return added


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", "-i", type=Path, required=True,
                   help="test_cases.jsonl")
    p.add_argument("--out-dir", "-o", type=Path, required=True,
                   help="Output directory; samples.jsonl and derive_log.md are written here.")
    args = p.parse_args(argv)

    args.out_dir.mkdir(parents=True, exist_ok=True)

    # Match the eval's selection logic (evaluate.py:1106-1113 — steps-only mode):
    # for each sample_id, take the FIRST occurrence in file order. This makes
    # the derived samples.jsonl evaluate-equivalent to running the eval on
    # test_cases.jsonl with --steps-only, so peer-fix results are directly
    # comparable to prior eval runs.
    seen: set[str] = set()
    chosen: list[dict] = []
    chosen_meta: list[tuple[str, int, int]] = []  # (sample_id, pre_fix_mismatches, file_idx)
    n_variants_seen: dict[str, int] = defaultdict(int)
    with args.input.open() as f:
        for idx, line in enumerate(f):
            d = json.loads(line)
            if "sample_id" not in d or "objects" not in d:
                continue
            sid = d["sample_id"]
            n_variants_seen[sid] += 1
            if sid in seen:
                continue
            seen.add(sid)
            chosen.append(d)
            chosen_meta.append((sid, len(find_mismatches(d)), idx))

    # Convert to samples and fix
    fixed_samples: list[dict] = []
    fix_records: list[tuple[str, int, list[tuple[str, str]]]] = []
    for tc in chosen:
        s = to_sample(tc)
        before = find_mismatches(s)
        added = fix_peers_in_place(s)
        after = find_mismatches(s)
        fixed_samples.append(s)
        fix_records.append((s["id"], added, before))
        assert not after, (
            f"Sample {s['id']} still has mismatches after fix: {after}"
        )

    # Write samples.jsonl
    out_samples = args.out_dir / "samples.jsonl"
    with out_samples.open("w") as fo:
        for s in fixed_samples:
            fo.write(json.dumps(s) + "\n")

    # Write log
    n_fixed = sum(1 for _, added, _ in fix_records if added > 0)
    total_added = sum(added for _, added, _ in fix_records)
    log = args.out_dir / "derive_log.md"
    with log.open("w") as fo:
        fo.write("# samples.jsonl derivation log\n\n")
        fo.write(f"**Input:** `{args.input}`\n")
        fo.write(f"**Output:** `{out_samples}`\n\n")
        fo.write(f"**Total samples:** {len(fixed_samples)}\n")
        fo.write(f"**Samples needing peer fixes:** {n_fixed}\n")
        fo.write(f"**Total peer entries added:** {total_added}\n\n")
        fo.write("## Per-sample variant selection\n\n")
        fo.write("Matches the eval's steps-only dedupe logic: first occurrence per "
                 "sample_id in file order. Ensures the derived samples are "
                 "evaluate-equivalent to the original eval runs.\n\n")
        fo.write("| sample_id | file_idx | pre_fix_mismatches |\n|---|---|---|\n")
        for sid, pre, idx in sorted(chosen_meta, key=lambda x: -x[1]):
            fo.write(f"| `{sid}` | {idx} | {pre} |\n")
        fo.write("\n## Per-sample fixes applied\n\n")
        for sid, added, mismatches in fix_records:
            if not added:
                continue
            fo.write(f"### `{sid}` — added {added} peer entries\n\n")
            for src_obj, missing in mismatches:
                fo.write(f"- `[{src_obj}]` ← added peer `{missing}`\n")
            fo.write("\n")

    print(f"Wrote: {out_samples} ({len(fixed_samples)} samples)")
    print(f"Wrote: {log}")
    print(f"Samples fixed: {n_fixed} / {len(fixed_samples)}; peers added: {total_added}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
