#!/usr/bin/env python3
"""Analyze evaluation result files and generate an interactive HTML report with charts.

Usage:
    python -m src.data.analyze_results <results.jsonl> [<results2.jsonl> ...] [options]

Examples:
    python -m src.data.analyze_results outputs/.../runs/test_cases_eval_*.jsonl
    python -m src.data.analyze_results lnl.jsonl baseline.jsonl --label LNL,Baseline
    python -m src.data.analyze_results outputs/.../eval.jsonl --no-html
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import plotly.graph_objects as go
    import plotly.io as pio
    from plotly.subplots import make_subplots

    _PLOTLY = True
except ImportError:
    _PLOTLY = False

from src.data.failure_analysis import _extract_metrics as _extract_interaction_metrics
from src.data.schema import EvalSummary, EventResult, TestCaseResult


# ── Failure category patterns (reused from scripts/categorize-failures.py) ──

CATEGORIES: list[tuple[str, list[str]]] = [
    ("write_service_silent", [
        r"no confirmation that .*(row|record|entry|document).* (was )?(created|appended|stored|written|added)",
        r"no (recorded|new)? ?(row|record|entry|write) (created|appended|stored|written|recorded)",
        r"no .*(airtable|google sheets|zapier tables?|zapier table|database|supabase|notion|google drive).*(record|row|entry)",
        r"not (stored|written|appended|recorded|saved) (in|to) (airtable|google sheets|tables?|drive|database|supabase|notion)",
        r"(airtable|google sheets|tables?|drive|database|supabase|notion).*\b(no (row|record|entry|write|append|upload)|not (recorded|stored|written))\b",
        r"no recorded (write|append|creation)",
        r"write[- ]service .* (empty|did not|no record)",
    ]),
    ("notification_silent", [
        r"no (slack|email|notification|message).*(sent|posted|delivered|triggered)",
        r"(slack|email|notification).*(not (sent|posted|delivered|triggered))",
        r"(was|is) not (posted|sent|delivered|triggered) (to|in)",
        r"no .*(slack dm|direct message|email) (to|was)",
        r"no recorded (email|slack|notification)",
        r"did not (send|post|deliver) (the|a|an)? ?(email|slack|notification|message)",
    ]),
    ("fan_out_incomplete", [
        r"only shows .* (and|,)? ?(no|with no|but no)",
        r"only .* (and no|, no) ",
        r"with no recorded actions? (updating|to|for)",
        r"no evidence (that|of) (the )?(other|remaining|additional|second|third)",
        r"but no (message|record|write|notification|action|entry) (to|for|in|was)",
    ]),
    ("content_incomplete", [
        r"does not (include|contain|show|specify|reflect|match) the (required|specified|requested|exact|full)",
        r"(does|did) not include .* (date|link|field|name|value|amount|id|url|subject|body|detail)",
        r"missing (the )?(required|specified|requested) (date|link|field|name|value|detail|information)",
        r"(subject|body|title|description|content|message) .* (do(es)? not|did not) (fully )?match",
        r"only partially (reflect|include|match)",
        r"does not show the required",
        r"do not match the requested",
    ]),
    ("conditional_not_triggered", [
        r"(escalation|alert|branch|condition) (was )?not (triggered|sent|taken|fired)",
        r"should have (escalated|triggered|alerted|routed|notified)",
        r"required .* (escalation|escalation message) .* (missing|not sent)",
        r"manager (escalation|message) .* (missing|not sent)",
    ]),
    ("wrong_content", [
        r"incorrectly set",
        r"instead of (the )?(required|expected|specified)",
        r"rather than (the )?(required|expected|specified)",
        r"(does|did) not match the (condition|expected|required)",
        r"mismatch",
        r"(wrong|incorrect) (value|name|score|id|subject|field|data|record|format)",
        r"status was updated to .* rather than",
    ]),
    ("service_error", [
        r"service explicitly (said|reported|stated)",
        r"explicitly (said|reported|stated) (it )?(could not|failed|missing)",
        r"explicitly failed",
        r"could not complete",
        r"failed due to",
        r"explicit(ly)? marked .* not (triggered|completed|done)",
    ]),
    ("action_never_taken", [
        r"no (write|update|upload|creation|post|action)",
        r"did not (act|update|create|write|upload|post)",
        r"no (recorded )?action",
        r"took no action",
        r"without (any )?(write|update|notification|action|recorded)",
    ]),
    ("pending_no_completion", [
        r"workflow state is pending",
        r"is pending",
        r"only .* pending",
        r"pending (draft|approval|deduplication|request).* (no|without)",
    ]),
    ("state_mismatch", [
        r"was in fact .* contradict",
        r"contradicts the (requested|expected) ",
        r"stored record .* not the required",
        r"(updated|written) to .* rather than",
    ]),
]

COMPILED: list[tuple[str, list[re.Pattern]]] = [
    (cat, [re.compile(p) for p in pats]) for cat, pats in CATEGORIES
]

MOD_TYPES = ["temporal", "contextual", "exception", "correction", "expansion", "removal"]


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class ResultBundle:
    path: Path
    label: str
    file_format: str  # "lnl" | "baseline" | "unknown"
    run_config: dict
    tc_results: list[TestCaseResult]
    summary: Optional[EvalSummary]


@dataclass
class FlatEvent:
    """EventResult with TC context injected for flat analysis."""
    tc_id: str
    name: str
    domain: str
    run_index: int
    n_events: int  # total events in this TC
    bundle_label: str
    event_id: str
    passed: bool
    reasoning: str
    role: Optional[str]
    input_tokens: int
    output_tokens: int
    judge_input_tokens: int
    judge_output_tokens: int
    latency_ms: float
    observed_chain_depth: int = 0  # max peer-message hop depth extracted from evidence


@dataclass
class AnalysisData:
    bundles: list[ResultBundle]
    flat_events: list[FlatEvent]


# ── Data loading ─────────────────────────────────────────────────────────────

def _detect_format(first: dict) -> str:
    if first.get("record_type") == "run_config":
        return "lnl"
    if first.get("type") == "meta":
        return "baseline"
    return "unknown"


def _extract_label_from_config(run_config: dict, fmt: str) -> str:
    if fmt == "lnl":
        model = run_config.get("model", "")
    else:
        model = run_config.get("params", {}).get("model", "")
    return model or "unknown"


def load_result_file(path: Path, label: Optional[str] = None) -> ResultBundle:
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    if not lines:
        raise ValueError(f"{path}: empty file")

    first = json.loads(lines[0])
    fmt = _detect_format(first)
    if fmt == "unknown":
        print(f"Warning: {path.name}: unrecognized first-line format; treating as LNL", file=sys.stderr)
        fmt = "lnl"

    derived_label = _extract_label_from_config(first, fmt)
    effective_label = label or derived_label

    # Parse body lines — skip first; try last as EvalSummary
    summary: Optional[EvalSummary] = None
    tc_results: list[TestCaseResult] = []

    body_lines = lines[1:]
    # Check if last line is an EvalSummary (has mean_pass_rate)
    if body_lines:
        try:
            last_obj = json.loads(body_lines[-1])
            if "mean_pass_rate" in last_obj:
                summary = EvalSummary.model_validate(last_obj)
                body_lines = body_lines[:-1]
        except Exception:
            pass

    for line in body_lines:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Skip any stray config records
        if d.get("record_type") or d.get("type") == "meta" or "mean_pass_rate" in d:
            continue
        if "tc_id" not in d or "events" not in d:
            continue
        try:
            tc_results.append(TestCaseResult.model_validate(d))
        except Exception as e:
            print(f"Warning: skipping TC record: {e}", file=sys.stderr)

    if not summary and tc_results:
        print(f"Note: {path.name}: no EvalSummary found; role pass rates will be recomputed.", file=sys.stderr)

    return ResultBundle(
        path=path,
        label=effective_label,
        file_format=fmt,
        run_config=first,
        tc_results=tc_results,
        summary=summary,
    )


def _extract_mod_type(tc_id: str) -> str:
    for mt in MOD_TYPES:
        if f"-{mt}-" in tc_id:
            return mt
    return "base"


def build_analysis_data(bundles: list[ResultBundle]) -> AnalysisData:
    flat_events: list[FlatEvent] = []
    for bundle in bundles:
        for tc in bundle.tc_results:
            n_ev = len(tc.events)
            for ev in tc.events:
                metrics = _extract_interaction_metrics(ev.evidence or "")
                flat_events.append(FlatEvent(
                    tc_id=tc.tc_id,
                    name=tc.name,
                    domain=tc.domain,
                    run_index=tc.run_index,
                    n_events=n_ev,
                    bundle_label=bundle.label,
                    event_id=ev.event_id,
                    passed=ev.passed,
                    reasoning=ev.reasoning,
                    role=ev.role,
                    input_tokens=ev.input_tokens,
                    output_tokens=ev.output_tokens,
                    judge_input_tokens=ev.judge_input_tokens,
                    judge_output_tokens=ev.judge_output_tokens,
                    latency_ms=ev.latency_ms,
                    observed_chain_depth=metrics.chain_depth,
                ))
    return AnalysisData(bundles=bundles, flat_events=flat_events)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _tc_pass_rates(bundle: ResultBundle) -> dict[str, list[float]]:
    """tc_id → list of pass_rate across runs."""
    d: dict[str, list[float]] = defaultdict(list)
    for tc in bundle.tc_results:
        if tc.pass_rate is not None:
            d[tc.tc_id].append(tc.pass_rate)
    return d


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _role_pass_rates_from_events(events: list[FlatEvent]) -> dict[str, tuple[float, int]]:
    """role → (mean_pass_rate, n_events). Role None → 'steps'."""
    buckets: dict[str, list[bool]] = defaultdict(list)
    for e in events:
        key = e.role if e.role else "steps"
        buckets[key].append(e.passed)
    return {k: (_mean([float(v) for v in vs]), len(vs)) for k, vs in buckets.items()}


def _categorize_failure(reasoning: str) -> str:
    if not reasoning:
        return "no_reasoning"
    r = reasoning.lower()
    for cat, pats in COMPILED:
        for p in pats:
            if p.search(r):
                return cat
    return "other"


# ── Charts ───────────────────────────────────────────────────────────────────

_COLORS = ["#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A"]


def chart_pass_rate_distribution(data: AnalysisData) -> "go.Figure":
    fig = go.Figure()
    for i, bundle in enumerate(data.bundles):
        rates = [tc.pass_rate for tc in bundle.tc_results if tc.pass_rate is not None and tc.run_index == 0]
        mean_val = _mean(rates)
        fig.add_trace(go.Histogram(
            x=rates,
            name=bundle.label,
            opacity=0.7,
            nbinsx=20,
            marker_color=_COLORS[i % len(_COLORS)],
        ))
        fig.add_vline(x=mean_val, line_dash="dash", line_color=_COLORS[i % len(_COLORS)],
                      annotation_text=f"{bundle.label} mean={mean_val:.2f}",
                      annotation_position="top right")
    fig.update_layout(
        title="Pass Rate Distribution per Test Case",
        xaxis_title="Pass Rate",
        yaxis_title="Number of Test Cases",
        barmode="overlay",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def chart_role_pass_rates(data: AnalysisData) -> "go.Figure":
    role_order = ["steps", "pre_mod", "post_mod", "irrelevant"]
    role_labels = {"steps": "Steps", "pre_mod": "Pre-Mod", "post_mod": "Post-Mod", "irrelevant": "Irrelevant"}

    fig = go.Figure()
    for i, bundle in enumerate(data.bundles):
        x_vals, y_vals, err_vals = [], [], []

        if bundle.summary:
            s = bundle.summary
            role_data = {
                "steps": (s.steps_pass_rate, s.steps_pass_rate_std),
                "pre_mod": (s.pre_mod_pass_rate, s.pre_mod_pass_rate_std),
                "post_mod": (s.post_mod_pass_rate, s.post_mod_pass_rate_std),
                "irrelevant": (s.irrelevant_pass_rate, s.irrelevant_pass_rate_std),
            }
        else:
            ev = [e for e in data.flat_events if e.bundle_label == bundle.label]
            computed = _role_pass_rates_from_events(ev)
            role_data = {k: (v[0], None) for k, v in computed.items()}

        for role in role_order:
            pr, std = role_data.get(role, (None, None))
            if pr is not None:
                x_vals.append(role_labels.get(role, role))
                y_vals.append(pr)
                err_vals.append(std if std is not None else 0)

        fig.add_trace(go.Bar(
            name=bundle.label,
            x=x_vals,
            y=y_vals,
            error_y=dict(type="data", array=err_vals, visible=True) if any(e > 0 for e in err_vals) else None,
            marker_color=_COLORS[i % len(_COLORS)],
        ))

    fig.update_layout(
        title="Pass Rate by Event Role",
        xaxis_title="Role",
        yaxis_title="Pass Rate",
        yaxis=dict(range=[0, 1.05]),
        barmode="group",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def chart_mod_type_pass_rates(data: AnalysisData) -> "go.Figure":
    fig = go.Figure()
    for i, bundle in enumerate(data.bundles):
        buckets: dict[str, list[float]] = defaultdict(list)
        seen: set[str] = set()
        for tc in bundle.tc_results:
            if tc.run_index != 0:
                continue
            mt = _extract_mod_type(tc.tc_id)
            key = (tc.tc_id, mt)
            if key in seen:
                continue
            seen.add(key)
            if tc.pass_rate is not None:
                buckets[mt].append(tc.pass_rate)

        x_vals = sorted(buckets.keys())
        y_vals = [_mean(buckets[k]) for k in x_vals]
        counts = [len(buckets[k]) for k in x_vals]

        fig.add_trace(go.Bar(
            name=bundle.label,
            x=x_vals,
            y=y_vals,
            text=[f"n={c}" for c in counts],
            textposition="outside",
            marker_color=_COLORS[i % len(_COLORS)],
        ))

    fig.update_layout(
        title="Pass Rate by Modification Type",
        xaxis_title="Modification Type",
        yaxis_title="Mean Pass Rate",
        yaxis=dict(range=[0, 1.15]),
        barmode="group",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def chart_per_tc_pass_rates(data: AnalysisData, top_n: int = 50) -> "go.Figure":
    """Sorted bar chart showing per-TC pass rate (worst to best, first run only)."""
    # Collect pass rates per TC per bundle
    all_tcs: dict[str, dict[str, float]] = {}  # tc_id → {label → pass_rate}
    tc_meta: dict[str, dict] = {}  # tc_id → {name, domain, mod_type}

    for bundle in data.bundles:
        for tc in bundle.tc_results:
            if tc.run_index != 0:
                continue
            if tc.pass_rate is None:
                continue
            if tc.tc_id not in all_tcs:
                all_tcs[tc.tc_id] = {}
                tc_meta[tc.tc_id] = {
                    "name": tc.name,
                    "domain": tc.domain,
                    "mod_type": _extract_mod_type(tc.tc_id),
                }
            all_tcs[tc.tc_id][bundle.label] = tc.pass_rate

    if len(data.bundles) == 1:
        label = data.bundles[0].label
        sorted_tcs = sorted(all_tcs.keys(), key=lambda tid: all_tcs[tid].get(label, 1.0))
        display_tcs = sorted_tcs[:top_n]
        mean_val = _mean([all_tcs[tid].get(label, 0.0) for tid in sorted_tcs])

        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=display_tcs,
            y=[all_tcs[tid].get(label, 0.0) for tid in display_tcs],
            customdata=[[tc_meta[tid]["name"], tc_meta[tid]["domain"], tc_meta[tid]["mod_type"]] for tid in display_tcs],
            hovertemplate="<b>%{x}</b><br>Pass Rate: %{y:.2f}<br>Name: %{customdata[0]}<br>Domain: %{customdata[1]}<br>Mod type: %{customdata[2]}<extra></extra>",
            marker_color=_COLORS[0],
            name=label,
        ))
        fig.add_hline(y=mean_val, line_dash="dash", line_color="gray",
                      annotation_text=f"mean={mean_val:.2f}", annotation_position="top right")
        title_suffix = f" (worst {top_n})" if len(sorted_tcs) > top_n else ""
    else:
        # Multi-file: sort by first bundle
        first_label = data.bundles[0].label
        sorted_tcs = sorted(all_tcs.keys(), key=lambda tid: all_tcs[tid].get(first_label, 1.0))
        display_tcs = sorted_tcs[:top_n]
        fig = go.Figure()
        for i, bundle in enumerate(data.bundles):
            fig.add_trace(go.Bar(
                name=bundle.label,
                x=display_tcs,
                y=[all_tcs[tid].get(bundle.label, None) for tid in display_tcs],
                marker_color=_COLORS[i % len(_COLORS)],
            ))
        title_suffix = f" (worst {top_n} by {first_label})" if len(sorted_tcs) > top_n else ""

    fig.update_layout(
        title=f"Per-TC Pass Rate (sorted worst→best){title_suffix}",
        xaxis_title="Test Case ID",
        yaxis_title="Pass Rate",
        yaxis=dict(range=[0, 1.05]),
        xaxis=dict(tickangle=45, rangeslider=dict(visible=len(display_tcs) > 20)),
        height=500,
        barmode="group",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def chart_run_consistency(data: AnalysisData) -> Optional["go.Figure"]:
    """Std dev per TC across runs. Returns None if no bundle has runs > 1."""
    has_multi_run = any(
        any(tc.run_index > 0 for tc in b.tc_results)
        for b in data.bundles
    )
    if not has_multi_run:
        return None

    fig = go.Figure()
    for i, bundle in enumerate(data.bundles):
        tc_runs: dict[str, list[float]] = defaultdict(list)
        for tc in bundle.tc_results:
            if tc.pass_rate is not None:
                tc_runs[tc.tc_id].append(tc.pass_rate)

        # Compute std dev (simplified: mean abs deviation from mean, or use variance)
        stds: list[tuple[str, float]] = []
        for tc_id, rates in tc_runs.items():
            if len(rates) < 2:
                continue
            mean_r = _mean(rates)
            std_r = (_mean([(r - mean_r) ** 2 for r in rates])) ** 0.5
            stds.append((tc_id, std_r))

        stds.sort(key=lambda x: -x[1])
        tc_ids = [s[0] for s in stds]
        std_vals = [s[1] for s in stds]

        fig.add_trace(go.Bar(
            name=bundle.label,
            x=tc_ids,
            y=std_vals,
            marker_color=_COLORS[i % len(_COLORS)],
        ))

    fig.update_layout(
        title="Behavioral Consistency — Pass Rate Std Dev per TC (most inconsistent first)",
        xaxis_title="Test Case ID",
        yaxis_title="Pass Rate Std Dev",
        xaxis=dict(tickangle=45, rangeslider=dict(visible=True)),
        height=450,
        barmode="group",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def chart_token_usage(data: AnalysisData, top_n: int = 30) -> "go.Figure":
    """Stacked bar: event in/out + judge in/out tokens per TC, top-N most expensive."""
    fig = go.Figure()

    for i, bundle in enumerate(data.bundles):
        tc_tokens: dict[str, dict[str, int]] = defaultdict(lambda: dict(ev_in=0, ev_out=0, j_in=0, j_out=0))
        for e in data.flat_events:
            if e.bundle_label != bundle.label or e.run_index != 0:
                continue
            t = tc_tokens[e.tc_id]
            t["ev_in"] += e.input_tokens
            t["ev_out"] += e.output_tokens
            t["j_in"] += e.judge_input_tokens
            t["j_out"] += e.judge_output_tokens

        sorted_tcs = sorted(tc_tokens.keys(), key=lambda tid: -sum(tc_tokens[tid].values()))[:top_n]

        stacks = [
            ("Event Input", "ev_in", "#636EFA"),
            ("Event Output", "ev_out", "#EF553B"),
            ("Judge Input", "j_in", "#00CC96"),
            ("Judge Output", "j_out", "#AB63FA"),
        ]
        for stack_name, key, color in stacks:
            label_name = f"{bundle.label} — {stack_name}" if len(data.bundles) > 1 else stack_name
            fig.add_trace(go.Bar(
                name=label_name,
                x=sorted_tcs,
                y=[tc_tokens[tid][key] for tid in sorted_tcs],
                marker_color=color,
                legendgroup=stack_name,
                showlegend=(i == 0),
            ))

    fig.update_layout(
        title=f"Token Usage by Test Case — Top {top_n} Most Expensive (run 0)",
        xaxis_title="Test Case ID",
        yaxis_title="Tokens",
        xaxis=dict(tickangle=45),
        barmode="stack",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=500,
    )
    return fig


def chart_latency_distribution(data: AnalysisData) -> "go.Figure":
    fig = go.Figure()
    for i, bundle in enumerate(data.bundles):
        latencies = [
            e.latency_ms for e in data.flat_events
            if e.bundle_label == bundle.label and e.run_index == 0
        ]
        fig.add_trace(go.Box(
            y=latencies,
            name=bundle.label,
            marker_color=_COLORS[i % len(_COLORS)],
            boxmean="sd",
        ))
    fig.update_layout(
        title="Event Latency Distribution",
        yaxis_title="Latency (ms)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def chart_chain_depth_vs_pass_rate(data: AnalysisData) -> "go.Figure":
    """Bar chart: mean pass rate binned by observed max chain depth per TC.

    Observed chain depth = max peer-message hop depth (from evidence) across all
    events in a TC. Uses first run only.
    """
    fig = go.Figure()

    def _bin(n: int) -> str:
        if n <= 0:
            return "0"
        if n == 1:
            return "1"
        if n == 2:
            return "2"
        if n == 3:
            return "3"
        return "4+"

    bin_order = ["0", "1", "2", "3", "4+"]

    for i, bundle in enumerate(data.bundles):
        # Compute max observed chain depth per TC (first run only)
        tc_max_depth: dict[str, int] = defaultdict(int)
        tc_pass: dict[str, float] = {}
        for e in data.flat_events:
            if e.bundle_label != bundle.label or e.run_index != 0:
                continue
            if e.observed_chain_depth > tc_max_depth[e.tc_id]:
                tc_max_depth[e.tc_id] = e.observed_chain_depth
        for tc in bundle.tc_results:
            if tc.run_index == 0 and tc.pass_rate is not None:
                tc_pass[tc.tc_id] = tc.pass_rate

        bins: dict[str, list[float]] = defaultdict(list)
        for tc_id, depth in tc_max_depth.items():
            if tc_id in tc_pass:
                bins[_bin(depth)].append(tc_pass[tc_id])

        y_vals = [_mean(bins.get(b, [])) for b in bin_order]
        counts = [len(bins.get(b, [])) for b in bin_order]

        fig.add_trace(go.Bar(
            name=bundle.label,
            x=bin_order,
            y=y_vals,
            text=[f"n={c}" for c in counts],
            textposition="outside",
            marker_color=_COLORS[i % len(_COLORS)],
        ))

    fig.update_layout(
        title="Pass Rate by Observed Max Chain Depth per TC",
        xaxis_title="Max observed peer-message hops (chain depth) per TC",
        yaxis_title="Mean Pass Rate",
        yaxis=dict(range=[0, 1.15]),
        barmode="group",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def chart_comparison(data: AnalysisData) -> Optional["go.Figure"]:
    """Head-to-head comparison of key summary metrics. Only for multi-file mode."""
    if len(data.bundles) < 2:
        return None

    metrics = [
        ("mean_pass_rate", "Overall Pass Rate"),
        ("steps_pass_rate", "Steps Pass Rate"),
        ("mod_pass_rate", "Mod Pass Rate"),
        ("post_mod_pass_rate", "Post-Mod Pass Rate"),
        ("pre_mod_pass_rate", "Pre-Mod Pass Rate"),
    ]

    fig = go.Figure()
    for i, bundle in enumerate(data.bundles):
        if not bundle.summary:
            continue
        s = bundle.summary
        x_vals, y_vals = [], []
        for attr, label in metrics:
            val = getattr(s, attr, None)
            if val is not None:
                x_vals.append(label)
                y_vals.append(val)
        fig.add_trace(go.Bar(
            name=bundle.label,
            x=x_vals,
            y=y_vals,
            marker_color=_COLORS[i % len(_COLORS)],
        ))

    fig.update_layout(
        title="Head-to-Head Comparison: Key Metrics",
        xaxis_title="Metric",
        yaxis_title="Pass Rate",
        yaxis=dict(range=[0, 1.05]),
        barmode="group",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


# ── Insights ─────────────────────────────────────────────────────────────────

def _format_rate(val: Optional[float]) -> str:
    if val is None:
        return "N/A"
    return f"{val:.1%}"


def compute_and_print_insights(data: AnalysisData, top_n: int) -> None:
    sep = "─" * 60

    print(sep)
    print("EVALUATION ANALYSIS INSIGHTS")
    print(sep)

    for bundle in data.bundles:
        print(f"\n◆ {bundle.label}  ({bundle.path.name})")

        tc_count = len(set(tc.tc_id for tc in bundle.tc_results))
        runs_per_tc = max((tc.run_index for tc in bundle.tc_results), default=0) + 1

        if bundle.summary:
            s = bundle.summary
            consistency = "consistent" if (s.pass_rate_std or 1) < 0.05 else \
                          "variable" if (s.pass_rate_std or 0) > 0.15 else "moderate"
            print(f"  Overall pass rate:  {_format_rate(s.mean_pass_rate)}"
                  f"  (std={_format_rate(s.pass_rate_std)}, {consistency})")
            print(f"  Test cases: {s.total_test_cases}  |  Runs/TC: {runs_per_tc}  |  Events: {s.total_events}")
            print(f"  Inconclusive TCs:   {s.inconclusive_tcs} ({s.inconclusive_tcs/max(s.total_test_cases,1):.1%})")
        else:
            ev = [e for e in data.flat_events if e.bundle_label == bundle.label]
            n_pass = sum(1 for e in ev if e.passed)
            rate = n_pass / len(ev) if ev else 0.0
            print(f"  Overall pass rate:  {rate:.1%}  (recomputed — no EvalSummary)")
            print(f"  Test cases: {tc_count}  |  Runs/TC: {runs_per_tc}  |  Events: {len(ev)}")

    # ── Role comparison ──
    print(f"\n{sep}")
    print("PASS RATE BY ROLE")
    print(sep)
    for bundle in data.bundles:
        print(f"\n  {bundle.label}:")
        if bundle.summary:
            s = bundle.summary
            roles = [
                ("Steps",      s.steps_pass_rate,      s.steps_pass_rate_std),
                ("Pre-Mod",    s.pre_mod_pass_rate,     s.pre_mod_pass_rate_std),
                ("Post-Mod",   s.post_mod_pass_rate,    s.post_mod_pass_rate_std),
                ("Irrelevant", s.irrelevant_pass_rate,  s.irrelevant_pass_rate_std),
            ]
            for rname, rate, std in roles:
                if rate is not None:
                    std_str = f" ± {std:.1%}" if std else ""
                    print(f"    {rname:<12} {_format_rate(rate)}{std_str}")
            # Flag biggest drop
            if s.pre_mod_pass_rate and s.post_mod_pass_rate:
                delta = s.pre_mod_pass_rate - s.post_mod_pass_rate
                if delta > 0.1:
                    print(f"    ⚠  Post-Mod is {delta:.1%} lower than Pre-Mod — modification adaptation is the primary failure mode")
            if s.steps_pass_rate and s.mean_pass_rate:
                if s.steps_pass_rate < 0.5:
                    print(f"    ⚠  Steps pass rate is low ({s.steps_pass_rate:.1%}) — baseline capability is the bottleneck")
        else:
            ev = [e for e in data.flat_events if e.bundle_label == bundle.label]
            role_rates = _role_pass_rates_from_events(ev)
            for role, (rate, count) in sorted(role_rates.items()):
                print(f"    {role:<12} {rate:.1%}  (n={count})")

    # ── Chain depth vs pass rate ──
    print(f"\n{sep}")
    print("OBSERVED MAX CHAIN DEPTH (HOPS) vs PASS RATE")
    print(sep)
    print("  (Observed chain depth = max peer-message hop depth across all events in a TC)")

    def _bin_depth(n: int) -> str:
        if n <= 0: return "0"
        if n == 1: return "1"
        if n == 2: return "2"
        if n == 3: return "3"
        return "4+"

    for bundle in data.bundles:
        print(f"\n  {bundle.label}:")
        tc_max_depth: dict[str, int] = defaultdict(int)
        tc_pass_r: dict[str, float] = {}
        for e in data.flat_events:
            if e.bundle_label != bundle.label or e.run_index != 0:
                continue
            if e.observed_chain_depth > tc_max_depth[e.tc_id]:
                tc_max_depth[e.tc_id] = e.observed_chain_depth
        for tc in bundle.tc_results:
            if tc.run_index == 0 and tc.pass_rate is not None:
                tc_pass_r[tc.tc_id] = tc.pass_rate

        bins: dict[str, list[float]] = defaultdict(list)
        for tc_id, depth in tc_max_depth.items():
            if tc_id in tc_pass_r:
                bins[_bin_depth(depth)].append(tc_pass_r[tc_id])

        for b in ["0", "1", "2", "3", "4+"]:
            vals = bins.get(b, [])
            if vals:
                print(f"    depth={b}:  {_mean(vals):.1%}  (n={len(vals)})")

        # Flag degradation
        low_rate = _mean(bins.get("0", []) + bins.get("1", []))
        high_rate = _mean(bins.get("3", []) + bins.get("4+", []))
        if low_rate and high_rate and (low_rate - high_rate) > 0.1:
            print(f"    ⚠  Deeper chains (3+ hops) have {low_rate - high_rate:.1%} lower pass rate — complexity degrades performance")

    # ── Top failing TCs ──
    print(f"\n{sep}")
    print(f"TOP {top_n} FAILING TEST CASES")
    print(sep)
    for bundle in data.bundles:
        print(f"\n  {bundle.label}:")
        tc_rates: dict[str, list[float]] = defaultdict(list)
        tc_names: dict[str, str] = {}
        for tc in bundle.tc_results:
            if tc.pass_rate is not None:
                tc_rates[tc.tc_id].append(tc.pass_rate)
                tc_names[tc.tc_id] = tc.name
        sorted_tcs = sorted(tc_rates.keys(), key=lambda tid: _mean(tc_rates[tid]))
        for tc_id in sorted_tcs[:top_n]:
            rate = _mean(tc_rates[tc_id])
            mt = _extract_mod_type(tc_id)
            name = tc_names.get(tc_id, "")[:60]
            print(f"    {rate:.1%}  [{mt}]  {tc_id}  ({name})")

    # ── Most expensive TCs by token count ──
    print(f"\n{sep}")
    print(f"TOP {top_n} MOST EXPENSIVE TEST CASES (by total tokens, run 0)")
    print(sep)
    for bundle in data.bundles:
        print(f"\n  {bundle.label}:")
        tc_tok: dict[str, int] = defaultdict(int)
        for e in data.flat_events:
            if e.bundle_label != bundle.label or e.run_index != 0:
                continue
            tc_tok[e.tc_id] += e.input_tokens + e.output_tokens + e.judge_input_tokens + e.judge_output_tokens
        sorted_toks = sorted(tc_tok.keys(), key=lambda tid: -tc_tok[tid])[:top_n]
        for tc_id in sorted_toks:
            print(f"    {tc_tok[tc_id]:>8,} tokens  {tc_id}")

    # ── Slowest events ──
    print(f"\n{sep}")
    print(f"TOP {top_n} SLOWEST EVENTS (run 0)")
    print(sep)
    for bundle in data.bundles:
        print(f"\n  {bundle.label}:")
        ev0 = [e for e in data.flat_events if e.bundle_label == bundle.label and e.run_index == 0]
        slowest = sorted(ev0, key=lambda e: -e.latency_ms)[:top_n]
        for e in slowest:
            status = "✓" if e.passed else "✗"
            role_tag = f"[{e.role}]" if e.role else "[step]"
            print(f"    {e.latency_ms/1000:>6.1f}s  {status}  {role_tag:<12}  {e.tc_id}/{e.event_id}")

    # ── Failure pattern summary ──
    print(f"\n{sep}")
    print("FAILURE PATTERN SUMMARY")
    print(sep)
    for bundle in data.bundles:
        print(f"\n  {bundle.label}:")
        failed_ev = [e for e in data.flat_events if e.bundle_label == bundle.label and not e.passed]
        if not failed_ev:
            print("    No failed events.")
            continue
        buckets: dict[str, int] = defaultdict(int)
        for e in failed_ev:
            buckets[_categorize_failure(e.reasoning)] += 1
        sorted_cats = sorted(buckets.items(), key=lambda x: -x[1])
        total_failed = len(failed_ev)
        for cat, count in sorted_cats[:8]:
            pct = count / total_failed * 100
            print(f"    {cat:<28} {count:>5}  ({pct:.1f}%)")

    # ── Multi-file comparison ──
    if len(data.bundles) >= 2:
        print(f"\n{sep}")
        print("COMPARISON")
        print(sep)
        b0, b1 = data.bundles[0], data.bundles[1]
        if b0.summary and b1.summary:
            s0, s1 = b0.summary, b1.summary
            metrics = [
                ("Overall pass rate",   s0.mean_pass_rate,     s1.mean_pass_rate),
                ("Post-Mod pass rate",  s0.post_mod_pass_rate, s1.post_mod_pass_rate),
                ("Inconclusive TCs",    s0.inconclusive_tcs / max(s0.total_test_cases, 1),
                                        s1.inconclusive_tcs / max(s1.total_test_cases, 1)),
            ]
            # Total tokens
            tok0 = sum(e.input_tokens + e.output_tokens + e.judge_input_tokens + e.judge_output_tokens
                       for e in data.flat_events if e.bundle_label == b0.label)
            tok1 = sum(e.input_tokens + e.output_tokens + e.judge_input_tokens + e.judge_output_tokens
                       for e in data.flat_events if e.bundle_label == b1.label)
            print()
            print(f"  {'Metric':<25}  {b0.label:<20}  {b1.label:<20}  Delta")
            print("  " + "-" * 70)
            for mname, v0, v1 in metrics:
                if v0 is None or v1 is None:
                    continue
                delta = v1 - v0
                sign = "+" if delta >= 0 else ""
                print(f"  {mname:<25}  {v0:<20.1%}  {v1:<20.1%}  {sign}{delta:.1%}")
            if tok0 > 0 and tok1 > 0:
                ratio = tok1 / tok0
                print(f"  {'Token cost ratio':<25}  {'1.0x':<20}  {ratio:<20.2f}x  ({b1.label} uses {ratio:.2f}x tokens)")

    print(f"\n{sep}\n")


# ── HTML report ───────────────────────────────────────────────────────────────

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Evaluation Analysis — {title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f1117; color: #e0e0e0; margin: 0; padding: 20px; }}
  h1   {{ font-size: 1.4rem; color: #fff; margin-bottom: 4px; }}
  .meta {{ font-size: 0.82rem; color: #888; margin-bottom: 24px; }}
  .chart-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(700px, 1fr));
                 gap: 20px; }}
  .chart-card {{ background: #1a1d27; border-radius: 10px; padding: 12px;
                 box-shadow: 0 2px 8px rgba(0,0,0,0.4); overflow: hidden; }}
  .chart-card.full {{ grid-column: 1 / -1; }}
  pre.insights {{ background: #1a1d27; border-radius: 10px; padding: 20px;
                  font-size: 0.82rem; color: #ccc; white-space: pre-wrap;
                  line-height: 1.6; margin-bottom: 20px; overflow-x: auto; }}
</style>
</head>
<body>
<h1>Evaluation Analysis</h1>
<p class="meta">{meta}</p>
<pre class="insights">{insights}</pre>
<div class="chart-grid">
{charts}
</div>
</body>
</html>
"""


