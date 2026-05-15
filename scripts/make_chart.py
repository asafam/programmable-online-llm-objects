"""
Probe accuracy by depth — sleek modern chart for POLO paper.

Reproduces the gradient-area-fill aesthetic with two lines and a choice of palette.
Outputs a vector PDF suitable for LaTeX inclusion.

Usage:
    # Load data from JSONL files (recommended):
    python scripts/make_chart.py \\
        --lnl-probes    outputs/data/zapier/.../probes_lnl.jsonl \\
        --baseline-probes outputs/data/zapier/.../probes_baseline_multi.jsonl \\
        --baseline-label "OpenClaw (multi-agent)" \\
        --palette botanical --output figures/depth_chart.pdf

    # Use hardcoded data constants (fallback):
    python scripts/make_chart.py --palette botanical --output figures/depth_chart.pdf
"""

import argparse
import json
import math
import re
import statistics
import sys
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np
from collections import defaultdict
from pathlib import Path


# ---------- DATA ----------
# Depths are x-axis positions (number of prior events before the probe question).
# mean = probe accuracy %; std = SEM (uncertainty of the mean estimate).
# Source: probes_v6_corr_prop  (probes_lnl.jsonl, probes_baseline_multi.jsonl)
DEPTHS = [5, 10, 20, 30]

SERIES = {
    'POLO (ours)': {
        'mean': [72.9, 88.6, 77.7, 75.3],
        'std':  [3.5,   2.5,  3.2,  3.4],   # SEM (n=166 per depth)
    },
    'OpenClaw (multi-agent)': {
        'mean': [36.5, 24.4, 16.5,  6.6],
        'std':  [5.6,   4.8,  3.9,  2.4],   # SEM (n=74–106 per depth)
    },
}


# ---------- PALETTES ----------
# Each palette defines: bg, gridline, axis, text, and one color per series in order.
PALETTES = {
    'botanical': {
        'bg':       '#F2EDDE',
        'gridline': '#D4CBB0',
        'axis':     '#A89A75',
        'text':     '#5A5040',
        'text_dim': '#7A6E50',
        'colors':   ['#2D5A3E', '#A04E2D', '#A8862D'],   # forest, rust, gold
        'linestyles': ['solid', (0, (8, 4)), (0, (2, 3))],
        'markers':    ['o', 's', '^'],
    },
    'pastel': {
        'bg':       '#E8E4DD',
        'gridline': '#CCC6B8',
        'axis':     '#9E9684',
        'text':     '#5C5444',
        'text_dim': '#7A7263',
        'colors':   ['#C56F89', '#588F92', '#A89668'],   # pink, teal, buttercream
        'linestyles': ['solid', (0, (8, 4)), (0, (2, 3))],
        'markers':    ['o', 's', '^'],
    },
    'monochrome': {
        'bg':       '#FFFFFF',
        'gridline': '#EEEEEE',
        'axis':     '#333333',
        'text':     '#222222',
        'text_dim': '#666666',
        'colors':   ['#0066CC', '#666666', '#AAAAAA'],   # blue, mid grey, light grey
        'linestyles': ['solid', (0, (8, 4)), (0, (2, 3))],
        'markers':    ['o', 's', '^'],
    },
    'okabe': {
        'bg':       '#FFFFFF',
        'gridline': '#E5E5E5',
        'axis':     '#333333',
        'text':     '#222222',
        'text_dim': '#666666',
        'colors':   ['#D55E00', '#009E73', '#56B4E9'],   # vermillion, bluish green, sky blue
        'linestyles': ['solid', (0, (8, 4)), (0, (2, 3))],
        'markers':    ['o', 's', '^'],
    },
    'riso': {
        # White background — dark navy / cyan / coral (matches diff_riso reference)
        'bg':       '#FFFFFF',
        'gridline': '#E0DACE',
        'axis':     '#12123c',
        'text':     '#333333',
        'text_dim': '#666666',
        'colors':   ['#12123c', '#00b4d8', '#e63946'],   # dark navy, cyan, coral red
        'linestyles': ['solid', (0, (8, 4)), (0, (2, 3))],
        'markers':    ['o', 's', '^'],
    },
}


# ---------- DATA LOADING ----------
_PROBE_RE = re.compile(r"-probe\d*-D(\d+)-")


