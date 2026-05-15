#!/usr/bin/env python3
"""Per-event pass rate and conditional probe accuracy for probe-dataset experiments.

Metrics reported per (system, depth, probe_type):
  tracked_pass_rate          — fraction of tracked events (role=irrelevant, expect set) that passed
  conditional_probe_accuracy — among included probes, fraction answered correctly
  inclusion_rate             — fraction of probes where all dependency events passed

A probe is INCLUDED if every event in its `depends_on` list has `passed=True`.

Usage:
    python scripts/analyze_probe_dataset.py \\
        --tcs outputs/.../probe_dataset.jsonl \\
        --lnl outputs/.../probes_lnl.jsonl \\
        --baseline outputs/.../probes_baseline.jsonl \\
        [--output results.json] [--plots plots/]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

PROBE2_RE = re.compile(r"-probe2-D(\d+)-S(\d+)-TC")
PROBE_TYPES = ["direct_lookup", "aggregate", "conditional_aggregate", "retraction_status"]


# ── Loading ────────────────────────────────────────────────────────────────────

def load_tcs(path: Path) -> dict[str, dict]:
    tcs: dict[str, dict] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if "id" in d:
                tcs[d["id"]] = d
    return tcs


def load_results(path: Path) -> dict[str, list[dict]]:
    """Load eval results JSONL → {tc_id → list of {event_id → EventResult dict} per run}."""
    results: dict[str, list[dict[str, dict]]] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            tc_id = d.get("tc_id")
            if not tc_id:
                continue
            if "timeout" in (d.get("error_type") or "").lower():
                continue
            run_ev = {er["event_id"]: er for er in d.get("events", [])}
            results.setdefault(tc_id, []).append(run_ev)
    return results


# ── Classification ─────────────────────────────────────────────────────────────

def _parse_depth(tc_id: str) -> int | None:
    m = PROBE2_RE.search(tc_id)
    return int(m.group(1)) if m else None


def _tracked_event_kind(ev: dict) -> str:
    """Classify a tracked (role=irrelevant, expect set) event by its lifecycle role."""
    expect = (ev.get("expect") or {}).get("action", "").lower()
    inp = (ev.get("input") or "").lower()
    if "updated to" in expect or "corrected to" in expect or "revised to" in expect:
        return "modification"
    if "correction" in inp or "revised" in inp or "amend" in inp:
        return "modification"
    if "created with" in expect or "entity created" in expect:
        return "creation"
    if "transitioned to" in expect:
        return "transition"
    if "deleted" in expect or "retracted" in expect or "cancelled" in expect or "withdrawn" in expect:
        return "retraction"
    return "other"


def _probe_type_of(ev: dict) -> str:
    """Read probe_type from expect.reason (set by the generator)."""
    reason = (ev.get("expect") or {}).get("reason", "")
    if reason in PROBE_TYPES:
        return reason
    # Fallback: classify from question text
    q = (ev.get("input") or "").lower()
    if re.search(r"\b(still active|still tracked|still being tracked|still open|retracted|cancelled|canceled|withdrawn|deleted|closed|removed)\b", q):
        return "retraction_status"
    if re.search(r"\bwhich\b.*\b(that|where|with|above|below|more than|less than|over|under)\b", q):
        return "conditional_aggregate"
    if re.search(r"\b(how many|count|number of).*\b(above|below|more than|less than)\b", q):
        return "conditional_aggregate"
    if re.search(r"\b(total|sum|how many|count|number of|combined)\b", q):
        return "aggregate"
    return "direct_lookup"


# ── Core analysis ──────────────────────────────────────────────────────────────

def analyze(tcs: dict[str, dict], results: dict[str, list[dict]], system: str) -> list[dict]:
    Cell = lambda: {"tracked_total": 0, "tracked_passed": 0,
                    "probes_total": 0, "probes_included": 0, "probes_correct": 0}
    cells: dict[tuple, dict] = defaultdict(Cell)

    for tc_id, tc in tcs.items():
        if not PROBE2_RE.search(tc_id):
            continue
        depth = _parse_depth(tc_id)
        if depth is None:
            continue
        tc_run_results = results.get(tc_id, [])
        if not tc_run_results:
            continue

        events = tc.get("events", [])

        # Each run is an independent observation — loop over all runs per TC.

        # Tracked events: role=irrelevant with expect set
        for ev in events:
            if ev.get("role") == "irrelevant" and ev.get("expect"):
                for ev_results in tc_run_results:
                    er = ev_results.get(ev["id"])
                    if er is None:
                        continue
                    passed = er.get("passed", False)
                    cells[(depth, "_tracked")]["tracked_total"] += 1
                    if passed:
                        cells[(depth, "_tracked")]["tracked_passed"] += 1
                    kind = _tracked_event_kind(ev)
                    k = (depth, f"_tracked_{kind}")
                    cells[k]["tracked_total"] += 1
                    if passed:
                        cells[k]["tracked_passed"] += 1

        # Probe events: role=post_mod with depends_on
        for ev in events:
            if ev.get("role") != "post_mod" or not ev.get("depends_on"):
                continue
            ptype = _probe_type_of(ev)
            key = (depth, ptype)
            cells[key].setdefault("probes_correct_uncond", 0)
            for ev_results in tc_run_results:
                cells[key]["probes_total"] += 1

                dep_results = [ev_results.get(dep) for dep in ev["depends_on"]]
                included = all(er is not None and er.get("passed", False) for er in dep_results)
                per = ev_results.get(ev["id"])
                probe_passed = bool(per and per.get("passed", False))
                if probe_passed:
                    cells[key]["probes_correct_uncond"] += 1
                if included:
                    cells[key]["probes_included"] += 1
                    if probe_passed:
                        cells[key]["probes_correct"] += 1

    rows = []

    # Tracked pass rate (one row per depth, plus rows per kind)
    tracked_keys = {key for (_, key) in cells.keys() if key.startswith("_tracked")}
    for tkey in sorted(tracked_keys):
        per_depth: dict[int, dict] = defaultdict(lambda: {"t": 0, "p": 0})
        for (depth, key), c in cells.items():
            if key == tkey:
                per_depth[depth]["t"] += c["tracked_total"]
                per_depth[depth]["p"] += c["tracked_passed"]
        for depth, d in sorted(per_depth.items()):
            rows.append({
                "system": system, "depth": depth, "probe_type": tkey,
                "tracked_pass_rate": d["p"] / d["t"] if d["t"] else None,
                "n_tracked": d["t"], "n_tracked_passed": d["p"],
            })

    # Probe accuracy (one row per depth × probe_type)
    for (depth, ptype), c in sorted(cells.items()):
        if ptype == "_tracked":
            continue
        rows.append({
            "system": system, "depth": depth, "probe_type": ptype,
            "conditional_probe_accuracy": c["probes_correct"] / c["probes_included"] if c["probes_included"] else None,
            "unconditional_probe_accuracy": c.get("probes_correct_uncond", 0) / c["probes_total"] if c["probes_total"] else None,
            "inclusion_rate": c["probes_included"] / c["probes_total"] if c["probes_total"] else None,
            "n_probes_total": c["probes_total"],
            "n_probes_included": c["probes_included"],
            "n_probes_correct": c["probes_correct"],
            "n_probes_correct_uncond": c.get("probes_correct_uncond", 0),
        })

    return rows


# ── Table printing ─────────────────────────────────────────────────────────────

def _pct(v) -> str:
    return f"{v*100:5.1f}%" if v is not None else "  N/A "


def _lookup(rows: list[dict], system: str, depth: int, ptype: str) -> dict | None:
    return next((r for r in rows if r["system"] == system
                 and r["depth"] == depth and r["probe_type"] == ptype), None)


def print_table(all_rows: list[dict]) -> None:
    systems = sorted({r["system"] for r in all_rows})
    depths  = sorted({r["depth"]  for r in all_rows})
    ptypes  = [pt for pt in PROBE_TYPES if any(r["probe_type"] == pt for r in all_rows)]

    sys_a, sys_b = (systems[0], systems[1]) if len(systems) >= 2 else (systems[0], None)

    def _col_header(s): return f"{s:^28}"
    def _divider(w): return "─" * w

    # ── Tracked events: overall and per kind (modifications/creation/transition) ─
    tracked_kinds = [
        ("_tracked", "TRACKED EVENT RETENTION (all kinds)"),
        ("_tracked_modification", "MODIFICATION RETENTION (corrections / updates)"),
        ("_tracked_creation", "CREATION RETENTION"),
        ("_tracked_transition", "TRANSITION RETENTION"),
        ("_tracked_retraction", "RETRACTION RETENTION"),
    ]
    for tkey, title in tracked_kinds:
        if not any(r["probe_type"] == tkey for r in all_rows):
            continue
        print("\n┌" + _divider(70) + "┐")
        print(f"│{title:^70}│")
        print("├" + _divider(70) + "┤")
        if sys_b:
            print(f"│{'Depth':>6}  {sys_a:^14}  {sys_a+' n':^8}  "
                  f"{sys_b:^14}  {sys_b+' n':^8}  {'Δ (B−A)':^10}│")
        else:
            print(f"│{'Depth':>6}  {sys_a:^14}  {sys_a+' n':^8}│")
        print("├" + _divider(70) + "┤")
        for depth in depths:
            ra = _lookup(all_rows, sys_a, depth, tkey)
            va = ra["tracked_pass_rate"] if ra else None
            na = ra["n_tracked"] if ra else None
            line = f"│{depth:>6}  {_pct(va):^14}  {(str(na) if na is not None else '-'):^8}"
            if sys_b:
                rb = _lookup(all_rows, sys_b, depth, tkey)
                vb = rb["tracked_pass_rate"] if rb else None
                nb = rb["n_tracked"] if rb else None
                delta = (vb - va) if (va is not None and vb is not None) else None
                line += f"  {_pct(vb):^14}  {(str(nb) if nb is not None else '-'):^8}  {_pct(delta):^10}"
            line += "│"
            print(line)
        print("└" + _divider(70) + "┘")

    # ── Probe accuracy per type (conditional, gated by inclusion) ───────────────
    for ptype in ptypes:
        label = ptype.replace("_", " ").title()
        print(f"\n┌" + _divider(90) + "┐")
        print(f"│{('PROBE ACCURACY (conditional, included only) — ' + label):^90}│")
        print("├" + _divider(90) + "┤")
        if sys_b:
            print(f"│{'Depth':>6}  {sys_a+' acc':^14}  {sys_a+' incl':^12}  "
                  f"{sys_b+' acc':^14}  {sys_b+' incl':^12}  {'Δ acc':^8}│")
        else:
            print(f"│{'Depth':>6}  {'acc':^14}  {'incl':^12}│")
        print("├" + _divider(90) + "┤")
        for depth in depths:
            ra = _lookup(all_rows, sys_a, depth, ptype)
            acc_a = ra["conditional_probe_accuracy"] if ra else None
            inc_a = ra["inclusion_rate"] if ra else None
            line = f"│{depth:>6}  {_pct(acc_a):^14}  {_pct(inc_a):^12}"
            if sys_b:
                rb = _lookup(all_rows, sys_b, depth, ptype)
                acc_b = rb["conditional_probe_accuracy"] if rb else None
                inc_b = rb["inclusion_rate"] if rb else None
                delta = (acc_b - acc_a) if (acc_a is not None and acc_b is not None) else None
                line += f"  {_pct(acc_b):^14}  {_pct(inc_b):^12}  {_pct(delta):^8}"
            line += "│"
            print(line)
        print("└" + _divider(90) + "┘")

    # ── Probe accuracy per type (unconditional, all probes, no gating) ──────────
    for ptype in ptypes:
        label = ptype.replace("_", " ").title()
        print(f"\n┌" + _divider(90) + "┐")
        print(f"│{('PROBE ACCURACY (unconditional, all probes) — ' + label):^90}│")
        print("├" + _divider(90) + "┤")
        if sys_b:
            print(f"│{'Depth':>6}  {sys_a+' acc':^14}  {sys_a+' n':^12}  "
                  f"{sys_b+' acc':^14}  {sys_b+' n':^12}  {'Δ acc':^8}│")
        else:
            print(f"│{'Depth':>6}  {'acc':^14}  {'n':^12}│")
        print("├" + _divider(90) + "┤")
        for depth in depths:
            ra = _lookup(all_rows, sys_a, depth, ptype)
            acc_a = ra["unconditional_probe_accuracy"] if ra else None
            n_a = ra["n_probes_total"] if ra else None
            line = f"│{depth:>6}  {_pct(acc_a):^14}  {(str(n_a) if n_a is not None else '-'):^12}"
            if sys_b:
                rb = _lookup(all_rows, sys_b, depth, ptype)
                acc_b = rb["unconditional_probe_accuracy"] if rb else None
                n_b = rb["n_probes_total"] if rb else None
                delta = (acc_b - acc_a) if (acc_a is not None and acc_b is not None) else None
                line += f"  {_pct(acc_b):^14}  {(str(n_b) if n_b is not None else '-'):^12}  {_pct(delta):^8}"
            line += "│"
            print(line)
        print("└" + _divider(90) + "┘")

    # ── Mixed view: LNL conditional, baseline unconditional ───────────────────
    if sys_b and "lnl" in systems and "baseline" in systems:
        for ptype in ptypes:
            label = ptype.replace("_", " ").title()
            print(f"\n┌" + _divider(90) + "┐")
            print(f"│{('PROBE ACCURACY (LNL conditional, baseline unconditional) — ' + label):^90}│")
            print("├" + _divider(90) + "┤")
            print(f"│{'Depth':>6}  {'lnl acc (cond)':^16}  {'lnl n':^10}  "
                  f"{'base acc (uncond)':^18}  {'base n':^10}  {'Δ (L−B)':^8}│")
            print("├" + _divider(90) + "┤")
            for depth in depths:
                rl = _lookup(all_rows, "lnl", depth, ptype)
                rb = _lookup(all_rows, "baseline", depth, ptype)
                acc_l = rl["conditional_probe_accuracy"] if rl else None
                n_l = rl["n_probes_included"] if rl else None
                acc_b = rb["unconditional_probe_accuracy"] if rb else None
                n_b = rb["n_probes_total"] if rb else None
                delta = (acc_l - acc_b) if (acc_l is not None and acc_b is not None) else None
                line = (f"│{depth:>6}  {_pct(acc_l):^16}  {(str(n_l) if n_l is not None else '-'):^10}  "
                        f"{_pct(acc_b):^18}  {(str(n_b) if n_b is not None else '-'):^10}  {_pct(delta):^8}│")
                print(line)
            print("└" + _divider(90) + "┘")


# ── Plots ─────────────────────────────────────────────────────────────────────

def make_plots(all_rows: list[dict], plots_dir: Path) -> None:
    try:
        import matplotlib.pyplot as plt
        import matplotlib.ticker as mticker
        import numpy as np
    except ImportError:
        print("WARNING: matplotlib not installed — skipping plots", file=sys.stderr)
        return

    plots_dir.mkdir(parents=True, exist_ok=True)
    systems = sorted({r["system"] for r in all_rows})
    depths  = sorted({r["depth"]  for r in all_rows})
    ptypes  = [pt for pt in PROBE_TYPES if any(r["probe_type"] == pt for r in all_rows)]

    COLORS  = {"lnl": "#2563eb", "baseline": "#dc2626"}
    MARKERS = {"lnl": "o", "baseline": "s"}

    def _vals(system, ptype, metric):
        return [
            (r[metric] if r and r.get(metric) is not None else float("nan"))
            for d in depths
            for r in [_lookup(all_rows, system, d, ptype)]
        ]

    # ── Plot 1: Tracked pass rate vs depth ─────────────────────────────────────
    fig, ax = plt.subplots(figsize=(7, 4))
    for sys in systems:
        ys = _vals(sys, "_tracked", "tracked_pass_rate")
        ax.plot(depths, [y * 100 for y in ys],
                marker=MARKERS.get(sys, "o"), color=COLORS.get(sys, "gray"),
                label=sys, linewidth=2, markersize=7)
    ax.set_xlabel("Event depth (D)")
    ax.set_ylabel("Tracked event pass rate (%)")
    ax.set_title("Tracked Event Retention vs. Depth")
    ax.set_xticks(depths)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter())
    ax.set_ylim(0, 105)
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    p = plots_dir / "tracked_pass_rate.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    print(f"  Saved {p}")

    # ── Plot 2: Conditional probe accuracy per type ────────────────────────────
    n_types = len(ptypes)
    fig, axes = plt.subplots(1, n_types, figsize=(5 * n_types, 4), sharey=True)
    if n_types == 1:
        axes = [axes]
    for ax, ptype in zip(axes, ptypes):
        for sys in systems:
            ys = _vals(sys, ptype, "conditional_probe_accuracy")
            ax.plot(depths, [y * 100 if not (y != y) else float("nan") for y in ys],
                    marker=MARKERS.get(sys, "o"), color=COLORS.get(sys, "gray"),
                    label=sys, linewidth=2, markersize=7)
        ax.set_title(ptype.replace("_", "\n"))
        ax.set_xlabel("Depth (D)")
        ax.set_xticks(depths)
        ax.yaxis.set_major_formatter(mticker.PercentFormatter())
        ax.set_ylim(0, 105)
        ax.grid(True, alpha=0.3)
        if ax is axes[0]:
            ax.set_ylabel("Conditional probe accuracy (%)")
        ax.legend(fontsize=8)
    fig.suptitle("Conditional Probe Accuracy vs. Depth", fontsize=12)
    fig.tight_layout()
    p = plots_dir / "probe_accuracy.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    print(f"  Saved {p}")

    # ── Plot 3: Inclusion rate vs depth ────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(7, 4))
    for sys in systems:
        for ptype, ls in zip(ptypes, ["-", "--", ":"]):
            ys = _vals(sys, ptype, "inclusion_rate")
            ax.plot(depths, [y * 100 if not (y != y) else float("nan") for y in ys],
                    linestyle=ls, marker=MARKERS.get(sys, "o"),
                    color=COLORS.get(sys, "gray"),
                    label=f"{sys}/{ptype.replace('_',' ')}", linewidth=1.5, markersize=5)
    ax.set_xlabel("Event depth (D)")
    ax.set_ylabel("Probe inclusion rate (%)")
    ax.set_title("Probe Inclusion Rate vs. Depth\n(all dependency events passed)")
    ax.set_xticks(depths)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter())
    ax.set_ylim(0, 105)
    ax.legend(fontsize=7, ncol=2)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    p = plots_dir / "inclusion_rate.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    print(f"  Saved {p}")

    # ── Plot 4: Combined summary bar chart ─────────────────────────────────────
    metrics = ["tracked_pass_rate"] + ["conditional_probe_accuracy"] * len(ptypes)
    ptypes_full = ["_tracked"] + ptypes
    labels = ["Tracked\nretention"] + [pt.replace("_", "\n") for pt in ptypes]

    x = np.arange(len(labels))
    width = 0.35
    fig, axes = plt.subplots(1, len(depths), figsize=(4 * len(depths), 5), sharey=True)
    if len(depths) == 1:
        axes = [axes]
    for ax, depth in zip(axes, depths):
        for i, sys in enumerate(systems):
            vals = []
            for ptype, metric in zip(ptypes_full, metrics):
                r = _lookup(all_rows, sys, depth, ptype)
                v = r.get(metric) if r else None
                vals.append((v or 0) * 100)
            offset = (i - (len(systems) - 1) / 2) * width
            bars = ax.bar(x + offset, vals, width, label=sys,
                          color=COLORS.get(sys, "gray"), alpha=0.85)
        ax.set_title(f"D={depth}")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=8)
        ax.set_ylim(0, 110)
        ax.yaxis.set_major_formatter(mticker.PercentFormatter())
        ax.grid(True, axis="y", alpha=0.3)
        if ax is axes[0]:
            ax.set_ylabel("%")
        ax.legend(fontsize=8)
    fig.suptitle("Per-Depth Summary: LNL vs Baseline", fontsize=12)
    fig.tight_layout()
    p = plots_dir / "summary_by_depth.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    print(f"  Saved {p}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze probe-dataset eval results: per-event pass rate and conditional probe accuracy."
    )
    parser.add_argument("--tcs", type=Path, required=True,
                        help="Probe-dataset TCs JSONL (generated by generate_probe_dataset_tcs.py)")
    parser.add_argument("--lnl", type=Path, default=None,
                        help="LNL eval results JSONL (from evaluate.py)")
    parser.add_argument("--baseline", type=Path, default=None,
                        help="OpenClaw baseline eval results JSONL (from evaluate_baseline.py)")
    parser.add_argument("--output", type=Path, default=None,
                        help="Output JSON file for raw metrics (default: none)")
    parser.add_argument("--plots", type=Path, default=None, metavar="DIR",
                        help="Directory to write PNG plots into")
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if not args.lnl and not args.baseline:
        print("ERROR: provide at least one of --lnl or --baseline", file=sys.stderr)
        sys.exit(1)

    print(f"Loading TCs from {args.tcs} …")
    tcs = load_tcs(args.tcs)
    print(f"  {len(tcs)} TCs loaded")

    all_rows: list[dict] = []

    if args.lnl:
        print(f"Loading LNL results from {args.lnl} …")
        r = load_results(args.lnl)
        n_runs = sum(len(v) for v in r.values())
        print(f"  {len(r)} TCs loaded ({n_runs} total run records)")
        all_rows.extend(analyze(tcs, r, "lnl"))

    if args.baseline:
        print(f"Loading baseline results from {args.baseline} …")
        r = load_results(args.baseline)
        n_runs = sum(len(v) for v in r.values())
        print(f"  {len(r)} TCs loaded ({n_runs} total run records)")
        all_rows.extend(analyze(tcs, r, "baseline"))

    print_table(all_rows)

    if args.plots:
        print(f"\nGenerating plots → {args.plots}/")
        make_plots(all_rows, args.plots)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"rows": all_rows}, indent=2))
        print(f"\nWrote metrics to {args.output}")


if __name__ == "__main__":
    main()
