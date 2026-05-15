#!/usr/bin/env python3
"""Conditioned probe accuracy analysis for state-probe experiments.

Computes three probe accuracy metrics:
  raw          — all probe questions (role="post_mod")
  specific     — only specific-entity probes (exclude aggregative)
  conditioned  — specific probes, only for TCs where ALL state events passed

Aggregative probes ("which entities are active?", "how many X?") are excluded
from the 'specific' metric because they compound errors across all prior events.
Specific-entity probes ("what is the status of Y?", "is Z still active?") depend
only on events for that entity, so they're the cleaner signal.

State-OK classification uses the judged pass/fail of irrelevant (state-mutating)
events in the eval results. Requires TCs generated with expect on state events.

Usage:
    python scripts/analyze_state_probes.py \\
        --tcs outputs/.../test_cases_state_probes.jsonl \\
        --lnl outputs/.../test_cases_state_probes_lnl.jsonl \\
        --baseline outputs/.../test_cases_state_probes_baseline.jsonl \\
        [--output conditioned_results.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

DEPTH_RE   = re.compile(r"-probe-D(\d+)-TC")
FIDELITY_RE = re.compile(r"-sfid-D(\d+)-C(\d+)-TC")

# Entity patterns: if a probe question references a specific entity, it's specific (not aggregative)
_ENTITY_PATTERNS = [
    re.compile(r"https?://[^\s,>\]'\")]+"),        # URLs
    re.compile(r"\b[A-Z]+-\d+\b"),                  # INC-003, T-042, DEAL-217
    re.compile(r"#\d+"),                             # #4217
    re.compile(r'"([A-Z][a-z]+ [A-Z][a-z]+)"'),    # "Priya Nair" (quoted full names)
    re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b"),    # Priya Nair (unquoted full names)
]

# Aggregative question starters
_AGGREGATIVE_STARTS = re.compile(
    r"^\s*(which|what are|list|name all|how many|how often|what (is the total|count)|"
    r"are there any|give me all|show all)",
    re.IGNORECASE,
)

# ── Probe classification ──────────────────────────────────────────────────────

def _is_aggregative(probe_input: str) -> bool:
    """Return True if the probe asks about multiple entities (aggregative).

    A probe is specific if it contains a concrete entity identifier (URL, ID, name).
    A probe is aggregative if it asks about a set or count without naming a specific entity.
    """
    for pat in _ENTITY_PATTERNS:
        if pat.search(probe_input):
            return False
    if _AGGREGATIVE_STARTS.match(probe_input):
        return True
    return True


# ── Loading ────────────────────────────────────────────────────────────────────

def load_tcs(path: Path) -> dict[str, dict]:
    tcs = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            tcs[d["id"]] = d
    return tcs


def load_results(path: Path) -> dict[str, dict]:
    results = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if "tc_id" not in d:
                continue
            results[d["tc_id"]] = d
    return results


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze_fidelity(
    tcs: dict[str, dict],
    results: dict[str, dict],
) -> dict[tuple[int, int], dict]:
    """Like analyze() but groups by (n_c, depth) from sfid TC IDs.

    Returns {(n_c, depth): metrics} so depth is the x-axis and n_c selects the line.
    """
    by_cell: dict[tuple[int, int], list] = defaultdict(list)

    for tc_id, result in results.items():
        m = FIDELITY_RE.search(tc_id)
        if not m:
            continue
        depth, n_c = int(m.group(1)), int(m.group(2))

        tc = tcs.get(tc_id)
        if not tc:
            continue

        events = result.get("events", [])
        probe_events     = [e for e in events if e.get("role") == "post_mod"]
        state_ev_results = [e for e in events if e.get("role") == "irrelevant"]
        if not probe_events:
            continue

        state_pass_map = {
            e.get("event_id"): e.get("passed", False)
            for e in state_ev_results
            if e.get("event_id")
        }

        if state_ev_results:
            tc_state_ok = all(e.get("passed", False) for e in state_ev_results)
            n_state_judged = len(state_ev_results)
        else:
            tc_state_ok = None
            n_state_judged = 0

        tc_event_by_id = {te.get("id"): te for te in tc.get("events", [])}

        raw_pass = raw_total = 0
        cond_pass = cond_total = 0
        any_probe_has_depends = False

        for e in probe_events:
            passed = e.get("passed", False)
            tc_evt = tc_event_by_id.get(e.get("event_id")) or {}
            depends_on = tc_evt.get("depends_on") or []

            raw_pass  += 1 if passed else 0
            raw_total += 1

            if depends_on:
                any_probe_has_depends = True
                if state_pass_map:
                    deps_met = all(state_pass_map.get(d, False) for d in depends_on)
                    if deps_met:
                        cond_pass  += 1 if passed else 0
                        cond_total += 1
            elif tc_state_ok is True:
                cond_pass  += 1 if passed else 0
                cond_total += 1

        by_cell[(n_c, depth)].append({
            "tc_id": tc_id,
            "tc_state_ok": tc_state_ok,
            "n_state_judged": n_state_judged,
            "any_probe_has_depends": any_probe_has_depends,
            "raw_pass": raw_pass,
            "raw_total": raw_total,
            "cond_pass": cond_pass,
            "cond_total": cond_total,
        })

    metrics: dict[tuple[int, int], dict] = {}
    for cell, records in sorted(by_cell.items()):
        def _rate(pass_key, total_key, _records=records):
            p = sum(r[pass_key] for r in _records)
            t = sum(r[total_key] for r in _records)
            return p / t if t else None

        judged = [r for r in records if r["tc_state_ok"] is not None]
        tc_state_ok_rate = (
            sum(1 for r in judged if r["tc_state_ok"]) / len(judged) if judged else None
        )

        metrics[cell] = {
            "raw_accuracy":         _rate("raw_pass",  "raw_total"),
            "conditioned":          _rate("cond_pass", "cond_total"),
            "tc_state_ok_rate":     tc_state_ok_rate,
            "n_tcs":                len(records),
            "n_tcs_with_depends":   sum(1 for r in records if r["any_probe_has_depends"]),
            "n_total_probes":       sum(r["raw_total"]  for r in records),
            "n_conditioned_probes": sum(r["cond_total"] for r in records),
        }
    return metrics


def print_table_fidelity(lnl: dict, base: dict) -> None:
    """Print fidelity metrics table. Keys are (n_c, depth)."""
    cells = sorted(set(lnl) | set(base))
    def pct(v): return f"{v:.1%}" if v is not None else "  N/A"
    def cnt(v): return str(v) if v is not None else "N/A"

    print(f"\n{'':>12}  {'──────────── LNL ────────────':^36}  {'────────── Baseline ──────────':^36}")
    print(f"{'C  Depth':>12}  {'raw':>7}  {'cond':>7}  {'n-cond':>7}  {'tc-ok%':>7}  "
          f"{'raw':>7}  {'cond':>7}  {'n-cond':>7}  {'tc-ok%':>7}")
    print("─" * 101)
    prev_nc = None
    for (n_c, depth) in cells:
        if n_c != prev_nc:
            if prev_nc is not None:
                print()
            prev_nc = n_c
        lm = lnl.get((n_c, depth), {})
        bm = base.get((n_c, depth), {})
        print(
            f"  C={n_c:>2} D={depth:>2}    "
            f"{pct(lm.get('raw_accuracy')):>7}  "
            f"{pct(lm.get('conditioned')):>7}  "
            f"{cnt(lm.get('n_conditioned_probes')):>7}  "
            f"{pct(lm.get('tc_state_ok_rate')):>7}  "
            f"{pct(bm.get('raw_accuracy')):>7}  "
            f"{pct(bm.get('conditioned')):>7}  "
            f"{cnt(bm.get('n_conditioned_probes')):>7}  "
            f"{pct(bm.get('tc_state_ok_rate')):>7}"
        )
    print()
    print("C     = n_corrections — selects the line in the plot")
    print("Depth = total input events in the batch — x-axis")
    print("raw   = all probes (per-event pass/fail from judge)")
    print("cond  = probe counted only if all events in its depends_on passed")
    print("tc-ok% = fraction of TCs where ALL state events passed")


def analyze(
    tcs: dict[str, dict],
    results: dict[str, dict],
) -> dict[int, dict]:
    by_depth: dict[int, list] = defaultdict(list)

    for tc_id, result in results.items():
        m = DEPTH_RE.search(tc_id)
        if not m:
            continue
        depth = int(m.group(1))

        tc = tcs.get(tc_id)
        if not tc:
            continue

        events = result.get("events", [])
        probe_events    = [e for e in events if e.get("role") == "post_mod"]
        state_ev_results = [e for e in events if e.get("role") == "irrelevant"]
        if not probe_events:
            continue

        # Build pass map from state-event records: {event_id: passed_bool}
        state_pass_map = {
            e.get("event_id"): e.get("passed", False)
            for e in state_ev_results
            if e.get("event_id")
        }

        # TC-level fallback (legacy TCs without depends_on on probes)
        if state_ev_results:
            tc_state_ok = all(e.get("passed", False) for e in state_ev_results)
            n_state_judged = len(state_ev_results)
        else:
            tc_state_ok = None
            n_state_judged = 0

        # Look up each probe's depends_on from the TC definition
        tc_event_by_id = {te.get("id"): te for te in tc.get("events", [])}

        raw_pass = raw_total = 0
        cond_pass = cond_total = 0
        any_probe_has_depends = False

        for e in probe_events:
            passed = e.get("passed", False)
            tc_evt = tc_event_by_id.get(e.get("event_id")) or {}
            depends_on = tc_evt.get("depends_on") or []

            raw_pass  += 1 if passed else 0
            raw_total += 1

            # Per-probe conditioning via depends_on
            if depends_on:
                any_probe_has_depends = True
                if state_pass_map:
                    deps_met = all(state_pass_map.get(d, False) for d in depends_on)
                    if deps_met:
                        cond_pass  += 1 if passed else 0
                        cond_total += 1
            elif tc_state_ok is True:
                # Legacy fallback: TC-level conditioning
                cond_pass  += 1 if passed else 0
                cond_total += 1

        by_depth[depth].append({
            "tc_id":               tc_id,
            "tc_state_ok":         tc_state_ok,
            "n_state_judged":      n_state_judged,
            "any_probe_has_depends": any_probe_has_depends,
            "raw_pass":            raw_pass,
            "raw_total":           raw_total,
            "cond_pass":           cond_pass,
            "cond_total":          cond_total,
        })

    metrics: dict[int, dict] = {}
    for depth, records in sorted(by_depth.items()):
        def _rate(pass_key, total_key):
            p = sum(r[pass_key] for r in records)
            t = sum(r[total_key] for r in records)
            return p / t if t else None

        judged = [r for r in records if r["tc_state_ok"] is not None]
        tc_state_ok_rate = (
            sum(1 for r in judged if r["tc_state_ok"]) / len(judged) if judged else None
        )

        metrics[depth] = {
            "raw_accuracy":         _rate("raw_pass",  "raw_total"),
            "conditioned":          _rate("cond_pass", "cond_total"),
            "tc_state_ok_rate":     tc_state_ok_rate,
            "n_tcs":                len(records),
            "n_tcs_with_depends":   sum(1 for r in records if r["any_probe_has_depends"]),
            "n_total_probes":       sum(r["raw_total"]  for r in records),
            "n_conditioned_probes": sum(r["cond_total"] for r in records),
        }
    return metrics


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_table(lnl: dict, base: dict) -> None:
    depths = sorted(set(lnl) | set(base))
    def pct(v): return f"{v:.1%}" if v is not None else "  N/A"
    def cnt(v): return str(v) if v is not None else "N/A"

    print(f"\n{'':>6}  {'──────────── LNL ────────────':^36}  {'────────── Baseline ──────────':^36}")
    print(f"{'Depth':>6}  {'raw':>7}  {'cond':>7}  {'n-cond':>7}  {'tc-ok%':>7}  "
          f"{'raw':>7}  {'cond':>7}  {'n-cond':>7}  {'tc-ok%':>7}")
    print("─" * 95)
    for d in depths:
        lm = lnl.get(d, {})
        bm = base.get(d, {})
        print(
            f"  D={d:>2}  "
            f"{pct(lm.get('raw_accuracy')):>7}  "
            f"{pct(lm.get('conditioned')):>7}  "
            f"{cnt(lm.get('n_conditioned_probes')):>7}  "
            f"{pct(lm.get('tc_state_ok_rate')):>7}  "
            f"{pct(bm.get('raw_accuracy')):>7}  "
            f"{pct(bm.get('conditioned')):>7}  "
            f"{cnt(bm.get('n_conditioned_probes')):>7}  "
            f"{pct(bm.get('tc_state_ok_rate')):>7}"
        )
    print()
    print("raw     = all probes (per-event pass/fail from judge)")
    print("cond    = per-probe conditioning: probe counted only if all events in its depends_on passed.")
    print("          Falls back to TC-level (all state events passed) for legacy TCs without depends_on.")
    print("n-cond  = number of probes included in cond")
    print("tc-ok%  = fraction of TCs where ALL state events passed (legacy TC-level diagnostic)")


def save_json(lnl: dict, base: dict, path: Path) -> None:
    out = {
        "lnl":      {str(k): v for k, v in lnl.items()},
        "baseline": {str(k): v for k, v in base.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2))
    print(f"Saved: {path}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tcs",      type=Path, required=True)
    parser.add_argument("--lnl",      type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Save per-depth metrics as JSON")
    parser.add_argument("--mode", choices=["probe", "fidelity"], default="probe",
                        help="Analysis mode: 'probe' groups by depth (default), "
                             "'fidelity' groups by (n_transitions, n_corrections)")
    args = parser.parse_args()

    for p in (args.tcs, args.lnl, args.baseline):
        if not p.exists():
            print(f"File not found: {p}", file=sys.stderr)
            sys.exit(1)

    print(f"Loading TCs:              {args.tcs}")
    tcs = load_tcs(args.tcs)
    print(f"  {len(tcs)} test cases")

    print(f"Loading LNL results:      {args.lnl}")
    lnl_results = load_results(args.lnl)
    print(f"  {len(lnl_results)} results")

    print(f"Loading baseline results: {args.baseline}")
    base_results = load_results(args.baseline)
    print(f"  {len(base_results)} results")

    print("\nAnalyzing ...")
    if args.mode == "fidelity":
        lnl_m  = analyze_fidelity(tcs, lnl_results)
        base_m = analyze_fidelity(tcs, base_results)
        print_table_fidelity(lnl_m, base_m)
    else:
        lnl_m  = analyze(tcs, lnl_results)
        base_m = analyze(tcs, base_results)
        print_table(lnl_m, base_m)

    if args.output:
        save_json(lnl_m, base_m, args.output)
        print(f"\nPass --conditioned {args.output} to plot_state_probes.py to overlay these metrics.")


if __name__ == "__main__":
    main()
