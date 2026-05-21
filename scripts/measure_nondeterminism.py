"""Measure LLM non-determinism at T=0 across identical re-runs of the same TCs.

Reports two metrics:
  - Workflow Instability Rate (WIR): % of TCs whose binary event outcomes
    are not identical across all runs (any step flips → unstable TC).
  - Step-Level Shannon Entropy: per-event entropy across runs, treating
    each (tc_id, event_id) as a Bernoulli trial sample. H=0 → deterministic;
    H=1 → maximally uncertain (50/50 split).

Usage:
    python scripts/measure_nondeterminism.py <results.jsonl> [results2.jsonl ...]

Input: an evaluate.py / evaluate_baseline.py results JSONL containing
multiple run_index values per tc_id (i.e., run with --runs N where N≥2).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path


def _entropy_bernoulli(p: float) -> float:
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return -(p * math.log2(p) + (1 - p) * math.log2(1 - p))


def analyze(path: Path) -> dict:
    # outcomes[(tc_id, event_id)] = list of bool outcomes across runs
    outcomes: dict[tuple[str, str], list[bool]] = defaultdict(list)
    # runs_per_tc[tc_id] = set of run_index seen
    runs_per_tc: dict[str, set] = defaultdict(set)
    # TCs with any infra/timeout run — excluded from entropy (uniform R required)
    infra_tainted: set[str] = set()

    # First pass: tag infra-tainted TCs
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "tc_id" in d and d.get("error_type") in ("infra", "timeout"):
                infra_tainted.add(d["tc_id"])

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "tc_id" not in d:
                continue
            if d.get("error_type") in ("infra", "timeout"):
                continue
            tc_id = d["tc_id"]
            run   = d.get("run_index", 0)
            runs_per_tc[tc_id].add(run)
            if tc_id in infra_tainted:
                continue
            for ev in d.get("events") or []:
                eid = ev.get("event_id")
                passed = ev.get("passed")
                if eid is None or passed is None:
                    continue
                outcomes[(tc_id, eid)].append(bool(passed))

    # Eligible: ≥2 clean runs AND not infra-tainted (so R is uniform per TC).
    eligible_tcs = {
        tc for tc, runs in runs_per_tc.items()
        if tc not in infra_tainted and len(runs) >= 2
    }
    n_multi = len(eligible_tcs)
    clean_runs = {len(runs_per_tc[tc]) for tc in eligible_tcs}
    uniform_R  = next(iter(clean_runs)) if len(clean_runs) == 1 else None

    # Workflow Instability: TC is unstable if any event group has mixed outcomes
    unstable_tcs: set[str] = set()
    for (tc_id, _eid), vals in outcomes.items():
        if tc_id not in eligible_tcs:
            continue
        if len(set(vals)) > 1:
            unstable_tcs.add(tc_id)
    wir = (len(unstable_tcs) / n_multi) if n_multi else 0.0

    # Step entropy with Miller-Madow correction (binary alphabet, +1/(2R) bias).
    step_entropies: list[float] = []
    flipped_steps = 0
    total_steps = 0
    for (tc_id, _eid), vals in outcomes.items():
        if tc_id not in eligible_tcs or len(vals) < 2:
            continue
        total_steps += 1
        R = len(vals)
        p = sum(vals) / R
        h = _entropy_bernoulli(p) + 1.0 / (2 * R)
        step_entropies.append(min(h, 1.0))
        if 0.0 < p < 1.0:
            flipped_steps += 1

    h_mean   = (sum(step_entropies) / total_steps) if total_steps else 0.0
    h_max    = max(step_entropies) if step_entropies else 0.0
    flip_pct = (flipped_steps / total_steps) if total_steps else 0.0

    return {
        "n_tcs_total":            len(runs_per_tc),
        "n_tcs_infra_excluded":   len(infra_tainted),
        "n_tcs_eligible":         n_multi,
        "max_runs_per_tc":        max((len(r) for r in runs_per_tc.values()), default=0),
        "uniform_R":              uniform_R,
        "n_unstable_tcs":         len(unstable_tcs),
        "workflow_instability":   wir,
        "n_step_groups":          total_steps,
        "n_flipped_step_groups":  flipped_steps,
        "frac_flipped_steps":     flip_pct,
        "step_entropy_mean":      h_mean,
        "step_entropy_max":       h_max,
    }


def _print_report(path: Path, m: dict) -> None:
    print(f"\n{path}")
    print(f"  TCs total            : {m['n_tcs_total']}")
    print(f"  TCs infra-excluded   : {m['n_tcs_infra_excluded']}")
    print(f"  TCs eligible (≥2 clean runs, no infra): "
          f"{m['n_tcs_eligible']}  (max runs/TC: {m['max_runs_per_tc']})")
    if m['uniform_R']:
        print(f"  R per TC (uniform)   : {m['uniform_R']}")
    else:
        print(f"  R per TC             : MIXED (entropy comparison may be biased)")
    if m["n_tcs_eligible"] == 0:
        print("  (skipping — need multi-run data; re-run with --runs N≥2)")
        return
    print(f"  Unstable TCs         : {m['n_unstable_tcs']}")
    print(f"  Workflow Instability : {m['workflow_instability']:.1%}  "
          f"({m['n_unstable_tcs']}/{m['n_tcs_eligible']})")
    print(f"  Step groups          : {m['n_step_groups']}")
    print(f"  Flipped step groups  : {m['n_flipped_step_groups']}  "
          f"({m['frac_flipped_steps']:.1%})")
    print(f"  Mean step entropy H̄ : {m['step_entropy_mean']:.4f}  "
          f"(max {m['step_entropy_max']:.3f}, Miller-Madow corrected)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("results", type=Path, nargs="+",
                   help="Results JSONL file(s) from evaluate.py / evaluate_baseline.py")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON instead of a table")
    args = p.parse_args()

    out = {}
    for path in args.results:
        if not path.exists():
            print(f"File not found: {path}", file=sys.stderr)
            sys.exit(1)
        m = analyze(path)
        out[str(path)] = m
        if not args.json:
            _print_report(path, m)

    if args.json:
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
