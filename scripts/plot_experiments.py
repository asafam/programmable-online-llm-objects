#!/usr/bin/env python3
"""Plot experiment results across concurrency levels and paradigms.

Usage:
    python scripts/plot_experiments.py [exp_dir]

Reads files matching: exp_{lnl|baseline}_{N}mod_conc{C}.jsonl
Generates plots saved to <exp_dir>/plots/:
  - concurrency_x_modifications_passrate[__<judge>].png  — one per judge found
  - concurrency_x_modifications_elapsed.png
  - concurrency_x_modifications_tokens.png

If a file contains rejudge entries, one additional pass-rate plot is generated
per rejudge model, using that model's verdicts instead of the original ones.
Elapsed and token plots are judge-independent and generated once.
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mtick

# ── Metric definitions ────────────────────────────────────────────────────────

PASS_METRICS = [
    ("mean",       "Mean pass rate"),
    ("steps",      "Steps pass rate"),
    ("mod",        "Mod pass rate (pre+post)"),
    ("pre_mod",    "Pre-mod pass rate"),
    ("post_mod",   "Post-mod pass rate"),
    ("irrelevant", "Irrelevant pass rate"),
]

ELAPSED_METRICS = [
    ("elapsed_mean_s",  "Mean elapsed time / TC (s)"),
    ("elapsed_p90_s",   "P90 elapsed time / TC (s)"),
]

TOKEN_METRICS = [
    ("mean_in_tok",  "Mean agent input tokens / event"),
    ("mean_out_tok", "Mean agent output tokens / event"),
    ("mean_judge_in_tok",  "Mean judge input tokens / event"),
    ("mean_judge_out_tok", "Mean judge output tokens / event"),
]

STEP_ID = re.compile(r"^S\d+$")

PARADIGM_LABEL = {"lnl": "Ours",    "baseline": "OpenClaw"}
PARADIGM_COLOR = {"lnl": "#2196F3", "baseline": "#E64A19"}
MOD_LINESTYLE  = {1: "-",  2: "--"}
MOD_MARKER     = {1: "o",  2: "s"}
MOD_ALPHA      = {1: 1.0,  2: 0.75}

# Sentinel for the original (non-rejudge) judge
_ORIGINAL = "__original__"


# ── File scanning ─────────────────────────────────────────────────────────────

def _scan_judges(path: Path) -> tuple[str, list[str]]:
    """Return (original_judge_model, [rejudge_model, ...]) found in a file."""
    original = "unknown"
    rejudge_models: set[str] = set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if "tc_id" not in d:
                # meta / run_config record
                original = (d.get("judge_model")
                            or (d.get("params") or {}).get("judge_model")
                            or original)
                continue
            for e in d.get("events", []):
                for rj in e.get("rejudges", []):
                    if rj.get("model"):
                        rejudge_models.add(rj["model"])
    return original, sorted(rejudge_models)


# ── Metric computation ────────────────────────────────────────────────────────

def _get_passed_original(evt: dict):
    return evt.get("passed")


def _make_get_passed_rejudge(model: str):
    def _get(evt: dict):
        for rj in evt.get("rejudges", []):
            if rj.get("model") == model:
                return rj.get("passed")
        return None  # event not scored by this model
    return _get


def compute_metrics(path: Path, get_passed=_get_passed_original) -> dict:
    results = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if "tc_id" not in d:
                continue
            results.append(d)

    if not results:
        return {}

    # Step deduplication: only first TC per sample contributes step events.
    first_tc_per_sample: dict[str, str] = {}
    for r in results:
        sid = r.get("sample_id") or r["tc_id"]
        if sid not in first_tc_per_sample:
            first_tc_per_sample[sid] = r["tc_id"]
    base_tc_ids = set(first_tc_per_sample.values())

    by_role: dict[str, dict] = defaultdict(lambda: {"pass": 0, "total": 0})
    pass_rates: list[float] = []
    elapsed_s: list[float] = []
    in_toks: list[float] = []
    out_toks: list[float] = []
    judge_in_toks: list[float] = []
    judge_out_toks: list[float] = []

    for r in results:
        is_base = r["tc_id"] in base_tc_ids
        events = r.get("events", [])

        if r.get("elapsed_ms") is not None:
            elapsed_s.append(r["elapsed_ms"] / 1000)

        # Pass rates (step-deduplicated); skip events where get_passed returns None
        effective = [e for e in events if is_base or not STEP_ID.match(e["event_id"])]
        scored = [e for e in effective if get_passed(e) is not None]
        if scored:
            pass_rates.append(sum(1 for e in scored if get_passed(e)) / len(scored))

        for e in events:
            eid = e["event_id"]
            role = e.get("role")
            if STEP_ID.match(eid):
                if not is_base:
                    continue
                role = "step"
            elif not role:
                continue
            p = get_passed(e)
            if p is None:
                continue  # not scored by this judge — skip
            by_role[role]["pass"]  += 1 if p else 0
            by_role[role]["total"] += 1
            if e.get("input_tokens"):
                in_toks.append(e["input_tokens"])
            if e.get("output_tokens"):
                out_toks.append(e["output_tokens"])
            if e.get("judge_input_tokens"):
                judge_in_toks.append(e["judge_input_tokens"])
            if e.get("judge_output_tokens"):
                judge_out_toks.append(e["judge_output_tokens"])

    def rate(role: str):
        s = by_role.get(role)
        return s["pass"] / s["total"] if s and s["total"] else None

    def mean(xs): return sum(xs) / len(xs) if xs else None
    def p90(xs):
        if not xs: return None
        xs_s = sorted(xs)
        return xs_s[int(len(xs_s) * 0.9)]

    mod_pass  = sum(by_role[r]["pass"]  for r in ("pre_mod", "post_mod"))
    mod_total = sum(by_role[r]["total"] for r in ("pre_mod", "post_mod"))

    return {
        "mean":       mean(pass_rates),
        "steps":      rate("step"),
        "mod":        mod_pass / mod_total if mod_total else None,
        "pre_mod":    rate("pre_mod"),
        "post_mod":   rate("post_mod"),
        "irrelevant": rate("irrelevant"),
        "elapsed_mean_s": mean(elapsed_s),
        "elapsed_p90_s":  p90(elapsed_s),
        "mean_in_tok":       mean(in_toks),
        "mean_out_tok":      mean(out_toks),
        "mean_judge_in_tok": mean(judge_in_toks),
        "mean_judge_out_tok":mean(judge_out_toks),
        "n_tcs": len(results),
    }


# ── Data loading ──────────────────────────────────────────────────────────────

def load_experiments(exp_dir: Path) -> tuple[dict, dict]:
    """Return (data_by_judge, original_judge_by_file).

    data_by_judge: {judge_key: {(paradigm, mods): {conc: metrics}}}
        judge_key is _ORIGINAL for the original verdicts, or the rejudge model name.

    original_judge_by_file: {filename: original_model_name}  — for display.
    """
    pattern = re.compile(r"exp_(lnl|baseline)_(\d+)mod_conc(\d+)\.jsonl$")
    data_by_judge: dict[str, dict] = defaultdict(lambda: defaultdict(dict))
    original_names: dict[str, str] = {}  # judge_key → display name for original

    paths = sorted(exp_dir.glob("exp_*.jsonl"))
    for path in paths:
        m = pattern.match(path.name)
        if not m:
            continue
        paradigm, mods, conc = m.group(1), int(m.group(2)), int(m.group(3))

        orig_model, rejudge_models = _scan_judges(path)
        original_names[_ORIGINAL] = original_names.get(_ORIGINAL, orig_model)

        # Original judge
        metrics = compute_metrics(path, _get_passed_original)
        if not metrics:
            print(f"  Skipping {path.name} (empty)")
            continue
        data_by_judge[_ORIGINAL][(paradigm, mods)][conc] = metrics
        mean_str = f"{metrics['mean']:.1%}" if metrics.get("mean") is not None else "N/A"
        print(f"  {path.name}: {metrics['n_tcs']} TCs  mean={mean_str} (judge: {orig_model})")

        # Rejudge models
        for rj_model in rejudge_models:
            get_passed = _make_get_passed_rejudge(rj_model)
            rj_metrics = compute_metrics(path, get_passed)
            if rj_metrics:
                data_by_judge[rj_model][(paradigm, mods)][conc] = rj_metrics
                rj_mean = f"{rj_metrics['mean']:.1%}" if rj_metrics.get("mean") is not None else "N/A"
                print(f"    └─ rejudge {rj_model}: mean={rj_mean}")

    return dict(data_by_judge), original_names


# ── Plotting helpers ──────────────────────────────────────────────────────────

def _draw_lines(ax, data, metric_key, all_concs):
    plotted = False
    for (paradigm, mods), series in sorted(data.items()):
        concs = sorted(series)
        values = [series[c].get(metric_key) for c in concs]
        if all(v is None for v in values):
            continue
        y = [v if v is not None else float("nan") for v in values]
        label = f"{PARADIGM_LABEL[paradigm]} ({mods} mod{'s' if mods > 1 else ''})"
        ax.plot(
            concs, y,
            linestyle=MOD_LINESTYLE.get(mods, "-"),
            marker=MOD_MARKER.get(mods, "o"),
            color=PARADIGM_COLOR[paradigm],
            alpha=MOD_ALPHA.get(mods, 1.0),
            label=label, linewidth=2, markersize=7,
        )
        plotted = True
    if all_concs:
        ax.set_xticks(all_concs)
    ax.set_xlabel("Concurrency level", fontsize=9)
    ax.grid(True, alpha=0.3, linestyle=":")
    if plotted:
        ax.legend(fontsize=8, loc="best")
    else:
        ax.text(0.5, 0.5, "No data", transform=ax.transAxes,
                ha="center", va="center", color="gray")
    return plotted


def _save(fig, path: Path, title: str):
    fig.suptitle(title, fontsize=13, fontweight="bold")
    plt.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(path, dpi=150, bbox_inches="tight")
    print(f"  Saved: {path}")
    plt.close(fig)


def _filename_safe(model: str) -> str:
    return re.sub(r"[^\w\-.]", "_", model)


# ── Plot functions ────────────────────────────────────────────────────────────

def plot_passrate(data: dict, plots_dir: Path, judge_label: str, filename_suffix: str = "") -> None:
    all_concs = sorted({c for s in data.values() for c in s})
    fig, axes = plt.subplots(2, 3, figsize=(15, 9))
    for ax, (key, label) in zip(axes.flatten(), PASS_METRICS):
        _draw_lines(ax, data, key, all_concs)
        ax.set_title(label, fontsize=11, fontweight="bold")
        ax.set_ylabel("Pass rate", fontsize=9)
        ax.yaxis.set_major_formatter(mtick.PercentFormatter(1.0))
        ax.set_ylim(-0.02, 1.05)
    fname = "concurrency_x_modifications_passrate"
    if filename_suffix:
        fname += f"__{filename_suffix}"
    fname += ".png"
    _save(fig, plots_dir / fname,
          f"Ours vs OpenClaw — Pass Rates  (judge: {judge_label})")


def plot_elapsed(data: dict, plots_dir: Path) -> None:
    all_concs = sorted({c for s in data.values() for c in s})
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    for ax, (key, label) in zip(axes, ELAPSED_METRICS):
        _draw_lines(ax, data, key, all_concs)
        ax.set_title(label, fontsize=11, fontweight="bold")
        ax.set_ylabel("Seconds", fontsize=9)
    _save(fig, plots_dir / "concurrency_x_modifications_elapsed.png",
          "Ours vs OpenClaw — Elapsed Time per TC")


def plot_tokens(data: dict, plots_dir: Path) -> None:
    all_concs = sorted({c for s in data.values() for c in s})
    fig, axes = plt.subplots(2, 2, figsize=(12, 9))
    for ax, (key, label) in zip(axes.flatten(), TOKEN_METRICS):
        _draw_lines(ax, data, key, all_concs)
        ax.set_title(label, fontsize=11, fontweight="bold")
        ax.set_ylabel("Tokens", fontsize=9)
    _save(fig, plots_dir / "concurrency_x_modifications_tokens.png",
          "Ours vs OpenClaw — Token Usage per Event")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    exp_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("outputs/experiments/exp_mod_conc")

    if not exp_dir.exists():
        print(f"Directory not found: {exp_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading experiments from {exp_dir} ...")
    data_by_judge, original_names = load_experiments(exp_dir)

    if not data_by_judge:
        print("No matching experiment files found.", file=sys.stderr)
        sys.exit(1)

    plots_dir = exp_dir / "plots"
    all_judge_keys = sorted(data_by_judge)
    multi_judge = len(all_judge_keys) > 1

    print(f"\nGenerating plots → {plots_dir}")

    for judge_key in all_judge_keys:
        data = data_by_judge[judge_key]
        if judge_key == _ORIGINAL:
            display = original_names.get(_ORIGINAL, "original")
            suffix = _filename_safe(display) if multi_judge else ""
        else:
            display = judge_key
            suffix = _filename_safe(judge_key)
        plot_passrate(data, plots_dir, judge_label=display, filename_suffix=suffix)

    # Elapsed and tokens use the original judge data (judge-independent metrics)
    orig_data = data_by_judge[_ORIGINAL]
    plot_elapsed(orig_data, plots_dir)
    plot_tokens(orig_data, plots_dir)

    print("Done.")


if __name__ == "__main__":
    main()
