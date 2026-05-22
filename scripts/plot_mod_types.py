#!/usr/bin/env python3
"""plot_mod_types.py — Grouped bar chart of pass rate by modification type.

Compares Ours (LNL), OpenClaw single-agent, and OpenClaw multi-agent
across modification types (temporal, contextual, exception, correction,
expansion, removal).

Usage:
    # Scan a directory for result files (auto-detect method from filename):
    python scripts/plot_mod_types.py outputs/data/zapier/runs/experiments/myexp/

    # Explicit files:
    python scripts/plot_mod_types.py \\
        --lnl   outputs/.../test_cases_eval.jsonl \\
        --multi outputs/.../test_cases_baseline_multi.jsonl \\
        --single outputs/.../test_cases_baseline_single.jsonl

    # Single metric panel (default: post_mod):
    python scripts/plot_mod_types.py myexp/ --metric post_mod

File naming convention when scanning a directory:
    *eval*.jsonl            → LNL (Ours)
    *baseline*single*.jsonl → OpenClaw single-agent
    *baseline*.jsonl        → OpenClaw multi-agent (catches all remaining baselines)
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# ── Constants ─────────────────────────────────────────────────────────────────

MOD_TYPES_ORDER = [
    "temporal", "contextual", "exception", "correction", "expansion", "removal",
]

MOD_TYPE_RE = re.compile(
    r"-(temporal|contextual|exception|correction|expansion|removal)-TC\d+",
    re.IGNORECASE,
)

SERIES = [
    ("lnl",    "Ours"),
    ("single", "OpenClaw (single-agent)"),
    ("multi",  "OpenClaw (multi-agent)"),
]

METRICS = {
    "post_mod":  "Post-mod pass rate",
    "pre_mod":   "Pre-mod pass rate",
    "mean":      "Mean pass rate",
    "irrelevant": "Irrelevant pass rate",
}

DEFAULT_PALETTE = "default"
DEFAULT_METRIC  = "post_mod"

PALETTES: dict[str, dict] = {
    "default": {
        # Series order: SERIES = [(lnl, Ours), (single, OC single), (multi, OC multi)]
        "bg":       "#FFFFFF",
        "gridline": "#E5E5E5",
        "axis":     "#333333",
        "text":     "#222222",
        "text_dim": "#666666",
        "colors":   ["#005EF5", "#FFBA08", "#D00000"],
    },
    "botanical": {
        "bg":       "#F2EDDE",
        "gridline": "#D4CBB0",
        "axis":     "#A89A75",
        "text":     "#5A5040",
        "text_dim": "#7A6E50",
        "colors":   ["#2D5A3E", "#A04E2D", "#A8862D"],
    },
    "pastel": {
        "bg":       "#E8E4DD",
        "gridline": "#CCC6B8",
        "axis":     "#9E9684",
        "text":     "#5C5444",
        "text_dim": "#7A7263",
        "colors":   ["#C56F89", "#588F92", "#A89668"],
    },
    "monochrome": {
        "bg":       "#FFFFFF",
        "gridline": "#EEEEEE",
        "axis":     "#333333",
        "text":     "#222222",
        "text_dim": "#666666",
        "colors":   ["#0066CC", "#666666", "#AAAAAA"],
    },
    "okabe": {
        "bg":       "#FFFFFF",
        "gridline": "#E5E5E5",
        "axis":     "#333333",
        "text":     "#222222",
        "text_dim": "#666666",
        "colors":   ["#D55E00", "#009E73", "#56B4E9"],
    },
    "riso": {
        "bg":       "#FFFFFF",
        "gridline": "#F0F0F0",
        "axis":     "#222222",
        "text":     "#111111",
        "text_dim": "#666666",
        "colors":   ["#FF4B00", "#005AFF", "#03AF7A"],
    },
}

# ── Failure classification (matches evaluate_baseline.py::_classify_failure) ──

# Patterns sourced from observed judge reasoning + the live classifier in
# src/data/evaluate_baseline.py. Used here to retroactively classify events
# from older eval files that predate the failure_class field.

_INFRA_PROVIDER_PATTERNS = [
    "rate-limit", "rate limit", "throttle", "429",
    "the llm may be rate-limited or unavailable", "agent completed with no response",
    "content_filter", "content filter", "jailbreak", "responsibleaipolicy",
    "rejected the request schema", "provider rejected the request",
    "llm request failed: provider rejected",
    "http 500", "http 503", "internal server error",
    "azure openai response truncated", "service unavailable",
]
_OC_EVAL_PATTERNS = [
    "gateway did not become ready", "openclaw gateway",
    "not connected", "call await gw.connect", "websocket disconnected",
    "pairing required", "pairing-required",
    "timeout after", "timed out after", "timed out", "timeout",
    "container restarted", "worker restart",
]


def _classify_event(event: dict) -> Optional[str]:
    """Return 'oc_eval' | 'infra_provider' | 'behavioral' | None (passed).

    Prefers the persisted `failure_class` field when present (new eval files).
    Falls back to regex over reasoning text for older files.
    """
    if event.get("passed"):
        return None
    # New-format: trust the persisted classification
    fc = event.get("failure_class")
    if fc in ("oc_eval", "infra_provider", "behavioral"):
        return fc
    # Old format: classify from reasoning
    reasoning = (event.get("reasoning") or "").lower()
    if any(p in reasoning for p in _INFRA_PROVIDER_PATTERNS):
        return "infra_provider"
    if event.get("infra_error", False):
        return "oc_eval"
    if any(p in reasoning for p in _OC_EVAL_PATTERNS):
        return "oc_eval"
    return "behavioral"


# ── Data loading ──────────────────────────────────────────────────────────────

def _extract_mod_type(tc_id: str) -> Optional[str]:
    m = MOD_TYPE_RE.search(tc_id)
    return m.group(1).lower() if m else None


def load_mod_rates(path: Path, metric: str) -> dict[str, tuple[float, float, int]]:
    """Return {mod_type: (mean_pass_rate, std_pass_rate, n_tcs)}.

    Excludes:
      - TC-level infra/timeout failures (OC baseline sets `error_type`).
      - Event-level infra failures via `infra_error=True` (LNL flags these on the
        event, not the TC).
      - Reasoning-pattern infra failures (older eval files without persisted flags).
    For 'mean': all non-step events count.
    For 'post_mod' / 'pre_mod' / 'irrelevant': only events with that role.
    """
    role_filter = None if metric == "mean" else metric

    by_mod: dict[str, list[float]] = defaultdict(list)

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if "tc_id" not in d:
                continue
            if d.get("error_type") in ("infra", "timeout"):
                continue

            tc_id = d["tc_id"]
            mod_type = _extract_mod_type(tc_id)
            if mod_type is None:
                continue

            events = d.get("events", [])
            if role_filter:
                events = [e for e in events if e.get("role") == role_filter]
            else:
                # exclude step events (id matches S\d+) for mean metric
                events = [e for e in events if not re.match(r"^S\d+$", e.get("event_id", ""))]

            # Defensive event-level infra filter (covers LNL records where TC-level
            # error_type is unset but per-event infra_error is True).
            events = [e for e in events if not e.get("infra_error", False)]

            # Reasoning-pattern fallback for old eval files without persisted flags.
            events = [e for e in events if _classify_event(e) not in ("oc_eval", "infra_provider")]

            if not events:
                continue

            tc_rate = sum(1 for e in events if e["passed"]) / len(events)
            by_mod[mod_type].append(tc_rate)

    result: dict[str, tuple[float, float, int]] = {}
    for mod_type, rates in by_mod.items():
        mean_r = sum(rates) / len(rates)
        std_r  = float(np.std(rates)) if len(rates) > 1 else 0.0
        result[mod_type] = (mean_r, std_r, len(rates))

    return result


# ── LaTeX summary table ───────────────────────────────────────────────────────

def compute_table_row(path: Path) -> dict:
    """Per-TC aggregation of Base / On-Mod / Off-Mod pass rates, latency, tokens.

    For each TC + run, computes the pass rate over events of each role:
      - Base    = role=None (regular workflow events, no mod yet)
      - On-Mod  = role='post_mod' (events immediately following a modification)
      - Off-Mod = role='irrelevant' (memory-fidelity probes / off-modification events)

    Excludes infra+wiring-class failures (oc_eval, infra_provider) so the
    reported rates reflect agent behavior only.

    Std convention (matches the paper format): for each TC, compute the std
    of per-run pass rates ACROSS runs. Then report mean(per_tc_means) ±
    mean(per_tc_stds). This measures within-TC stochasticity rather than
    cross-TC heterogeneity (which would have much wider bars).
    """
    # rates_per_tc_run[tc_id][role] = [rate_run0, rate_run1, ...]
    from collections import defaultdict
    rates_per_tc_run: dict[str, dict] = defaultdict(lambda: defaultdict(list))
    # Per-event binary outcomes across runs, for non-determinism analysis.
    # event_outcomes[(tc_id, event_id)] = [pass_run0, pass_run1, ...]
    event_outcomes: dict[tuple[str, str], list[bool]] = defaultdict(list)
    runs_per_tc:    dict[str, set]                    = defaultdict(set)
    # Per (TC, run) totals — one entry per SampleResult line in the JSONL.
    # Per-TC convention (matches the paper format): each entry is the total
    # wall-clock or total tokens for one (TC, run) — the cost of running the
    # whole workflow once end-to-end. Judge tokens are NOT included (they're
    # eval cost, not system cost). For OC multi the per-event input_tokens
    # already aggregates across all cascade sessions via _delta_tokens.
    tc_total_elapsed_s: list[float] = []
    tc_total_in:  list[int]         = []
    tc_total_out: list[int]         = []

    # TCs that had ANY infra/timeout run are excluded from entropy (entropy
    # requires uniform R per TC; mixed R biases H̄).
    infra_tainted_tcs: set[str] = set()

    # First pass: identify TCs with any infra/timeout run.
    # A TC is tainted if EITHER the TC-level error_type is infra/timeout (OC baseline)
    # OR any event in the TC has infra_error=True (LNL flags errors at the event level).
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
                infra_tainted_tcs.add(d["tc_id"])
                continue
            if any(e.get("infra_error") for e in (d.get("events") or [])):
                infra_tainted_tcs.add(d["tc_id"])

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
            run_idx = d.get("run_index", 0)
            runs_per_tc[tc_id].add(run_idx)
            events = d.get("events") or []

            # Track per-event binary outcomes across runs — but skip TCs that
            # had any infra/timeout run, to keep R uniform per TC.
            if tc_id not in infra_tainted_tcs:
                for e in events:
                    eid    = e.get("event_id")
                    passed = e.get("passed")
                    if eid is None or passed is None:
                        continue
                    if _classify_event(e) in ("oc_eval", "infra_provider"):
                        continue
                    event_outcomes[(tc_id, eid)].append(bool(passed))

            for role_key, role_value in (("base", None), ("on_mod", "post_mod"), ("off_mod", "irrelevant")):
                role_events = [
                    e for e in events
                    if e.get("role") == role_value
                    and _classify_event(e) not in ("oc_eval", "infra_provider")
                ]
                if role_events:
                    rate = sum(1 for e in role_events if e["passed"]) / len(role_events)
                    rates_per_tc_run[tc_id][role_key].append(rate)

            # Per-TC totals (one entry per JSONL line = one (TC, run))
            tc_elapsed = d.get("elapsed_ms")
            if tc_elapsed is not None:
                tc_total_elapsed_s.append(float(tc_elapsed) / 1000.0)
            sum_in  = sum(int(e.get("input_tokens")  or 0) for e in events if e.get("passed") is not None)
            sum_out = sum(int(e.get("output_tokens") or 0) for e in events if e.get("passed") is not None)
            tc_total_in.append(sum_in)
            tc_total_out.append(sum_out)

    def _per_tc_stats(role_key: str) -> tuple[float, float, float, int]:
        """Return (mean, within_tc_std, ci95_half, n_tcs) in percent.

        - mean    = mean of per-TC means (each TC = one independent observation)
        - within_tc_std = mean of per-TC across-run stds (reproducibility, kept for plots)
        - ci95_half = t-based 95% CI half-width on the bucket mean, computed
                      from across-TC variance: t_crit * std(per_tc_means)/sqrt(n)
        """
        per_tc_means: list[float] = []
        per_tc_stds:  list[float] = []
        for tc_id, roles in rates_per_tc_run.items():
            rates = roles.get(role_key, [])
            if not rates:
                continue
            per_tc_means.append(float(np.mean(rates)))
            per_tc_stds.append(float(np.std(rates)) if len(rates) > 1 else 0.0)
        n = len(per_tc_means)
        if n == 0:
            return 0.0, 0.0, 0.0, 0
        mean        = float(np.mean(per_tc_means)) * 100
        within_std  = float(np.mean(per_tc_stds))  * 100
        if n > 1:
            across_std = float(np.std(per_tc_means, ddof=1))
            try:
                from scipy import stats as _stats
                t_crit = float(_stats.t.ppf(0.975, df=n - 1))
            except ImportError:
                t_crit = 1.96
            ci95_half = t_crit * across_std / (n ** 0.5) * 100
        else:
            ci95_half = 0.0
        return mean, within_std, ci95_half, n

    base_mean,   base_std,   base_ci,   base_n   = _per_tc_stats("base")
    on_mean,     on_std,     on_ci,     on_n     = _per_tc_stats("on_mod")
    off_mean,    off_std,    off_ci,    off_n    = _per_tc_stats("off_mod")

    def _mean(xs): return float(np.mean(xs)) if xs else 0.0

    # Non-determinism metrics (T=0 reproducibility across identical re-runs).
    # Restricted to TCs with no infra/timeout in any run, so R is uniform.
    import math
    def _bern_entropy(p: float) -> float:
        if p <= 0.0 or p >= 1.0:
            return 0.0
        return -(p * math.log2(p) + (1 - p) * math.log2(1 - p))

    eligible_tcs = {
        tc for tc, runs in runs_per_tc.items()
        if tc not in infra_tainted_tcs and len(runs) >= 2
    }
    max_runs    = max((len(r) for r in runs_per_tc.values()), default=0)
    clean_runs  = {len(runs_per_tc[tc]) for tc in eligible_tcs}
    uniform_R   = next(iter(clean_runs)) if len(clean_runs) == 1 else None

    unstable_tcs    = set()
    entropies_raw: list[float] = []
    entropies_mm:  list[float] = []
    flipped_groups = 0
    for (tc_id, _eid), outcomes in event_outcomes.items():
        if tc_id not in eligible_tcs or len(outcomes) < 2:
            continue
        if len(set(outcomes)) > 1:
            unstable_tcs.add(tc_id)
            flipped_groups += 1
        R = len(outcomes)
        p = sum(outcomes) / R
        h_raw = _bern_entropy(p)
        h_mm  = min(h_raw + 1.0 / (2 * R), 1.0)
        entropies_raw.append(h_raw)
        entropies_mm.append(h_mm)

    n_multi   = len(eligible_tcs)
    wir       = (len(unstable_tcs) / n_multi) if n_multi else None
    h_mean    = (sum(entropies_raw) / len(entropies_raw)) if entropies_raw else None
    h_mean_mm = (sum(entropies_mm)  / len(entropies_mm))  if entropies_mm  else None
    flip_frac = (flipped_groups / len(entropies_raw)) if entropies_raw else None

    return {
        "base_mean":   base_mean, "base_std":   base_std, "base_ci95":   base_ci, "base_n":   base_n,
        "on_mod_mean": on_mean,   "on_mod_std": on_std,   "on_mod_ci95": on_ci,   "on_mod_n": on_n,
        "off_mod_mean":off_mean,  "off_mod_std":off_std,  "off_mod_ci95":off_ci,  "off_mod_n":off_n,
        # Non-determinism (None if single-run data)
        "wir":              wir,
        "step_entropy":     h_mean,     # uncorrected plug-in Shannon H̄ — used in table
        "step_entropy_mm":  h_mean_mm,  # Miller-Madow corrected (available, not shown in table)
        "step_flip_frac":   flip_frac,
        "n_multi_run_tcs":  n_multi,
        "max_runs":         max_runs,
        "uniform_runs":     uniform_R,
        "n_infra_tcs":      len(infra_tainted_tcs),
        "n_step_groups":    len(entropies_raw),
        # Per-TC totals (averaged across all (TC, run) lines). Time is total
        # wall-clock per TC run; tokens are summed across all the TC's events
        # (judge tokens excluded — they're eval cost, not system cost).
        "elapsed_s":      _mean(tc_total_elapsed_s),
        "tokens_in_mean": _mean(tc_total_in),
        "tokens_out_mean":_mean(tc_total_out),
    }


def _fmt_rate(mean: float, std: float) -> str:
    return f"{mean:>4.1f} $\\pm$ {std:>4.1f}"


def _winners(items: list[tuple[str, float | None]], mode: str = "max") -> set[str]:
    """Return labels of the best-performing rows for a metric (set handles ties).

    items: [(label, value)]; None values are ignored.
    mode:  'max' (higher = better) or 'min' (lower = better).
    """
    vals = [(lbl, v) for lbl, v in items if v is not None]
    if not vals:
        return set()
    best = max(v for _, v in vals) if mode == "max" else min(v for _, v in vals)
    return {lbl for lbl, v in vals if v == best}


def _bf(s: str, winner: bool) -> str:
    return rf"\textbf{{{s.strip()}}}" if winner else s


def load_tc_object_counts(source_path: Path) -> dict[str, int]:
    """Read source workflows-mods.jsonl and return {tc_id: n_objects}."""
    out: dict[str, int] = {}
    if not source_path.exists():
        return out
    with open(source_path) as f:
        for line in f:
            try: tc = json.loads(line)
            except json.JSONDecodeError: continue
            if "id" in tc and "objects" in tc and isinstance(tc["objects"], list):
                out[tc["id"]] = len(tc["objects"])
    return out


def _object_bin(n: int) -> str:
    """Bin object count into readable buckets."""
    if n <= 4:   return "3–4"
    if n == 5:   return "5"
    if n <= 8:   return "6–8"
    return "9+"


_BIN_ORDER = ["3–4", "5", "6–8", "9+"]


def compute_table_by_objects(path: Path, tc_objects: dict[str, int]) -> dict[str, dict]:
    """Per object-count bin: aggregate Base/On-Mod/Off-Mod rates.

    Returns {bin_label: {base_mean, base_std, on_mod_..., off_mod_..., n_tcs}}
    Uses the same per-TC across-run std convention as compute_table_row.
    Excludes infra+wiring failures.
    """
    from collections import defaultdict
    # bin → tc_id → run_index → {role: [pass_bools]}
    by_bin: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(list))))

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try: d = json.loads(line)
            except json.JSONDecodeError: continue
            if "tc_id" not in d: continue
            if d.get("error_type") in ("infra", "timeout"): continue

            tc_id = d["tc_id"]
            n_obj = tc_objects.get(tc_id)
            if n_obj is None: continue
            b = _object_bin(n_obj)
            ri = d.get("run_index", 0)

            for e in (d.get("events") or []):
                if e.get("passed") is None: continue
                if e.get("infra_error"): continue  # LNL event-level infra flag
                if _classify_event(e) in ("oc_eval", "infra_provider"): continue
                role = e.get("role")
                if role is None:
                    by_bin[b][tc_id][ri]["base"].append(e["passed"])
                elif role == "pre_mod":
                    by_bin[b][tc_id][ri]["pre_mod"].append(e["passed"])
                elif role == "post_mod":
                    by_bin[b][tc_id][ri]["on_mod"].append(e["passed"])
                elif role == "irrelevant":
                    by_bin[b][tc_id][ri]["off_mod"].append(e["passed"])
                # mean: all non-step events regardless of role
                if not re.match(r"^S\d+$", e.get("event_id", "")):
                    by_bin[b][tc_id][ri]["mean"].append(e["passed"])

    out: dict[str, dict] = {}
    for b, tcs in by_bin.items():
        per_tc_means = {"mean": [], "base": [], "pre_mod": [], "on_mod": [], "off_mod": []}
        per_tc_stds  = {"mean": [], "base": [], "pre_mod": [], "on_mod": [], "off_mod": []}
        for tc_id, runs in tcs.items():
            for role in ("mean", "base", "pre_mod", "on_mod", "off_mod"):
                rates = []
                for ri, role_dict in runs.items():
                    bools = role_dict.get(role)
                    if not bools: continue
                    rates.append(sum(bools) / len(bools))
                if rates:
                    per_tc_means[role].append(float(np.mean(rates)))
                    per_tc_stds[role].append(float(np.std(rates)) if len(rates) > 1 else 0.0)

        row = {"n_tcs": len(tcs)}
        for role in ("mean", "base", "pre_mod", "on_mod", "off_mod"):
            tc_means = per_tc_means[role]
            n_role   = len(tc_means)
            if n_role:
                row[f"{role}_mean"] = float(np.mean(tc_means)) * 100
                row[f"{role}_std"]  = float(np.mean(per_tc_stds[role])) * 100
                if n_role > 1:
                    across_std = float(np.std(tc_means, ddof=1))
                    try:
                        from scipy import stats as _stats
                        t_crit = float(_stats.t.ppf(0.975, df=n_role - 1))
                    except ImportError:
                        t_crit = 1.96
                    row[f"{role}_ci95"] = t_crit * across_std / (n_role ** 0.5) * 100
                else:
                    row[f"{role}_ci95"] = 0.0
                row[f"{role}_n"]    = n_role
            else:
                row[f"{role}_mean"] = 0.0
                row[f"{role}_std"]  = 0.0
                row[f"{role}_ci95"] = 0.0
                row[f"{role}_n"]    = 0
        out[b] = row
    return out


def plot_by_objects(
    all_data: dict[str, dict[str, dict]],
    plots_dir: Path,
    palette: dict,
) -> None:
    """Three-panel figure: Base / On-Mod / Off-Mod pass rate vs n_objects bin.

    One line per system per panel. Shows how each paradigm degrades as
    workflow complexity (number of peer agents) grows.
    """
    p = palette
    plots_dir.mkdir(parents=True, exist_ok=True)

    # Filter to bins that actually have data somewhere
    bins_present = [b for b in _BIN_ORDER
                    if any(b in d and d[b]["n_tcs"] > 0 for d in all_data.values())]
    if not bins_present:
        print("  No by-object data to plot.")
        return

    metrics = [("mean_mean",    "mean_ci95",    "Mean"),
               ("base_mean",   "base_ci95",    "Base"),
               ("pre_mod_mean","pre_mod_ci95", "Pre-Mod"),
               ("on_mod_mean", "on_mod_ci95",  "On-Mod"),
               ("off_mod_mean","off_mod_ci95", "Off-Mod")]

    fig, axes = plt.subplots(1, 5, figsize=(20, 4.0), sharey=True)
    fig.patch.set_facecolor(p["bg"])
    colors = p["colors"]

    x = np.arange(len(bins_present))

    for ax_idx, (mean_key, std_key, title) in enumerate(metrics):
        ax = axes[ax_idx]
        _apply_style(fig, ax, p)
        for s_idx, (system, bins) in enumerate(all_data.items()):
            color = colors[s_idx % len(colors)]
            means = [bins[b][mean_key] if b in bins and bins[b]["n_tcs"] > 0 else None for b in bins_present]
            stds  = [bins[b][std_key]  if b in bins and bins[b]["n_tcs"] > 0 else 0     for b in bins_present]
            valid = [(xi, m, s) for xi, m, s in zip(x, means, stds) if m is not None]
            if not valid: continue
            xs, ms, ss = zip(*valid)
            ax.plot(xs, ms, marker="o", color=color, linewidth=2.0,
                    markersize=6, label=system, zorder=3)
            ax.fill_between(xs, [m - s for m, s in zip(ms, ss)],
                            [m + s for m, s in zip(ms, ss)],
                            color=color, alpha=0.12, linewidth=0, zorder=2)
        ax.set_xticks(x)
        ax.set_xticklabels(bins_present, fontsize=10, color=p["text"])
        ax.set_xlabel(f"Workflow size (#objects) — {title}", fontsize=10, color=p["text"])
        ax.set_ylim(0, 100)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
        if ax_idx == 0:
            ax.set_ylabel("Pass rate", fontsize=11, color=p["text"])

    # Single shared legend in the rightmost subplot
    leg = axes[-1].legend(
        loc="upper right", fontsize=9,
        frameon=True, framealpha=1.0,
        edgecolor=p["gridline"], facecolor=p["bg"],
        labelcolor=p["text"],
    )
    leg.get_frame().set_linewidth(0.8)

    out = plots_dir / "mod_types_by_objects.pdf"
    fig.savefig(out, bbox_inches="tight", facecolor=p["bg"])
    plt.close(fig)
    print(f"  Saved → {out}")


def plot_by_objects_single(
    all_data: dict[str, dict[str, dict]],
    plots_dir: Path,
    palette: dict,
    mean_key: str = "mean_mean",
    ci_key: str   = "mean_ci95",
    title: str    = "Mean pass rate",
    filename: str = "mod_types_by_objects_mean.pdf",
) -> None:
    """Single-panel version of plot_by_objects for one metric."""
    p = palette
    plots_dir.mkdir(parents=True, exist_ok=True)

    bins_present = [b for b in _BIN_ORDER
                    if any(b in d and d[b]["n_tcs"] > 0 for d in all_data.values())]
    if not bins_present:
        return

    fig, ax = plt.subplots(figsize=(6.0, 4.0))
    _apply_style(fig, ax, p)
    colors = p["colors"]
    x = np.arange(len(bins_present))

    for s_idx, (system, bins) in enumerate(all_data.items()):
        color = colors[s_idx % len(colors)]
        means = [bins[b][mean_key] if b in bins and bins[b]["n_tcs"] > 0 else None for b in bins_present]
        cis   = [bins[b][ci_key]   if b in bins and bins[b]["n_tcs"] > 0 else 0     for b in bins_present]
        valid = [(xi, m, s) for xi, m, s in zip(x, means, cis) if m is not None]
        if not valid:
            continue
        xs, ms, ss = zip(*valid)
        ax.plot(xs, ms, marker="o", color=color, linewidth=2.0,
                markersize=6, label=system, zorder=3)
        ax.fill_between(xs, [m - s for m, s in zip(ms, ss)],
                        [m + s for m, s in zip(ms, ss)],
                        color=color, alpha=0.12, linewidth=0, zorder=2)

    ax.set_xticks(x)
    ax.set_xticklabels(bins_present, fontsize=10, color=p["text"])
    ax.set_xlabel("Workflow size (#objects)", fontsize=10, color=p["text"])
    ax.set_ylabel(title, fontsize=11, color=p["text"])
    ax.set_ylim(0, 100)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))

    leg = ax.legend(
        loc="upper right", fontsize=9,
        frameon=True, framealpha=1.0,
        edgecolor=p["gridline"], facecolor=p["bg"],
        labelcolor=p["text"],
    )
    leg.get_frame().set_linewidth(0.8)

    out = plots_dir / filename
    fig.savefig(out, bbox_inches="tight", facecolor=p["bg"])
    plt.close(fig)
    print(f"  Saved → {out}")


def write_tex_table_by_objects(
    all_data: dict[str, dict[str, dict]],
    out_path: Path,
) -> None:
    """Emit a second LaTeX artifact: per (system × n_objects_bin) pass rates.

    all_data: {system_label: {bin_label: row_dict_from_compute_table_by_objects}}
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        r"% Per-system, per-object-count breakdown of modification pass rates.",
        r"% Bins are TCs grouped by number of objects in the workflow.",
        r"% Cells: mean $\pm$ 95\% CI (Student's t, across-TC variance).",
        r"% Infra + wiring failures excluded.",
        r"\begin{tabular}{l|c|c|c|c|c|c}",
        r"    \hline",
        r"    \textbf{System} & \textbf{Objects} & \textbf{Mean} & \textbf{Base} & \textbf{Pre-Mod} & \textbf{On-Mod} & \textbf{Off-Mod} \\",
        r"    \hline",
    ]
    # Per-bin winners (best system within each object-count bin, per role).
    win_by_bin: dict[str, dict[str, set[str]]] = {}
    for b in _BIN_ORDER:
        win_by_bin[b] = {
            role: _winners(
                [(sys_lbl, bins.get(b, {}).get(f"{role}_mean"))
                 for sys_lbl, bins in all_data.items()
                 if bins.get(b, {}).get("n_tcs", 0) > 0],
                "max",
            )
            for role in ("mean", "base", "pre_mod", "on_mod", "off_mod")
        }

    for system, bins in all_data.items():
        first = True
        for b in _BIN_ORDER:
            if b not in bins or bins[b]["n_tcs"] == 0: continue
            r = bins[b]
            sys_cell  = system if first else ""
            mean_cell = _bf(_fmt_rate(r['mean_mean'],    r['mean_ci95']),    system in win_by_bin[b]["mean"])
            base_cell = _bf(_fmt_rate(r['base_mean'],    r['base_ci95']),    system in win_by_bin[b]["base"])
            pre_cell  = _bf(_fmt_rate(r['pre_mod_mean'], r['pre_mod_ci95']), system in win_by_bin[b]["pre_mod"])
            on_cell   = _bf(_fmt_rate(r['on_mod_mean'],  r['on_mod_ci95']),  system in win_by_bin[b]["on_mod"])
            off_cell  = _bf(_fmt_rate(r['off_mod_mean'], r['off_mod_ci95']), system in win_by_bin[b]["off_mod"])
            lines.append(
                f"    {sys_cell:<24} & {b:<6}  ({r['n_tcs']:>3} TCs)"
                + f"  & {mean_cell:<15}"
                + f"  & {base_cell:<15}"
                + f"  & {pre_cell:<15}"
                + f"  & {on_cell:<15}"
                + f"  & {off_cell:<15} \\\\"
            )
            first = False
        lines.append(r"    \hline")
    lines.append(r"\end{tabular}")
    out_path.write_text("\n".join(lines) + "\n")
    print(f"  Saved → {out_path}")


