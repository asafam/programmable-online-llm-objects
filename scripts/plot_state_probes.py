#!/usr/bin/env python3
from __future__ import annotations
"""Plot state-probe experiment results: probe accuracy and token cost vs event depth.

Usage:
    python scripts/plot_state_probes.py <lnl_results.jsonl> <baseline_results.jsonl> [plots_dir]
        [--tcs <test_cases_state_probes.jsonl>]

    lnl_results      — output from evaluate.py on state-probe TCs
    baseline_results — output from evaluate_baseline.py on the same TCs
    plots_dir        — where to save PNGs (default: same dir as lnl_results/plots/)
    --tcs            — original TC file; enables conditioned accuracy plot

TC IDs must match the format: {sample_id}-probe-D{depth:02d}-TC{index:03d}

Generates:
    probe_accuracy_vs_depth.png            — raw post_mod pass rate by depth
    probe_conditioned_accuracy_vs_depth.png — per-probe conditioned accuracy (requires --tcs).
        Conditioned: a probe counts only when all events listed in its `depends_on`
        passed. Falls back to TC-level (all state events passed) for legacy TCs
        without `depends_on`.
    tokens_vs_depth.png                    — agent input tokens per event by depth
    elapsed_vs_depth.png                   — mean elapsed time per TC by depth
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mtick

PARADIGM_LABEL = {"lnl": "Ours", "baseline": "OpenClaw"}
PARADIGM_COLOR = {"lnl": "#2196F3",    "baseline": "#E64A19"}
PARADIGM_MARKER = {"lnl": "o",         "baseline": "s"}
PARADIGM_LINESTYLE = {"lnl": "-",      "baseline": "--"}

DEPTH_RE    = re.compile(r"-probe-D(\d+)-TC")
FIDELITY_RE = re.compile(r"-sfid-D(\d+)-C(\d+)-TC")
STEP_ID     = re.compile(r"^S\d+$")

# Probe classification patterns
_ENTITY_PATTERNS = [
    re.compile(r"https?://[^\s,>\]'\")]+"),
    re.compile(r"\b[A-Z]+-\d+\b"),
    re.compile(r"#\d+"),
    re.compile(r'"([A-Z][a-z]+ [A-Z][a-z]+)"'),
    re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b"),
]
_AGGREGATIVE_STARTS = re.compile(
    r"^\s*(which|what are|list|name all|how many|how often|what (is the total|count)|"
    r"are there any|give me all|show all)",
    re.IGNORECASE,
)


def _is_aggregative(probe_input: str) -> bool:
    for pat in _ENTITY_PATTERNS:
        if pat.search(probe_input):
            return False
    return bool(_AGGREGATIVE_STARTS.match(probe_input)) or True


# ── Data loading ──────────────────────────────────────────────────────────────

def _extract_depth(tc_id: str) -> int | None:
    m = DEPTH_RE.search(tc_id)
    return int(m.group(1)) if m else None


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


def load_results(path: Path) -> dict[int, list[dict]]:
    """Return {depth: [tc_result, ...]} parsed from a results JSONL file."""
    by_depth: dict[int, list[dict]] = defaultdict(list)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if "tc_id" not in d:
                continue
            depth = _extract_depth(d["tc_id"])
            if depth is None:
                continue
            by_depth[depth].append(d)
    return dict(by_depth)


def load_fidelity_results(path: Path) -> dict[int, dict[int, list[dict]]]:
    """Return {n_c: {depth: [tc_result, ...]}} for sfid TC IDs.

    Each (tc_id, run_index) slot may appear multiple times when the eval
    retried timed-out runs.  We keep only the LAST entry per slot — the most
    recent attempt.  If the last attempt is still a timeout, it counts as a
    failed probe (timeout = system couldn't answer = failure).
    """
    # Collect all results, preserving insertion order so last wins.
    # Key: (tc_id, run_index)
    slot_results: dict[tuple, dict] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if "tc_id" not in d:
                continue
            if not FIDELITY_RE.search(d["tc_id"]):
                continue
            key = (d["tc_id"], d.get("run_index", 0))
            slot_results[key] = d   # last write wins

    by_cell: dict[int, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
    n_timeout = 0
    for d in slot_results.values():
        if d.get("error_type") == "timeout":
            n_timeout += 1
            continue  # excluded — not a memory failure
        m = FIDELITY_RE.search(d["tc_id"])
        depth, n_c = int(m.group(1)), int(m.group(2))
        by_cell[n_c][depth].append(d)

    if n_timeout:
        print(f"  ({n_timeout} timeout result(s) excluded — not memory failures)")
    return {k: dict(v) for k, v in by_cell.items()}


# ── Metric computation ────────────────────────────────────────────────────────

def _compute_depth_metrics(tc_results: list[dict]) -> dict:
    """Aggregate raw metrics across all TC results at one depth."""
    probe_pass: list[int] = []
    probe_total: list[int] = []
    state_pass: list[int] = []
    state_total: list[int] = []
    in_toks: list[float] = []
    out_toks: list[float] = []
    elapsed_s: list[float] = []

    for r in tc_results:
        events = r.get("events", [])
        probe_events = [e for e in events if e.get("role") == "post_mod"]
        state_events = [e for e in events if e.get("role") == "irrelevant"]

        if probe_events:
            n_pass = sum(1 for e in probe_events if e.get("passed"))
            probe_pass.append(n_pass)
            probe_total.append(len(probe_events))

        if state_events:
            n_s_pass = sum(1 for e in state_events if e.get("passed"))
            state_pass.append(n_s_pass)
            state_total.append(len(state_events))

        for e in events:
            if STEP_ID.match(e.get("event_id", "")):
                continue
            if e.get("input_tokens"):
                in_toks.append(e["input_tokens"])
            if e.get("output_tokens"):
                out_toks.append(e["output_tokens"])

        if r.get("elapsed_ms") is not None:
            elapsed_s.append(r["elapsed_ms"] / 1000)

    def mean(xs): return sum(xs) / len(xs) if xs else None

    total_probes = sum(probe_total)
    total_pass   = sum(probe_pass)
    total_state  = sum(state_total)
    total_s_pass = sum(state_pass)

    return {
        "probe_accuracy":        total_pass   / total_probes if total_probes else None,
        "state_accuracy":        total_s_pass / total_state  if total_state  else None,
        "probe_accuracy_per_tc": [p / t for p, t in zip(probe_pass, probe_total) if t],
        "mean_in_tok":           mean(in_toks),
        "mean_out_tok":          mean(out_toks),
        "elapsed_mean_s":        mean(elapsed_s),
        "n_tcs":                 len(tc_results),
        "n_probes":              total_probes,
    }


def _compute_conditioned_metrics(
    tc_results: list[dict],
    tcs: dict[str, dict],
) -> dict:
    """Compute per-probe conditioned accuracy for one depth.

    raw          — all probes (per-event judge pass/fail)
    conditioned  — probe counted only when every event in its `depends_on` passed.
                   Falls back to TC-level conditioning (all state events passed)
                   for legacy TCs whose probes have no `depends_on`.
    """
    raw_pass = raw_total = 0
    cond_pass = cond_total = 0
    cond_per_tc: list[float] = []

    for r in tc_results:
        tc_id = r.get("tc_id", "")
        tc = tcs.get(tc_id, {})
        events = r.get("events", [])

        probe_events = [e for e in events if e.get("role") == "post_mod"]
        state_ev_results = [e for e in events if e.get("role") == "irrelevant"]

        state_pass_map = {
            e.get("event_id"): e.get("passed", False)
            for e in state_ev_results
            if e.get("event_id")
        }

        if state_ev_results:
            tc_state_ok: bool | None = all(e.get("passed", False) for e in state_ev_results)
        else:
            tc_state_ok = None

        tc_event_by_id = {te.get("id"): te for te in tc.get("events", [])}

        for e in probe_events:
            passed = e.get("passed", False)
            tc_evt = tc_event_by_id.get(e.get("event_id")) or {}
            depends_on = tc_evt.get("depends_on") or []

            raw_pass  += 1 if passed else 0
            raw_total += 1

            if depends_on:
                if state_pass_map and all(state_pass_map.get(d, False) for d in depends_on):
                    cond_pass  += 1 if passed else 0
                    cond_total += 1
                    cond_per_tc.append(1.0 if passed else 0.0)
            elif tc_state_ok is True:
                cond_pass  += 1 if passed else 0
                cond_total += 1
                cond_per_tc.append(1.0 if passed else 0.0)

    return {
        "raw_accuracy":         raw_pass / raw_total if raw_total else None,
        "conditioned":          cond_pass / cond_total if cond_total else None,
        "conditioned_per_tc":   cond_per_tc,
        "n_total_probes":       raw_total,
        "n_conditioned_probes": cond_total,
    }


def build_series(by_depth: dict[int, list[dict]]) -> dict[int, dict]:
    return {depth: _compute_depth_metrics(tcs) for depth, tcs in sorted(by_depth.items())}


def build_conditioned_series(
    by_depth: dict[int, list[dict]],
    tcs: dict[str, dict],
) -> dict[int, dict]:
    return {
        depth: _compute_conditioned_metrics(tc_results, tcs)
        for depth, tc_results in sorted(by_depth.items())
    }


# ── Plotting helpers ──────────────────────────────────────────────────────────

def _save(fig, path: Path, title: str) -> None:
    fig.suptitle(title, fontsize=13, fontweight="bold")
    plt.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(path, dpi=150, bbox_inches="tight")
    print(f"  Saved: {path}")
    plt.close(fig)


def _draw_depth_line(
    ax,
    series: dict[int, dict],
    metric_key: str,
    paradigm: str,
    label: str | None = None,
    error_key: str | None = None,
    linestyle: str | None = None,
    alpha: float = 1.0,
    markersize: int = 7,
) -> bool:
    depths = sorted(series)
    values = [series[d].get(metric_key) for d in depths]
    if all(v is None for v in values):
        return False
    y = [v if v is not None else float("nan") for v in values]

    ax.plot(
        depths, y,
        linestyle=linestyle or PARADIGM_LINESTYLE[paradigm],
        marker=PARADIGM_MARKER[paradigm],
        color=PARADIGM_COLOR[paradigm],
        label=label or PARADIGM_LABEL[paradigm],
        linewidth=2,
        markersize=markersize,
        alpha=alpha,
    )

    if error_key:
        try:
            from scipy import stats as _stats
            _use_scipy = True
        except ImportError:
            _use_scipy = False

        for depth, yval in zip(depths, y):
            vals = series[depth].get(error_key, [])
            n = len(vals)
            if n > 1:
                import statistics
                std = statistics.stdev(vals)
                t_crit = _stats.t.ppf(0.975, df=n - 1) if _use_scipy else 1.96
                ci = t_crit * std / (n ** 0.5)
                ax.errorbar(
                    depth, yval, yerr=ci,
                    fmt="none", color=PARADIGM_COLOR[paradigm],
                    capsize=4, linewidth=1.2, alpha=0.6,
                )
    return True


# ── Plot functions ────────────────────────────────────────────────────────────

def plot_probe_accuracy(
    lnl_series: dict[int, dict],
    baseline_series: dict[int, dict],
    plots_dir: Path,
    judge_label: str = "",
) -> None:
    all_depths = sorted(set(lnl_series) | set(baseline_series))

    fig, ax = plt.subplots(figsize=(8, 5))

    for paradigm, series in [("lnl", lnl_series), ("baseline", baseline_series)]:
        _draw_depth_line(ax, series, "probe_accuracy", paradigm,
                         error_key="probe_accuracy_per_tc")

    ax.set_xticks(all_depths)
    ax.set_xticklabels([f"D={d}" for d in all_depths], fontsize=10)
    ax.set_xlabel("State event depth (N events before probes)", fontsize=10)
    ax.set_ylabel("Probe accuracy (post-mod pass rate)", fontsize=10)
    ax.yaxis.set_major_formatter(mtick.PercentFormatter(1.0))
    ax.set_ylim(-0.02, 1.05)
    ax.grid(True, alpha=0.3, linestyle=":")
    ax.legend(fontsize=10, loc="best")

    for depth in all_depths:
        n = (lnl_series.get(depth) or baseline_series.get(depth) or {}).get("n_tcs", 0)
        ax.annotate(f"n={n}", xy=(depth, -0.02), xycoords=("data", "axes fraction"),
                    ha="center", va="top", fontsize=7, color="gray")

    title = "State Probe: Accuracy vs Event Depth  (error bars: 95% CI)"
    if judge_label:
        title += f"  —  judge: {judge_label}"
    _save(fig, plots_dir / "probe_accuracy_vs_depth.png", title)


def plot_conditioned_accuracy(
    lnl_cond: dict[int, dict],
    base_cond: dict[int, dict],
    plots_dir: Path,
    judge_label: str = "",
    lnl_series: dict[int, dict] | None = None,
) -> None:
    all_depths = sorted(set(lnl_cond) | set(base_cond))

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    for ax, (metric_key, panel_title, n_key) in zip(axes, [
        ("raw_accuracy",
         "Raw Probe Accuracy (all probes)",
         "n_total_probes"),
        ("conditioned",
         "Conditioned: Probes whose depends_on events all passed",
         "n_conditioned_probes"),
    ]):
        for paradigm, series in [("lnl", lnl_cond), ("baseline", base_cond)]:
            _draw_depth_line(ax, series, metric_key, paradigm)

        # State-event pass rate as a ceiling reference for LNL
        if lnl_series and metric_key == "conditioned":
            depths = sorted(lnl_series)
            state_vals = [lnl_series[d].get("state_accuracy") for d in depths]
            if any(v is not None for v in state_vals):
                ax.plot(
                    depths,
                    [v if v is not None else float("nan") for v in state_vals],
                    linestyle=":",
                    marker="",
                    color=PARADIGM_COLOR["lnl"],
                    linewidth=1.5,
                    alpha=0.5,
                    label="Ours — state event pass rate (ceiling)",
                )
                # Shade the gap between state% and conditioned probe%
                cond_vals = [lnl_cond[d].get("conditioned") for d in depths if d in lnl_cond]
                if len(cond_vals) == len(depths):
                    ax.fill_between(
                        depths,
                        [v if v is not None else float("nan") for v in cond_vals],
                        [v if v is not None else float("nan") for v in state_vals],
                        color=PARADIGM_COLOR["lnl"],
                        alpha=0.08,
                        label="State→probe gap (Ours)",
                    )

        ax.set_xticks(all_depths)
        ax.set_xticklabels([f"D={d}" for d in all_depths], fontsize=10)
        ax.set_xlabel("State event depth (N events before probes)", fontsize=10)
        ax.set_ylabel("Accuracy", fontsize=10)
        ax.yaxis.set_major_formatter(mtick.PercentFormatter(1.0))
        ax.set_ylim(-0.02, 1.05)
        ax.grid(True, alpha=0.3, linestyle=":")
        ax.legend(fontsize=10, loc="best")
        ax.set_title(panel_title, fontsize=10)

        for depth in all_depths:
            n = (lnl_cond.get(depth) or base_cond.get(depth) or {}).get(n_key, 0)
            ax.annotate(f"n={n}", xy=(depth, -0.02), xycoords=("data", "axes fraction"),
                        ha="center", va="top", fontsize=7, color="gray")

    title = "State Probe: Conditioned Accuracy vs Event Depth"
    if judge_label:
        title += f"  —  judge: {judge_label}"
    _save(fig, plots_dir / "probe_conditioned_accuracy_vs_depth.png", title)


def plot_tokens(
    lnl_series: dict[int, dict],
    baseline_series: dict[int, dict],
    plots_dir: Path,
) -> None:
    all_depths = sorted(set(lnl_series) | set(baseline_series))

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))

    for ax, (metric_key, ylabel) in zip(axes, [
        ("mean_in_tok",  "Mean agent input tokens / event"),
        ("mean_out_tok", "Mean agent output tokens / event"),
    ]):
        for paradigm, series in [("lnl", lnl_series), ("baseline", baseline_series)]:
            _draw_depth_line(ax, series, metric_key, paradigm)
        ax.set_xticks(all_depths)
        ax.set_xticklabels([f"D={d}" for d in all_depths], fontsize=10)
        ax.set_xlabel("State event depth", fontsize=10)
        ax.set_ylabel(ylabel, fontsize=10)
        ax.grid(True, alpha=0.3, linestyle=":")
        ax.legend(fontsize=10, loc="best")

    _save(fig, plots_dir / "tokens_vs_depth.png",
          "State Probe: Token Cost vs Event Depth")


def plot_elapsed(
    lnl_series: dict[int, dict],
    baseline_series: dict[int, dict],
    plots_dir: Path,
) -> None:
    all_depths = sorted(set(lnl_series) | set(baseline_series))

    fig, ax = plt.subplots(figsize=(8, 5))

    for paradigm, series in [("lnl", lnl_series), ("baseline", baseline_series)]:
        _draw_depth_line(ax, series, "elapsed_mean_s", paradigm)

    ax.set_xticks(all_depths)
    ax.set_xticklabels([f"D={d}" for d in all_depths], fontsize=10)
    ax.set_xlabel("State event depth", fontsize=10)
    ax.set_ylabel("Mean elapsed time per TC (s)", fontsize=10)
    ax.grid(True, alpha=0.3, linestyle=":")
    ax.legend(fontsize=10, loc="best")

    _save(fig, plots_dir / "elapsed_vs_depth.png",
          "State Probe: Elapsed Time vs Event Depth")


# ── Fidelity plot functions ───────────────────────────────────────────────────

# Distinct markers for multiple n_c levels; colors stay paradigm-fixed.
_FIDELITY_MARKERS = ["o", "s", "^", "D", "v", "P", "*"]


def _fidelity_all_depths(lnl_cells, base_cells):
    return sorted({d for cells in (lnl_cells, base_cells) for s in cells.values() for d in s})


def _is_coupled(cells: dict[int, dict[int, dict]]) -> bool:
    """True when each depth appears in exactly one n_c group (diagonal/coupled design)."""
    depth_count: dict[int, int] = {}
    for by_depth in cells.values():
        for d in by_depth:
            depth_count[d] = depth_count.get(d, 0) + 1
    return bool(depth_count) and all(v == 1 for v in depth_count.values())


def _merge_coupled(
    cells: dict[int, dict[int, dict]],
) -> tuple[dict[int, dict], dict[int, int]]:
    """Flatten coupled cells → (flat {depth: metrics}, {depth: n_c})."""
    flat: dict[int, dict] = {}
    depth_nc: dict[int, int] = {}
    for n_c, by_depth in cells.items():
        for depth, metrics in by_depth.items():
            flat[depth] = metrics
            depth_nc[depth] = n_c
    return flat, depth_nc


def _draw_fidelity_lines(
    ax,
    lnl_cells: dict[int, dict[int, dict]],
    base_cells: dict[int, dict[int, dict]],
    metric_key: str,
    error_key: str | None = None,
) -> None:
    """Draw one LNL (solid, blue) + one baseline (dashed, orange) line per n_c value.

    When there is only one n_c the labels are just "Ours" / "OpenClaw" to match
    the probe-plot style.  With multiple n_c values an extra "(C=N)" suffix is
    appended and the marker cycles to keep lines distinguishable.
    """
    all_nc = sorted(set(lnl_cells) | set(base_cells))
    multi = len(all_nc) > 1
    for i, n_c in enumerate(all_nc):
        nc_suffix = f" (C={n_c})" if multi else ""
        marker = _FIDELITY_MARKERS[i % len(_FIDELITY_MARKERS)]
        for paradigm, cells in [("lnl", lnl_cells), ("baseline", base_cells)]:
            series = cells.get(n_c, {})
            if not series:
                continue
            label = f"{PARADIGM_LABEL[paradigm]}{nc_suffix}"
            alpha = 0.85 - 0.15 * i
            _draw_depth_line(
                ax, series, metric_key, paradigm,
                label=label,
                error_key=error_key,
                markersize=7,
                alpha=alpha,
            )
            # override marker if multi-n_c (can't pass marker through _draw_depth_line directly)
            if multi and ax.get_lines():
                ax.get_lines()[-1].set_marker(marker)


def plot_fidelity_accuracy(
    lnl_cells: dict[int, dict[int, dict]],
    base_cells: dict[int, dict[int, dict]],
    plots_dir: Path,
    judge_label: str = "",
    lnl_cond_cells: dict[int, dict[int, dict]] | None = None,
    base_cond_cells: dict[int, dict[int, dict]] | None = None,
) -> None:
    """Single-panel plot: fidelity (conditioned probe accuracy) vs depth."""
    fig, ax = plt.subplots(figsize=(8, 5))
    axes = [ax]

    # Prefer conditioned cells (fidelity) over raw if available
    lnl_src  = lnl_cond_cells  if lnl_cond_cells  else lnl_cells
    base_src = base_cond_cells if base_cond_cells else base_cells
    metric_key = "conditioned" if lnl_cond_cells else "probe_accuracy"
    err_key    = "conditioned_per_tc" if lnl_cond_cells else "probe_accuracy_per_tc"

    panels = [
        (metric_key, err_key, lnl_src, base_src,
         "Fidelity: probe accuracy conditioned on all correction events passing"),
    ]

    coupled = _is_coupled(lnl_src) and _is_coupled(base_src)
    depth_nc: dict[int, int] = {}
    if coupled:
        lnl_flat, lnl_dn  = _merge_coupled(lnl_src)
        base_flat, base_dn = _merge_coupled(base_src)
        depth_nc = {**base_dn, **lnl_dn}

    for ax, (metric_key, err_key, lnl_src, base_src, panel_title) in zip(axes, panels):
        if coupled:
            lnl_flat, _  = _merge_coupled(lnl_src)
            base_flat, _ = _merge_coupled(base_src)
            for paradigm, series in [("lnl", lnl_flat), ("baseline", base_flat)]:
                _draw_depth_line(ax, series, metric_key, paradigm, error_key=err_key)
            all_depths = sorted(set(lnl_flat) | set(base_flat))
        else:
            _draw_fidelity_lines(ax, lnl_src, base_src, metric_key, error_key=err_key)
            all_depths = _fidelity_all_depths(lnl_src, base_src)

        ax.set_xticks(all_depths)
        if coupled and depth_nc:
            ax.set_xticklabels([f"D={d}\n(C={depth_nc.get(d,'?')})" for d in all_depths], fontsize=9)
        else:
            ax.set_xticklabels([f"D={d}" for d in all_depths], fontsize=10)
        ax.set_xlabel("Depth (total events before probe)", fontsize=10)
        ax.set_ylabel("Probe accuracy", fontsize=10)
        ax.yaxis.set_major_formatter(mtick.PercentFormatter(1.0))
        ax.set_ylim(-0.02, 1.05)
        ax.grid(True, alpha=0.3, linestyle=":")
        ax.legend(fontsize=10, loc="best")
        ax.set_title(panel_title, fontsize=10)

        for depth in all_depths:
            for cells in (lnl_src, base_src):
                for nc_series in cells.values():
                    m = nc_series.get(depth, {})
                    n = m.get("n_tcs") or m.get("n_total_probes")
                    if n:
                        ax.annotate(f"n={n}", xy=(depth, -0.02),
                                    xycoords=("data", "axes fraction"),
                                    ha="center", va="top", fontsize=7, color="gray")
                        break
                else:
                    continue
                break

    title = "State Fidelity Under Update Pressure: Probe Accuracy vs Depth"
    if judge_label:
        title += f"  —  judge: {judge_label}"
    fname = "fidelity_accuracy_vs_depth.png"
    _save(fig, plots_dir / fname, title)


def _fidelity_xticks(ax, all_depths, depth_nc: dict[int, int] | None) -> None:
    ax.set_xticks(all_depths)
    if depth_nc:
        ax.set_xticklabels([f"D={d}\n(C={depth_nc.get(d,'?')})" for d in all_depths], fontsize=9)
    else:
        ax.set_xticklabels([f"D={d}" for d in all_depths], fontsize=10)


def plot_fidelity_tokens(
    lnl_cells: dict[int, dict[int, dict]],
    base_cells: dict[int, dict[int, dict]],
    plots_dir: Path,
) -> None:
    coupled = _is_coupled(lnl_cells) and _is_coupled(base_cells)
    depth_nc: dict[int, int] | None = None
    if coupled:
        lnl_flat, lnl_dn   = _merge_coupled(lnl_cells)
        base_flat, base_dn = _merge_coupled(base_cells)
        depth_nc = {**base_dn, **lnl_dn}

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    for ax, (metric_key, ylabel) in zip(axes, [
        ("mean_in_tok",  "Mean agent input tokens / event"),
        ("mean_out_tok", "Mean agent output tokens / event"),
    ]):
        if coupled:
            for paradigm, series in [("lnl", lnl_flat), ("baseline", base_flat)]:
                _draw_depth_line(ax, series, metric_key, paradigm)
            all_depths = sorted(set(lnl_flat) | set(base_flat))
        else:
            _draw_fidelity_lines(ax, lnl_cells, base_cells, metric_key)
            all_depths = _fidelity_all_depths(lnl_cells, base_cells)
        _fidelity_xticks(ax, all_depths, depth_nc)
        ax.set_xlabel("Depth (total events)", fontsize=10)
        ax.set_ylabel(ylabel, fontsize=10)
        ax.grid(True, alpha=0.3, linestyle=":")
        ax.legend(fontsize=10, loc="best")
    _save(fig, plots_dir / "fidelity_tokens_vs_depth.png",
          "State Fidelity: Token Cost vs Depth")


def plot_fidelity_elapsed(
    lnl_cells: dict[int, dict[int, dict]],
    base_cells: dict[int, dict[int, dict]],
    plots_dir: Path,
) -> None:
    coupled = _is_coupled(lnl_cells) and _is_coupled(base_cells)
    depth_nc: dict[int, int] | None = None
    if coupled:
        lnl_flat, lnl_dn   = _merge_coupled(lnl_cells)
        base_flat, base_dn = _merge_coupled(base_cells)
        depth_nc = {**base_dn, **lnl_dn}

    fig, ax = plt.subplots(figsize=(8, 5))
    if coupled:
        for paradigm, series in [("lnl", lnl_flat), ("baseline", base_flat)]:
            _draw_depth_line(ax, series, "elapsed_mean_s", paradigm)
        all_depths = sorted(set(lnl_flat) | set(base_flat))
    else:
        _draw_fidelity_lines(ax, lnl_cells, base_cells, "elapsed_mean_s")
        all_depths = _fidelity_all_depths(lnl_cells, base_cells)
    _fidelity_xticks(ax, all_depths, depth_nc)
    ax.set_xlabel("Depth (total events)", fontsize=10)
    ax.set_ylabel("Mean elapsed time per TC (s)", fontsize=10)
    ax.grid(True, alpha=0.3, linestyle=":")
    ax.legend(fontsize=10, loc="best")
    _save(fig, plots_dir / "fidelity_elapsed_vs_depth.png",
          "State Fidelity: Elapsed Time vs Depth")


# ── Summary table ─────────────────────────────────────────────────────────────

def print_summary(
    lnl_series: dict[int, dict],
    baseline_series: dict[int, dict],
    lnl_cond: dict[int, dict] | None = None,
    base_cond: dict[int, dict] | None = None,
) -> None:
    all_depths = sorted(set(lnl_series) | set(baseline_series))

    def fmt_pct(v):   return f"{v:.1%}" if v is not None else "   N/A"
    def fmt_tok(v):   return f"{v:,.0f}" if v is not None else "    N/A"
    def fmt_delta(v): return f"{v:+.1%}" if v is not None else "    N/A"

    has_cond = lnl_cond is not None and base_cond is not None

    # Header
    hdr = f"\n{'Depth':>6}  {'state%':>7}  {'LNL raw':>8}  {'Base raw':>9}  {'Δ raw':>6}"
    if has_cond:
        hdr += f"  {'LNL cond':>9}  {'Base cond':>10}  {'Δ cond':>7}  {'gap':>8}"
    hdr += f"  {'LNL tok/evt':>12}  {'Base tok/evt':>13}"
    print(hdr)
    print("-" * (len(hdr) - 1))

    for d in all_depths:
        lnl_m    = lnl_series.get(d) or {}
        base_m   = baseline_series.get(d) or {}
        lnl_acc  = lnl_m.get("probe_accuracy")
        base_acc = base_m.get("probe_accuracy")
        # State pass rate: average of LNL and baseline (both should be similar)
        lnl_state  = lnl_m.get("state_accuracy")
        base_state = base_m.get("state_accuracy")
        state_pct  = lnl_state  # show LNL's state event pass rate as the primary signal
        lnl_tok   = lnl_m.get("mean_in_tok")
        base_tok  = base_m.get("mean_in_tok")
        delta_raw = (lnl_acc - base_acc) if (lnl_acc is not None and base_acc is not None) else None

        row = (f"  D={d:>2}  {fmt_pct(state_pct):>7}  {fmt_pct(lnl_acc):>8}  "
               f"{fmt_pct(base_acc):>9}  {fmt_delta(delta_raw):>6}")

        if has_cond:
            lnl_c  = (lnl_cond.get(d) or {}).get("conditioned")
            base_c = (base_cond.get(d) or {}).get("conditioned")
            delta_c = (lnl_c - base_c) if (lnl_c is not None and base_c is not None) else None
            # Gap: state% − conditioned probe% (how much state advantage is lost at query time)
            gap = (lnl_state - lnl_c) if (lnl_state is not None and lnl_c is not None) else None
            row += (f"  {fmt_pct(lnl_c):>9}  {fmt_pct(base_c):>10}  {fmt_delta(delta_c):>7}"
                    f"  {fmt_delta(-gap) if gap is not None else '    N/A':>8}")

        row += f"  {fmt_tok(lnl_tok):>12}  {fmt_tok(base_tok):>13}"
        print(row)

    if has_cond:
        print("  (cond = probe counted only when all depends_on events passed)")
        print("  (gap  = state% − LNL cond%; ideally →0, large gap = state written but not usable)")
    print()


def _print_fidelity_summary(
    lnl_cells: dict[int, dict[int, dict]],
    base_cells: dict[int, dict[int, dict]],
    lnl_cond_cells: dict[int, dict[int, dict]] | None,
    base_cond_cells: dict[int, dict[int, dict]] | None,
) -> None:
    all_nc  = sorted(set(lnl_cells) | set(base_cells))
    all_dep = sorted({d for s in list(lnl_cells.values()) + list(base_cells.values()) for d in s})

    def pct(v):   return f"{v:.1%}" if v is not None else "   N/A"
    def tok(v):   return f"{v:,.0f}" if v is not None else "    N/A"
    def delta(a, b): return f"{a-b:+.1%}" if a is not None and b is not None else "    N/A"

    has_cond = lnl_cond_cells is not None
    has_base = bool(base_cells)

    hdr = f"\n{'C':>4}  {'D':>4}  {'n_lnl':>6}  {'n_base':>6}"
    if has_cond:
        hdr += f"  {'LNL fidelity':>12}  {'n (LNL)':>8}"
        if has_base:
            hdr += f"  {'Base fidelity':>13}  {'n (Base)':>9}  {'Δ':>7}"
    hdr += f"  {'LNL tok/evt':>12}"
    print(hdr)
    print("-" * (len(hdr) - 1))

    coupled = _is_coupled(lnl_cells) or _is_coupled(base_cells)

    for n_c in all_nc:
        for depth in all_dep:
            lm = lnl_cells.get(n_c, {}).get(depth) or {}
            bm = base_cells.get(n_c, {}).get(depth) or {}
            lc = (lnl_cond_cells or {}).get(n_c, {}).get(depth) or {}
            bc = (base_cond_cells or {}).get(n_c, {}).get(depth) or {}

            n_lnl       = lm.get("n_probes") or 0
            n_base      = bm.get("n_probes") or 0

            if n_lnl == 0 and n_base == 0:
                continue  # skip empty cells (coupled design)

            lnl_fid     = lc.get("conditioned")
            base_fid    = bc.get("conditioned")
            n_lnl_cond  = lc.get("n_conditioned_probes") or 0
            n_base_cond = bc.get("n_conditioned_probes") or 0

            row = f"  {n_c:>2}  D={depth:>2}  {n_lnl:>6}  {n_base:>6}"
            if has_cond:
                row += f"  {pct(lnl_fid):>12}  {n_lnl_cond:>8}"
                if has_base:
                    row += f"  {pct(base_fid):>13}  {n_base_cond:>9}  {delta(lnl_fid, base_fid):>7}"
            row += f"  {tok(lm.get('mean_in_tok')):>12}"
            print(row)
        if not coupled:
            print()

    print("n_lnl/n_base   = total probe runs (timeouts excluded)")
    print("fidelity       = probe accuracy among runs where all correction events passed the judge")
    print("n (LNL/Base)   = number of runs that passed the fidelity gate (all depends_on events passed)")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("lnl",      type=Path, help="LNL results JSONL")
    parser.add_argument("plots_dir", type=Path, nargs="?", default=None,
                        help="Output directory for PNGs (default: <lnl_dir>/plots/)")
    parser.add_argument("--baseline", type=Path, default=None,
                        help="Baseline results JSONL (optional — omit to plot LNL only)")
    parser.add_argument("--tcs", type=Path, default=None,
                        help="TC file; enables conditioned accuracy plot")
    parser.add_argument("--mode", choices=["probe", "fidelity"], default="probe",
                        help="'probe' (default): group by depth; "
                             "'fidelity': multi-line by n_c, depth on x-axis")
    parser.add_argument("--chart", default=None, metavar="CHART",
                        choices=["accuracy", "conditioned", "tokens", "elapsed"],
                        help="Generate only one chart instead of all. "
                             "probe mode:    accuracy, conditioned, tokens, elapsed. "
                             "fidelity mode: accuracy, tokens, elapsed.")
    args = parser.parse_args()

    lnl_path      = args.lnl
    baseline_path = args.baseline
    plots_dir     = args.plots_dir or lnl_path.parent / "plots"

    if not lnl_path.exists():
        print(f"File not found: {lnl_path}", file=sys.stderr)
        sys.exit(1)
    if baseline_path and not baseline_path.exists():
        print(f"File not found: {baseline_path}", file=sys.stderr)
        sys.exit(1)

    judge_label = ""
    with open(lnl_path) as f:
        first = f.readline().strip()
        if first:
            d = json.loads(first)
            judge_label = d.get("judge_model") or d.get("model") or ""

    tcs = None
    if args.tcs:
        if not args.tcs.exists():
            print(f"TC file not found: {args.tcs}", file=sys.stderr)
            sys.exit(1)
        print(f"Loading TCs:              {args.tcs}")
        tcs = load_tcs(args.tcs)
        print(f"  {len(tcs)} test cases")

    if args.mode == "fidelity":
        print(f"Loading LNL results:      {lnl_path}")
        lnl_raw  = load_fidelity_results(lnl_path)
        base_raw = {}
        if baseline_path:
            print(f"Loading baseline results: {baseline_path}")
            base_raw = load_fidelity_results(baseline_path)

        if not lnl_raw and not base_raw:
            print("No sfid TC results found in either file.", file=sys.stderr)
            sys.exit(1)

        # Build {n_c: {depth: metrics}} using the existing metric functions
        lnl_cells  = {n_c: build_series(by_depth) for n_c, by_depth in lnl_raw.items()}
        base_cells = {n_c: build_series(by_depth) for n_c, by_depth in base_raw.items()}

        lnl_cond_cells = base_cond_cells = None
        if tcs:
            lnl_cond_cells  = {n_c: build_conditioned_series(by_depth, tcs)
                                for n_c, by_depth in lnl_raw.items()}
            base_cond_cells = {n_c: build_conditioned_series(by_depth, tcs)
                                for n_c, by_depth in base_raw.items()}

        _print_fidelity_summary(lnl_cells, base_cells, lnl_cond_cells, base_cond_cells)
        print(f"Generating fidelity plots → {plots_dir}")
        c = args.chart
        if c is None or c == "accuracy":
            plot_fidelity_accuracy(
                lnl_cells, base_cells, plots_dir, judge_label,
                lnl_cond_cells=lnl_cond_cells,
                base_cond_cells=base_cond_cells,
            )
        if c is None or c == "tokens":
            plot_fidelity_tokens(lnl_cells, base_cells, plots_dir)
        if c is None or c == "elapsed":
            plot_fidelity_elapsed(lnl_cells, base_cells, plots_dir)

    else:
        print(f"Loading LNL results:      {lnl_path}")
        lnl_by_depth = load_results(lnl_path)
        lnl_series   = build_series(lnl_by_depth)

        base_by_depth: dict[int, list[dict]] = {}
        base_series: dict[int, dict] = {}
        if baseline_path:
            print(f"Loading baseline results: {baseline_path}")
            base_by_depth = load_results(baseline_path)
            base_series   = build_series(base_by_depth)

        if not lnl_series and not base_series:
            print("No state-probe TC results found in either file.", file=sys.stderr)
            sys.exit(1)

        lnl_cond = base_cond = None
        if tcs:
            lnl_cond  = build_conditioned_series(lnl_by_depth, tcs)
            if base_by_depth:
                base_cond = build_conditioned_series(base_by_depth, tcs)

        print_summary(lnl_series, base_series, lnl_cond, base_cond)
        print(f"Generating plots → {plots_dir}")

        c = args.chart
        if c is None or c == "accuracy":
            plot_probe_accuracy(lnl_series, base_series, plots_dir, judge_label)
        if c is None or c == "tokens":
            plot_tokens(lnl_series, base_series, plots_dir)
        if c is None or c == "elapsed":
            plot_elapsed(lnl_series, base_series, plots_dir)
        if (c is None or c == "conditioned") and tcs:
            plot_conditioned_accuracy(lnl_cond, base_cond or {}, plots_dir, judge_label,
                                      lnl_series=lnl_series)

    print("Done.")


if __name__ == "__main__":
    main()