def _load_probe_series(path: Path, label: str) -> tuple[list[int], dict]:
    """Load probe JSONL → (depths, series_dict) where series_dict matches SERIES format."""
    by_depth: dict[int, list[float]] = defaultdict(list)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if "tc_id" not in d or d.get("error_type") == "infra":
                continue
            m = _PROBE_RE.search(d["tc_id"])
            if not m:
                continue
            depth = int(m.group(1))
            probe_events = [e for e in d.get("events", []) if e.get("role") == "post_mod"]
            if not probe_events:
                continue
            acc = sum(1 for e in probe_events if e.get("passed")) / len(probe_events)
            by_depth[depth].append(acc)

    depths = sorted(by_depth)
    means, sems = [], []
    for dep in depths:
        vals = by_depth[dep]
        n    = len(vals)
        mean = statistics.mean(vals) * 100
        std  = (statistics.stdev(vals) * 100) if n > 1 else 0.0
        sem  = std / math.sqrt(n)
        means.append(round(mean, 1))
        sems.append(round(sem, 1))
        print(f"  {label} depth={dep}: mean={mean:.1f}%  SEM={sem:.1f}%  n={n}")

    return depths, {"mean": means, "std": sems}


# ---------- CHART BUILDER ----------
def make_chart(palette_name='botanical', output_path='probe_accuracy_by_depth.pdf',
               depths=None, series=None, tension=0.0):
    p = PALETTES[palette_name]

    # Use provided data or fall back to module-level constants.
    active_depths = depths if depths is not None else DEPTHS
    active_series = series if series is not None else SERIES

    # ACL/EMNLP two-column figure: ~7 inches wide.
    fig, ax = plt.subplots(figsize=(7.0, 3.5))
    fig.patch.set_facecolor(p['bg'])
    ax.set_facecolor(p['bg'])

    try:
        from scipy.interpolate import make_interp_spline
        def _spline(xs, ys, xd):
            return make_interp_spline(xs, ys, k=3)(xd)
    except ImportError:
        def _spline(xs, ys, xd):
            deg = min(3, len(xs) - 1)
            return np.polyval(np.polyfit(xs, ys, deg), xd)

    def _smooth(xs, ys, xd, tension=0.0):
        """tension=0 → full cubic curve; tension=1 → straight lines."""
        curved = _spline(xs, ys, xd)
        if tension == 0.0:
            return curved
        linear = np.interp(xd, xs, ys)
        return tension * linear + (1.0 - tension) * curved

    # Smooth interpolation: cubic spline (or cubic polynomial) through the data points.
    x_dense = np.linspace(min(active_depths), max(active_depths), 200)

    series_names = list(active_series.keys())
    for i, name in enumerate(series_names):
        means = np.array(active_series[name]['mean'], dtype=float)
        stds  = np.array(active_series[name]['std'],  dtype=float)

        y_dense     = _smooth(active_depths, means, x_dense, tension)
        y_std_dense = _smooth(active_depths, stds,  x_dense, tension)

        color = p['colors'][i]

        # Gradient area fill below the line.
        # matplotlib doesn't do native gradient fills, so we approximate with
        # a many-stop fill_between using decreasing alpha. The visual effect
        # matches a true gradient closely enough for publication.
        _draw_gradient_fill(ax, x_dense, y_dense, color, alpha_top=0.28)

        # Confidence band (light shaded region of ±1 SEM).
        # Comment this out if you don't want CI bands.
        ax.fill_between(x_dense, y_dense - y_std_dense, y_dense + y_std_dense,
                        color=color, alpha=0.08, linewidth=0)

        # The line itself.
        ax.plot(x_dense, y_dense,
                color=color,
                linestyle=p['linestyles'][i],
                linewidth=2.2,
                solid_capstyle='round',
                zorder=3)

        # Markers at actual data points.
        ax.scatter(active_depths, means,
                   marker=p['markers'][i],
                   s=55,
                   facecolor=p['bg'],
                   edgecolor=color,
                   linewidth=2.0,
                   zorder=4)

    # Axes styling.
    ax.set_xlim(min(active_depths) - 1, max(active_depths) + 1)
    ax.set_ylim(0, 100)
    ax.set_xticks(active_depths)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_yticklabels(['0', '25%', '50%', '75%', '100%'])

    # Strip the top/right/left spines for a cleaner look. Keep bottom.
    for spine in ['top', 'right', 'left']:
        ax.spines[spine].set_visible(False)
    ax.spines['bottom'].set_color(p['axis'])
    ax.spines['bottom'].set_linewidth(1.2)

    # Horizontal gridlines only.
    ax.grid(axis='y', linestyle=':', linewidth=0.8, color=p['gridline'])
    ax.set_axisbelow(True)

    # Tick styling.
    ax.tick_params(axis='both', colors=p['text_dim'], labelsize=10, length=0)

    # X-axis label.
    ax.set_xlabel('Depth (number of prior events)',
                  color=p['text'], fontsize=11, labelpad=8)

    # Legend — manually built so we control exactly what it looks like.
    legend_handles = []
    for i, name in enumerate(series_names):
        h = Line2D([0], [0],
                   color=p['colors'][i],
                   linestyle=p['linestyles'][i],
                   linewidth=2.2,
                   marker=p['markers'][i],
                   markersize=7,
                   markerfacecolor=p['bg'],
                   markeredgewidth=2,
                   label=name)
        legend_handles.append(h)

    leg = ax.legend(handles=legend_handles,
                    loc='upper right',
                    frameon=True,
                    framealpha=1.0,
                    edgecolor=p['gridline'],
                    facecolor=p['bg'],
                    fontsize=10,
                    labelcolor=p['text'])
    leg.get_frame().set_linewidth(0.8)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    plt.savefig(output_path,
                facecolor=p['bg'],
                bbox_inches='tight',
                pad_inches=0.1)
    print(f'Saved: {output_path}')