def write_tex_table(
    rows: list[tuple[str, dict]],
    out_path: Path,
) -> None:
    """Write a LaTeX `tabular` artifact with one row per system.

    rows: list of (system_label, table_row_dict) tuples in display order.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    label_w = max(len(label) for label, _ in rows) if rows else 8
    label_w = max(label_w, 24)  # match user's example width

    lines = [
        r"% Pass rates: mean across TCs $\pm$ 95\% CI (Student's t, across-TC variance).",
        r"% $\bar{H}$ = mean step-level Shannon entropy (uncorrected plug-in estimator) per (TC, event) over runs.",
        r"% Time/Tokens: per-TC mean. Infra + wiring failures excluded.",
        r"\begin{tabular}{l|c|c|c|c|c|c}",
        r"    \hline",
        r"    \textbf{System}"
        + " " * (label_w - len("System"))
        + r" & \textbf{Base}   & \textbf{On-Mod} & \textbf{Off-Mod} "
          r"& $\,\overline{\mathbf{H}}\,$ & \textbf{Time (s)} & \textbf{Tokens} \\",
        r"    \hline",
    ]
    # Per-column winners (rate columns: max; cost/entropy: min)
    win = {
        "base_mean":      _winners([(lbl, r.get("base_mean"))      for lbl, r in rows], "max"),
        "on_mod_mean":    _winners([(lbl, r.get("on_mod_mean"))    for lbl, r in rows], "max"),
        "off_mod_mean":   _winners([(lbl, r.get("off_mod_mean"))   for lbl, r in rows], "max"),
        "step_entropy":   _winners([(lbl, r.get("step_entropy"))   for lbl, r in rows], "min"),
        "elapsed_s":      _winners([(lbl, r.get("elapsed_s"))      for lbl, r in rows], "min"),
        "tokens_in_mean": _winners([(lbl, r.get("tokens_in_mean")) for lbl, r in rows], "min"),
    }

    for label, r in rows:
        # Format tokens with thousands separators, "in/out" pattern
        tok_in = f"{int(r['tokens_in_mean']):,}"
        tok_out = f"{int(r['tokens_out_mean']):,}"
        tokens_str = f"{tok_in}/{tok_out}"
        h_str   = f"{r['step_entropy']:>5.3f}" if r.get('step_entropy') is not None else "  --  "
        base_cell = _bf(_fmt_rate(r['base_mean'],   r['base_ci95']),   label in win["base_mean"])
        on_cell   = _bf(_fmt_rate(r['on_mod_mean'], r['on_mod_ci95']), label in win["on_mod_mean"])
        off_cell  = _bf(_fmt_rate(r['off_mod_mean'],r['off_mod_ci95']),label in win["off_mod_mean"])
        h_cell    = _bf(h_str,      label in win["step_entropy"])
        time_cell = _bf(f"{r['elapsed_s']:.2f}", label in win["elapsed_s"])
        tok_cell  = _bf(tokens_str, label in win["tokens_in_mean"])
        lines.append(
            "    "
            + label.ljust(label_w)
            + f" & {base_cell:<16}"
            + f" & {on_cell:<16}"
            + f" & {off_cell:<16}"
            + f" & {h_cell:>7}"
            + f" & {time_cell:>17}"
            + f" & {tok_cell:<15} \\\\"
        )
    lines.append(r"    \hline")
    lines.append(r"\end{tabular}")
    out_path.write_text("\n".join(lines) + "\n")
    print(f"  Saved → {out_path}")


# ── File discovery ────────────────────────────────────────────────────────────

def discover_source_tcs(files: dict[str, Path]) -> Optional[Path]:
    """Read input_path from the run_config record of any result file.

    If the recorded path no longer exists, falls back to known canonical
    locations (the file may have been renamed since the run).
    """
    _FALLBACKS = [
        Path("data/zapier/workflows-mods.jsonl"),
        Path("data/zapier/test_cases.jsonl"),
    ]

    candidate: Optional[Path] = None
    for path in files.values():
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    d = json.loads(line)
                    ip = d.get("input_path")
                    if ip:
                        candidate = Path(ip)
                        break
                    if "tc_id" in d:
                        break
        except Exception:
            continue
        if candidate:
            break

    # Return the recorded path if it exists, otherwise try fallbacks.
    if candidate and candidate.exists():
        return candidate
    for fb in _FALLBACKS:
        if fb.exists():
            return fb
    return None


def discover_files(directory: Path) -> dict[str, Path]:
    """Auto-detect LNL / single / multi result files from a directory."""
    candidates: dict[str, Path] = {}
    jsonl_files = sorted(directory.glob("*.jsonl"))

    single_candidates, multi_candidates, lnl_candidates = [], [], []

    for p in jsonl_files:
        name = p.name.lower()
        if "eval" in name and "baseline" not in name:
            lnl_candidates.append(p)
        elif "baseline" in name and "single" in name:
            single_candidates.append(p)
        elif "baseline" in name:
            multi_candidates.append(p)

    # pick most recent (last alphabetically / by timestamp in name)
    if lnl_candidates:
        candidates["lnl"] = lnl_candidates[-1]
    if single_candidates:
        candidates["single"] = single_candidates[-1]
    if multi_candidates:
        candidates["multi"] = multi_candidates[-1]

    return candidates


# ── Plotting ──────────────────────────────────────────────────────────────────

def _apply_style(fig, ax, palette: dict) -> None:
    """Minimal, matches the convention in plot_concurrency.py et al:
    hide top/left/right spines, keep only a single bottom rule with the
    axis color. Horizontal gridlines stay subtle.
    """
    p = palette
    fig.patch.set_facecolor(p["bg"])
    ax.set_facecolor(p["bg"])
    ax.tick_params(colors=p["text"], labelsize=9, length=0)
    ax.xaxis.label.set_color(p["text"])
    ax.yaxis.label.set_color(p["text"])
    ax.title.set_color(p["text"])
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(p["axis"])
    ax.spines["bottom"].set_linewidth(0.8)
    ax.yaxis.grid(True, color=p["gridline"], linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)


def plot_mod_types(
    series_data: dict[str, dict[str, tuple[float, float, int]]],
    plots_dir: Path,
    metric: str = DEFAULT_METRIC,
    palette: dict = None,
    judge_label: str = "",
) -> None:
    p = palette or PALETTES[DEFAULT_PALETTE]

    # Determine which mod types appear in data
    all_mod_types = [mt for mt in MOD_TYPES_ORDER if any(mt in d for d in series_data.values())]
    present_series = [(key, label) for key, label in SERIES if key in series_data]

    if not all_mod_types or not present_series:
        print("  No data to plot.")
        return

    n_groups  = len(all_mod_types)
    n_series  = len(present_series)
    bar_w     = 0.22
    group_gap = 0.08
    group_w   = n_series * bar_w + group_gap
    x_centers = np.arange(n_groups) * group_w

    fig, ax = plt.subplots(figsize=(max(8, n_groups * group_w * 1.8 + 1.5), 4.5))
    _apply_style(fig, ax, p)

    colors = p["colors"]

    for s_idx, (key, label) in enumerate(present_series):
        data  = series_data[key]
        color = colors[s_idx % len(colors)]
        offsets = x_centers + (s_idx - (n_series - 1) / 2) * bar_w

        means  = [data.get(mt, (0.0, 0.0, 0))[0] for mt in all_mod_types]
        stds   = [data.get(mt, (0.0, 0.0, 0))[1] for mt in all_mod_types]
        counts = [data.get(mt, (0.0, 0.0, 0))[2] for mt in all_mod_types]

        bars = ax.bar(
            offsets, means,
            width=bar_w,  # no intra-group spacing — bars in a group stick together
            color=color, alpha=0.85,
            label=label,
            zorder=3,
            linewidth=0,
        )
        # Value labels on top of each bar
        for bar, val, n in zip(bars, means, counts):
            if val > 0:
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    val + 0.015,
                    f"{val:.0%}",
                    ha="center", va="bottom",
                    fontsize=7, color=p["text_dim"],
                )

    ax.set_xticks(x_centers)
    ax.set_xticklabels(
        [mt.capitalize() for mt in all_mod_types],
        fontsize=10, color=p["text"],
    )
    ax.set_ylim(0, 1.08)
    ax.set_ylabel(METRICS[metric], fontsize=11)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))

    leg = ax.legend(
        handles=[
            mpatches.Patch(color=colors[i % len(colors)], label=label)
            for i, (_, label) in enumerate(present_series)
        ],
        fontsize=9, loc="upper right",
        frameon=True, framealpha=1.0,
        edgecolor=p["gridline"], facecolor=p["bg"],
        labelcolor=p["text"],
    )
    leg.get_frame().set_linewidth(0.8)

    plots_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"__{judge_label}" if judge_label else ""
    stem   = f"mod_types_{metric}{suffix}"
    out    = plots_dir / f"{stem}.pdf"
    fig.savefig(out, bbox_inches="tight", facecolor=p["bg"])
    plt.close(fig)
    print(f"  Saved → {out}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Grouped bar chart of pass rate by modification type.",
    )
    parser.add_argument(
        "directory", nargs="?", type=Path,
        default=None,
        help="Directory containing result JSONL files (auto-detected by name).",
    )
    parser.add_argument("--lnl",    type=Path, default=None, help="LNL result JSONL")
    parser.add_argument("--multi",  type=Path, default=None, help="OC multi-agent result JSONL")
    parser.add_argument("--single", type=Path, default=None, help="OC single-agent result JSONL")
    parser.add_argument(
        "--metric", default=DEFAULT_METRIC, choices=list(METRICS.keys()),
        help=f"Metric to plot (default: {DEFAULT_METRIC}). "
             f"Choices: {', '.join(METRICS.keys())}",
    )
    parser.add_argument(
        "--palette", default=DEFAULT_PALETTE, choices=list(PALETTES.keys()),
        help=f"Color palette (default: {DEFAULT_PALETTE}). "
             f"Choices: {', '.join(PALETTES.keys())}",
    )
    parser.add_argument("--output-dir", "-o", type=Path, default=None,
                        help="Output directory for PDFs (default: <directory>/plots/ or next to --lnl)")
    parser.add_argument("--tex", action=argparse.BooleanOptionalAction, default=True,
                        help="Also emit a LaTeX summary table (.tex) alongside the plot. "
                             "Columns: Base / On-Mod / Off-Mod pass rates (mean±std), Time(s), Tokens(in/out). "
                             "Infra + wiring failures excluded. Default: ENABLED.")
    parser.add_argument("--tex-name", default="mod_types_summary.tex",
                        help="Filename for the LaTeX table artifact (default: mod_types_summary.tex)")
    parser.add_argument("--source-tcs", type=Path, default=None,
                        help="Source workflows-mods.jsonl (used to count objects per TC for "
                             "the by-objects breakdown). Auto-detected from run_config if omitted.")
    parser.add_argument("--by-objects", action=argparse.BooleanOptionalAction, default=True,
                        help="Also emit a per-(system × n_objects bin) LaTeX table "
                             "(mod_types_by_objects.tex). Default: ENABLED.")
    args = parser.parse_args()

    # Resolve file paths
    files: dict[str, Path] = {}
    if args.lnl:
        files["lnl"] = args.lnl
    if args.multi:
        files["multi"] = args.multi
    if args.single:
        files["single"] = args.single

    if not files:
        if args.directory is None:
            parser.error("Provide a directory or at least one of --lnl / --multi / --single.")
        discovered = discover_files(args.directory)
        if not discovered:
            parser.error(f"No result JSONL files found in {args.directory}")
        files = discovered

    # Output dir
    if args.output_dir:
        plots_dir = args.output_dir
    elif args.directory:
        plots_dir = args.directory / "plots"
    else:
        plots_dir = next(iter(files.values())).parent / "plots"

    palette = PALETTES[args.palette]

    # Load data
    series_data: dict[str, dict[str, tuple[float, float, int]]] = {}
    for key, path in files.items():
        if not path.exists():
            print(f"  [warn] File not found: {path}")
            continue
        label = dict(SERIES)[key]
        print(f"  Loading {label}: {path}")
        series_data[key] = load_mod_rates(path, args.metric)
        for mt, (mean_r, std_r, n) in sorted(series_data[key].items()):
            print(f"    {mt:15s}  {mean_r:.1%}  (n={n})")

    plot_mod_types(
        series_data=series_data,
        plots_dir=plots_dir,
        metric=args.metric,
        palette=palette,
    )

    # LaTeX summary table artifact
    if args.tex:
        rows: list[tuple[str, dict]] = []
        for key, label in SERIES:
            if key in files and files[key].exists():
                print(f"  Computing table row: {label}")
                rows.append((label, compute_table_row(files[key])))
        if rows:
            tex_path = plots_dir / args.tex_name
            write_tex_table(rows, tex_path)
            # Echo the rendered table to stdout for convenience
            print()
            print(tex_path.read_text())

    # By-objects-count breakdown — second LaTeX artifact
    if args.by_objects:
        source_tcs = args.source_tcs or discover_source_tcs(files)
        if source_tcs is None:
            print("  [warn] could not determine source TCs path — skipping --by-objects")
            source_tcs = Path("__missing__")
        tc_objects = load_tc_object_counts(source_tcs)
        if not tc_objects:
            print(f"  [warn] could not load TC object counts from {source_tcs} — skipping --by-objects")
        else:
            print(f"\n  Computing by-objects breakdown (from {source_tcs}, {len(tc_objects)} TCs indexed)")
            by_obj_data: dict[str, dict] = {}
            for key, label in SERIES:
                if key in files and files[key].exists():
                    by_obj_data[label] = compute_table_by_objects(files[key], tc_objects)
            if by_obj_data:
                by_obj_path = plots_dir / "mod_types_by_objects.tex"
                write_tex_table_by_objects(by_obj_data, by_obj_path)
                plot_by_objects(by_obj_data, plots_dir, palette)
                plot_by_objects_single(by_obj_data, plots_dir, palette)
                # Echo the table to stdout
                print()
                print(by_obj_path.read_text())


if __name__ == "__main__":
    main()
