"""Pair two evaluate.py JSONL outputs and surface per-TC pass/fail diffs.

Outputs:
  1. Per-TC matrix (TSV stdout): tc_id | left_pass | right_pass | delta
  2. regression_ids.txt — TCs where left passed AND right failed
       (written next to the right JSONL, alongside right_runs/)
  3. Per-failing-TC diagnostics block printed to stdout, sourced from
     existing EventResult fields populated by --verbose DEBUG:
       evidence, reasoning, outgoing_messages, planner_plans,
       executor_calls, executor_retries, trace.

Usage:
  python scripts/diff_eval_runs.py left.jsonl right.jsonl \
      --label-left sync --label-right async [--max-diagnostic-tcs 5]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_results(path: Path) -> dict[str, dict]:
    """Return tc_id → sample_result record."""
    by_tc: dict[str, dict] = {}
    for line in path.open():
        rec = json.loads(line)
        if "tc_id" in rec:
            by_tc[rec["tc_id"]] = rec
    return by_tc


def _tc_pass(rec: dict) -> bool | None:
    """True/False/None. None = inconclusive (no events ran, infra error)."""
    if rec.get("error_type"):
        return None
    events = rec.get("events") or []
    if not events:
        return None
    real = [e for e in events if not e.get("infra_error")]
    if not real:
        return None
    return all(e.get("passed") for e in real)


def _truncate(s: str | None, n: int = 600) -> str:
    if not s:
        return ""
    s = str(s)
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."


def _render_diagnostic(rec: dict, label: str) -> str:
    lines = [f"\n── {label}: {rec['tc_id']} ──"]
    for ev in rec.get("events") or []:
        passed = ev.get("passed")
        infra = ev.get("infra_error")
        mark = "✓" if passed else ("⚠" if infra else "✗")
        lines.append(f"  {mark} {ev.get('event_id')}  passed={passed} infra={infra}")
        if ev.get("expected"):
            lines.append(f"     expected:  {_truncate(ev['expected'], 240)}")
        if ev.get("reasoning"):
            lines.append(f"     reasoning: {_truncate(ev['reasoning'], 500)}")
        ec = ev.get("executor_calls")
        er = ev.get("executor_retries")
        if ec is not None:
            lines.append(f"     executor_calls={ec} retries={er}")
        # plans (per-turn). Compact: id, kind, target, status, result_summary.
        plans = ev.get("planner_plans") or []
        if plans:
            lines.append(f"     planner_plans ({len(plans)} turn(s)):")
            for i, plan in enumerate(plans):
                goal = _truncate(plan.get("goal", ""), 120)
                lines.append(f"       turn{i} goal: {goal}")
                for step in (plan.get("steps") or [])[:8]:
                    sid = step.get("id") or f"s{step.get('step_number','?')}"
                    line = (
                        f"         {sid:5} {step.get('kind'):6} → {step.get('target') or '-':24} "
                        f"status={step.get('status','?')}"
                    )
                    if step.get("result_summary"):
                        line += f"  result={_truncate(step['result_summary'], 80)}"
                    lines.append(line)
        # bus log (compact).
        bus = ev.get("outgoing_messages") or []
        if bus:
            lines.append(f"     outgoing_messages ({len(bus)} msgs):")
            for m in bus[:24]:
                t = m.get("type", "?")
                src = m.get("sender", "?")
                dst = m.get("recipient", "?")
                status = m.get("status") or ""
                content = _truncate(m.get("content", ""), 100)
                tag = f" [{status}]" if status else ""
                lines.append(f"       {t:7} {src:24} → {dst:24}{tag}: {content}")
            if len(bus) > 24:
                lines.append(f"       ... ({len(bus)-24} more)")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("left", type=Path)
    p.add_argument("right", type=Path)
    p.add_argument("--label-left", default="LEFT")
    p.add_argument("--label-right", default="RIGHT")
    p.add_argument("--max-diagnostic-tcs", type=int, default=5)
    p.add_argument(
        "--regression-out",
        type=Path,
        default=None,
        help="path to write regression TC ids (default: alongside right JSONL)",
    )
    args = p.parse_args()

    left = _load_results(args.left)
    right = _load_results(args.right)
    if not left or not right:
        print(
            f"empty result set — left={len(left)} right={len(right)}",
            file=sys.stderr,
        )
        return 1

    common = sorted(set(left) & set(right))
    only_left = sorted(set(left) - set(right))
    only_right = sorted(set(right) - set(left))

    rows = []
    n_agree_pass = n_agree_fail = n_left_only_pass = n_right_only_pass = 0
    inconclusive_l = inconclusive_r = 0
    regression_ids: list[str] = []
    collateral_ids: list[str] = []

    for tc in common:
        lp = _tc_pass(left[tc])
        rp = _tc_pass(right[tc])
        if lp is None:
            inconclusive_l += 1
        if rp is None:
            inconclusive_r += 1
        if lp and rp:
            n_agree_pass += 1
            delta = "="
        elif lp is False and rp is False:
            n_agree_fail += 1
            delta = "="
        elif lp and not rp:
            n_left_only_pass += 1
            delta = "REGRESS"
            regression_ids.append(tc)
        elif rp and not lp:
            n_right_only_pass += 1
            delta = "GAIN"
            collateral_ids.append(tc)
        else:
            delta = "?"
        rows.append((tc, lp, rp, delta))

    # Per-TC matrix
    print(f"tc_id\t{args.label_left}_pass\t{args.label_right}_pass\tdelta")
    for tc, lp, rp, d in rows:
        print(f"{tc}\t{lp}\t{rp}\t{d}")

    # Aggregate
    print()
    print("== Aggregate ==")
    print(f"common TCs:          {len(common)}")
    print(f"left only:           {len(only_left)}   {only_left[:5]}")
    print(f"right only:          {len(only_right)}  {only_right[:5]}")
    print(f"both pass:           {n_agree_pass}")
    print(f"both fail:           {n_agree_fail}")
    print(f"{args.label_left} pass / {args.label_right} fail (REGRESS): {n_left_only_pass}")
    print(f"{args.label_right} pass / {args.label_left} fail (GAIN):    {n_right_only_pass}")
    print(f"inconclusive {args.label_left}: {inconclusive_l}  {args.label_right}: {inconclusive_r}")

    # Regression / collateral files
    out_dir = (args.regression_out or args.right).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    reg_path = args.regression_out or (out_dir / "regression_ids.txt")
    reg_path.write_text("\n".join(regression_ids) + ("\n" if regression_ids else ""))
    print(f"\nregression ids → {reg_path}  (n={len(regression_ids)})")

    col_path = out_dir / "collateral_ids.txt"
    col_path.write_text("\n".join(collateral_ids) + ("\n" if collateral_ids else ""))
    print(f"collateral ids → {col_path}  (n={len(collateral_ids)})")

    # Diagnostics for the top regression TCs (largest event sets first)
    print("\n== Diagnostics for regression TCs ==")
    ranked = sorted(
        regression_ids,
        key=lambda t: -(len(right[t].get("events") or [])),
    )[: args.max_diagnostic_tcs]
    for tc in ranked:
        print(_render_diagnostic(right[tc], args.label_right))
    if regression_ids and not ranked:
        print("(no diagnostic surfaces — all regression TCs lack events)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