def _draw_gradient_fill(ax, x, y, color, alpha_top=0.28, n_steps=40):
    """
    Approximate a gradient fill from `color`@alpha_top at the top to fully
    transparent at the bottom of the chart area. Drawn as `n_steps` thin
    fill_between bands.
    """
    y_bottom = ax.get_ylim()[0]
    # Convert the visible range into thin horizontal slabs.
    # For each slab, only fill where y > slab_top, clipped to the line.
    for j in range(n_steps):
        # The slab's vertical position relative to the data.
        frac_low  = j / n_steps
        frac_high = (j + 1) / n_steps
        slab_alpha = alpha_top * (1 - frac_low)  # decreases as we go down
        # Fill between the curve and the slab boundary, clipped to the slab.
        y_clip_top    = np.minimum(y, y_bottom + (np.max(y) - y_bottom) * (1 - frac_low))
        y_clip_bottom = np.minimum(y, y_bottom + (np.max(y) - y_bottom) * (1 - frac_high))
        ax.fill_between(x, y_clip_bottom, y_clip_top,
                        color=color, alpha=slab_alpha, linewidth=0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--lnl-probes',      type=Path, default=None, metavar='JSONL',
                        help='POLO probe results JSONL')
    parser.add_argument('--baseline-probes', type=Path, default=None, metavar='JSONL',
                        help='Baseline probe results JSONL')
    parser.add_argument('--baseline-label',  default='OpenClaw (multi-agent)',
                        help='Legend label for the baseline series (default: "OpenClaw (multi-agent)")')
    parser.add_argument('--tension', type=float, default=0.0, metavar='T',
                        help='Curve tension: 0.0 = full cubic (most curved), 1.0 = straight lines (default: 0.0)')
    parser.add_argument('--palette', default='botanical',
                        choices=list(PALETTES.keys()),
                        help='Color palette to use.')
    parser.add_argument('--output', default='probe_accuracy_by_depth.pdf',
                        help='Output PDF path.')
    args = parser.parse_args()

    # Load from files if provided, otherwise fall back to hardcoded constants.
    depths, series = None, None
    if args.lnl_probes or args.baseline_probes:
        series = {}
        all_depths = set()
        if args.lnl_probes:
            d, s = _load_probe_series(args.lnl_probes, 'POLO (ours)')
            series['POLO (ours)'] = s
            all_depths.update(d)
        if args.baseline_probes:
            d, s = _load_probe_series(args.baseline_probes, args.baseline_label)
            series[args.baseline_label] = s
            all_depths.update(d)
        depths = sorted(all_depths)

    make_chart(palette_name=args.palette, output_path=args.output,
               depths=depths, series=series, tension=args.tension)
