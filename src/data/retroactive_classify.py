"""
Retroactive event role classifier.

Reads an _eval.jsonl file and a samples.jsonl file, then populates
EventResult.role for all non-step events based on:

  1. Explicit role from the test case Event.role (if set).
  2. Timing heuristic: event.when < first modification.when → "pre_mod",
     event.when >= first modification.when → "post_mod".
     "irrelevant" cannot be recovered from timing alone and is left as None
     unless the test case has an explicit role assigned.

Step events (S001, S002, ...) are always left with role=None — they are
baseline setup events and are handled separately in summary metrics.

Usage:
    python -m src.data.retroactive_classify \\
        --eval outputs/.../test_cases_eval_20260410_113327.jsonl \\
        --samples outputs/.../samples.jsonl

The input file is updated in-place (a .orig backup is written first).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

_STEP_RE = re.compile(r"^S\d+$")


# ── Timestamp parsing ─────────────────────────────────────────────────────────

def _parse_when(when: str) -> float:
    """Parse WW-DThh:mm → sortable float (week*10080 + day*1440 + hh*60 + mm)."""
    m = re.match(r"W(\d+)-(\d+)T(\d{1,2}):(\d{2})", when)
    if not m:
        return 0.0
    wk, dy, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return wk * 10080 + dy * 1440 + hh * 60 + mm


# ── Build role map from test cases ────────────────────────────────────────────

def _build_role_map(tc_path: Path) -> dict[str, dict[str, Optional[str]]]:
    """Return {tc_id: {event_id: role}} for all non-step events.

    Role is taken from Event.role if set; otherwise inferred from timing.
    """
    role_map: dict[str, dict[str, Optional[str]]] = {}
    for line in tc_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        # Support both Samples wrapper and bare Sample objects
        tcs = data.get("test_cases", [data]) if "test_cases" in data else [data]
        for tc in tcs:
            tc_id = tc.get("id", "")
            mods = tc.get("modifications", [])
            events = tc.get("events", [])
            if not tc_id:
                continue

            first_mod_when = min(
                (_parse_when(m.get("when", "W99-9T23:59")) for m in mods),
                default=float("inf"),
            )

            mapping: dict[str, Optional[str]] = {}
            for evt in events:
                eid = evt.get("id", "")
                if not eid or _STEP_RE.match(eid):
                    continue
                role = evt.get("role") or None
                if role is None:
                    # Infer from timing
                    evt_when = _parse_when(evt.get("when", "W00-0T00:00"))
                    if evt_when < first_mod_when:
                        role = "pre_mod"
                    else:
                        role = "post_mod"
                mapping[eid] = role

            role_map[tc_id] = mapping
    return role_map


# ── Summary recompute ─────────────────────────────────────────────────────────

def _recompute_summary(results: list[dict]) -> dict:
    import statistics
    from collections import defaultdict

    def mean(vals):
        return sum(vals) / len(vals) if vals else 0.0

    step_re = _STEP_RE
    all_events: list[dict] = []
    pass_rates: list[float] = []
    seen_samples: set[str] = set()

    # Deduplicate step events: only include steps for the first TC seen per sample
    first_tc_per_sample: dict[str, str] = {}
    for r in results:
        sid = r.get("sample_id") or r.get("tc_id", "")
        if sid not in first_tc_per_sample:
            first_tc_per_sample[sid] = r["tc_id"]
    base_tc_ids = set(first_tc_per_sample.values())

    for r in results:
        is_base = r["tc_id"] in base_tc_ids
        effective = [
            e for e in r.get("events", [])
            if is_base or not step_re.match(e.get("event_id", ""))
        ]
        all_events.extend(effective)
        if effective:
            pass_rates.append(sum(1 for e in effective if e.get("passed")) / len(effective))

    mean_pass_rate = mean(pass_rates) if pass_rates else 0.0

    by_tc: dict[str, list[float]] = defaultdict(list)
    for r in results:
        pr = r.get("pass_rate")
        if pr is not None:
            by_tc[r["tc_id"]].append(pr)
    per_tc_stds = [statistics.stdev(v) for v in by_tc.values() if len(v) > 1]
    pass_rate_std = mean(per_tc_stds)

    def role_rate(role_val):
        evts = [e for e in all_events if e.get("role") == role_val]
        return (sum(1 for e in evts if e.get("passed")) / len(evts)) if evts else None

    step_evts = [e for e in all_events if step_re.match(e.get("event_id", ""))]
    steps_pass_rate = (
        sum(1 for e in step_evts if e.get("passed")) / len(step_evts) if step_evts else None
    )

    inconclusive_tc_ids: set[str] = set()
    for r in results:
        s_evts = [e for e in r.get("events", []) if step_re.match(e.get("event_id", ""))]
        if s_evts and any(not e.get("passed") for e in s_evts):
            inconclusive_tc_ids.add(r["tc_id"])

    all_mods = [m for r in results for m in r.get("modifications", [])]

    return {
        "record_type": "eval_summary",
        "total_test_cases": len({r["tc_id"] for r in results}),
        "total_runs": len(results),
        "total_events": len(all_events),
        "mean_pass_rate": mean_pass_rate,
        "pass_rate_std": pass_rate_std,
        "steps_pass_rate": steps_pass_rate,
        "pre_mod_pass_rate": role_rate("pre_mod"),
        "post_mod_pass_rate": role_rate("post_mod"),
        "irrelevant_pass_rate": role_rate("irrelevant"),
        "inconclusive_tcs": len(inconclusive_tc_ids),
        "mean_event_input_tokens": mean([e.get("input_tokens", 0) for e in all_events]),
        "mean_event_output_tokens": mean([e.get("output_tokens", 0) for e in all_events]),
        "mean_event_latency_ms": mean([e.get("latency_ms", 0.0) for e in all_events]),
        "mean_mod_input_tokens": mean([m.get("input_tokens", 0) for m in all_mods]),
        "mean_mod_output_tokens": mean([m.get("output_tokens", 0) for m in all_mods]),
        "mean_mod_latency_ms": mean([m.get("latency_ms", 0.0) for m in all_mods]),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    eval_path: Path = args.eval
    tc_path: Path = args.samples

    if not eval_path.exists():
        print(f"Error: eval file not found: {eval_path}", file=sys.stderr)
        sys.exit(1)
    if not tc_path.exists():
        print(f"Error: test cases file not found: {tc_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Building role map from {tc_path} ...")
    role_map = _build_role_map(tc_path)
    print(f"  → {len(role_map)} test cases indexed")

    # Parse the eval file
    lines_raw = eval_path.read_text().splitlines()
    run_config_line: Optional[str] = None
    tc_result_lines: list[str] = []
    summary_line: Optional[str] = None

    for line in lines_raw:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        rt = data.get("record_type", "")
        if rt == "run_config":
            run_config_line = line
        elif rt == "eval_summary":
            summary_line = line  # will be replaced
        elif "tc_id" in data and "run_index" in data and "events" in data:
            tc_result_lines.append(line)

    print(f"Loaded {len(tc_result_lines)} run records from {eval_path}")

    # Apply roles
    classified = 0
    already_set = 0
    skipped_no_tc = 0
    updated_records: list[dict] = []

    for line in tc_result_lines:
        rec = json.loads(line)
        tc_id = rec["tc_id"]
        tc_roles = role_map.get(tc_id, {})
        if not tc_roles:
            skipped_no_tc += 1

        for evt in rec.get("events", []):
            eid = evt.get("event_id", "")
            if _STEP_RE.match(eid):
                continue  # steps have no role
            role = tc_roles.get(eid)
            if role is not None:
                if evt.get("role") is not None:
                    already_set += 1
                evt["role"] = role
                classified += 1

        updated_records.append(rec)

    print(f"  Events classified:   {classified}")
    print(f"  Already had role:    {already_set}")
    print(f"  TCs not in map:      {skipped_no_tc}")

    # Recompute summary
    new_summary = _recompute_summary(updated_records)

    # Backup original
    backup_path = eval_path.with_suffix(".jsonl.orig_roles")
    if not backup_path.exists():
        import shutil
        shutil.copy2(eval_path, backup_path)
        print(f"Backup: {backup_path}")
    else:
        print(f"Backup already exists, skipping: {backup_path}")

    # Write updated file
    with open(eval_path, "w") as f:
        if run_config_line:
            f.write(run_config_line + "\n")
        for rec in updated_records:
            f.write(json.dumps(rec) + "\n")
        f.write(json.dumps(new_summary) + "\n")

    print(f"\nUpdated: {eval_path}")
    print(f"Summary:")
    print(f"  steps_pass_rate:     {new_summary.get('steps_pass_rate')}")
    print(f"  pre_mod_pass_rate:   {new_summary.get('pre_mod_pass_rate')}")
    print(f"  post_mod_pass_rate:  {new_summary.get('post_mod_pass_rate')}")
    print(f"  irrelevant_pass_rate:{new_summary.get('irrelevant_pass_rate')}")
    print(f"  inconclusive_tcs:    {new_summary.get('inconclusive_tcs')}")
    print(f"  mean_pass_rate:      {new_summary.get('mean_pass_rate'):.4f}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Retroactively classify event roles in an eval file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.data.retroactive_classify \\
      --eval outputs/data/zapier/20260407_zapier_clean/runs/test_cases_eval_20260410_113327.jsonl \\
      --samples outputs/data/zapier/20260407_zapier_clean/samples.jsonl
""",
    )
    parser.add_argument("--eval", type=Path, required=True, metavar="JSONL",
                        help="Path to _eval.jsonl file to update in-place")
    parser.add_argument("--samples", type=Path, required=True, metavar="JSONL",
                        help="Path to samples.jsonl with event timing and role data")
    return parser


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