def build_html_report(charts: list[tuple[str, "go.Figure", bool]], insights: str,
                      bundles: list[ResultBundle]) -> str:
    meta_parts = [f"{b.label} ({b.path.name})" for b in bundles]
    meta = "Generated " + datetime.now().strftime("%Y-%m-%d %H:%M") + " | " + " | ".join(meta_parts)

    import io, contextlib
    insights_captured = io.StringIO()
    # insights is already a string here — just embed it
    chart_htmls = []
    for title, fig, full_width in charts:
        html = pio.to_html(fig, include_plotlyjs="cdn" if not chart_htmls else False,
                           full_html=False, config={"responsive": True})
        css_class = "chart-card full" if full_width else "chart-card"
        chart_htmls.append(f'<div class="{css_class}">{html}</div>')

    title_str = " vs ".join(b.label for b in bundles)
    return _HTML_TEMPLATE.format(
        title=title_str,
        meta=meta,
        insights=insights,
        charts="\n".join(chart_htmls),
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("inputs", nargs="+", type=Path, metavar="RESULTS_JSONL",
                   help="One or more evaluation result JSONL files")
    p.add_argument("--output", "-o", type=Path, default=None,
                   help="Output HTML file path (default: <input_stem>_analysis_<ts>.html)")
    p.add_argument("--top-n", type=int, default=10, dest="top_n",
                   help="Number of items in ranked lists (default: 10)")
    p.add_argument("--label", type=str, default=None,
                   help="Comma-separated display labels for each input file")
    p.add_argument("--no-html", action="store_true",
                   help="Skip HTML report generation; only print insights")
    p.add_argument("--png-dir", type=Path, default=None,
                   help="Also export each chart as PNG (requires kaleido)")
    return p


def _default_output_path(primary_input: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = primary_input.stem.replace("_eval_", "_").replace("_baseline_", "_")
    return primary_input.parent / f"{stem}_analysis_{ts}.html"


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.no_html and not _PLOTLY:
        print("Error: plotly is not installed. Run: uv pip install plotly", file=sys.stderr)
        print("Or use --no-html to skip chart generation.", file=sys.stderr)
        sys.exit(1)

    labels = [l.strip() for l in args.label.split(",")] if args.label else [None] * len(args.inputs)
    if len(labels) < len(args.inputs):
        labels += [None] * (len(args.inputs) - len(labels))

    bundles: list[ResultBundle] = []
    for path, label in zip(args.inputs, labels):
        print(f"Loading {path} ...", file=sys.stderr)
        bundle = load_result_file(path, label)
        print(f"  → {len(bundle.tc_results)} TC results | format={bundle.file_format} | label={bundle.label}",
              file=sys.stderr)
        bundles.append(bundle)

    data = build_analysis_data(bundles)

    # Capture insights as string for embedding in HTML
    import io, contextlib
    insights_buf = io.StringIO()
    with contextlib.redirect_stdout(insights_buf):
        compute_and_print_insights(data, args.top_n)
    insights_text = insights_buf.getvalue()
    print(insights_text, end="")

    if args.no_html:
        return

    output_path = args.output or _default_output_path(args.inputs[0])
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Build charts
    charts: list[tuple[str, "go.Figure", bool]] = []

    def _add(title: str, fig: Optional["go.Figure"], full: bool = False) -> None:
        if fig is not None:
            charts.append((title, fig, full))

    _add("Pass Rate Distribution", chart_pass_rate_distribution(data))
    _add("Role Pass Rates", chart_role_pass_rates(data))
    _add("Mod Type Pass Rates", chart_mod_type_pass_rates(data))
    _add("Per-TC Pass Rate", chart_per_tc_pass_rates(data, args.top_n * 5), full=True)
    _add("Run Consistency", chart_run_consistency(data), full=True)
    _add("Token Usage", chart_token_usage(data, args.top_n * 3), full=True)
    _add("Latency Distribution", chart_latency_distribution(data))
    _add("Chain Depth vs Pass Rate", chart_chain_depth_vs_pass_rate(data))
    _add("Comparison", chart_comparison(data))

    if args.png_dir:
        try:
            import kaleido  # noqa: F401
            args.png_dir.mkdir(parents=True, exist_ok=True)
            for title, fig, _ in charts:
                fname = re.sub(r"[^\w]+", "_", title.lower()).strip("_") + ".png"
                fig.write_image(args.png_dir / fname)
            print(f"PNGs: {args.png_dir}", file=sys.stderr)
        except ImportError:
            print("Warning: kaleido not installed; skipping PNG export.", file=sys.stderr)

    html = build_html_report(charts, insights_text, bundles)
    output_path.write_text(html)
    print(f"\nReport: {output_path}")


if __name__ == "__main__":
    main()
