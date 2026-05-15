"""Render a transaction trace from an evaluate.py results JSONL.

Reads the structured `trace` field on EventResult records (populated by
`build_event_trace` in src/data/evaluate.py) and prints two views:

  1. Indented ASCII cascade tree — each hop shows sender → recipient,
     wall-clock offset from the root, processing duration, and LLM latency.
  2. Per-sender duration breakdown — count of hops, total processing time,
     total LLM time, mean processing time.

Usage:
    python scripts/plot_event_trace.py --results outputs/eval/<run>.jsonl
    python scripts/plot_event_trace.py --results <file> --tc-id <id> --event-id <id>

Defaults: picks the first event in the file whose trace has >1 hop.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _iter_event_results(rows: list[dict]):
    """Yield (tc_id, run_index, event_dict) for every event in every TC result."""
    for row in rows:
        if "events" not in row or "tc_id" not in row:
            continue
        tc_id = row.get("tc_id", "")
        run_idx = row.get("run_index", 0)
        for evt in row.get("events", []):
            yield tc_id, run_idx, evt


def _pick_event(rows, tc_id: str | None, event_id: str | None) -> tuple[str, int, dict] | None:
    """Pick the target event: explicit ids if given, else first with cascade."""
    if event_id is not None:
        for tc, run, evt in _iter_event_results(rows):
            if evt.get("event_id") == event_id and (tc_id is None or tc == tc_id):
                return tc, run, evt
        return None
    # Auto-pick: first event with len(trace) > 1
    for tc, run, evt in _iter_event_results(rows):
        if tc_id is not None and tc != tc_id:
            continue
        if len(evt.get("trace") or []) > 1:
            return tc, run, evt
    return None


def _format_ms(v) -> str:
    if v is None:
        return "    n/a"
    return f"{v:>7.1f}ms"


def _print_cascade_tree(spans: list[dict]) -> None:
    """Print an indented ASCII tree following parent_id links."""
    by_id: dict[str, dict] = {s["msg_id"]: s for s in spans}
    children: dict[str | None, list[dict]] = defaultdict(list)
    roots: list[dict] = []
    for s in spans:
        pid = s.get("parent_id")
        if pid is None or pid not in by_id:
            roots.append(s)
        else:
            children[pid].append(s)
    # Sort children by creation offset for stable rendering.
    for kids in children.values():
        kids.sort(key=lambda x: x.get("t_offset_ms") or 0)

    def render(span: dict, depth: int) -> None:
        indent = "  " * depth
        sender = span.get("sender") or "?"
        recipient = span.get("recipient") or "?"
        msg_type = span.get("type") or "?"
        arrow = "↩" if msg_type == "reply" else "→"
        t_off = _format_ms(span.get("t_offset_ms"))
        proc = _format_ms(span.get("processing_ms"))
        llm = _format_ms(span.get("llm_latency_ms"))
        # Mark external/system senders with [brackets] to match _print_message style.
        label = f"[{sender}]" if sender.startswith("__") else sender
        print(f"{indent}{label} {arrow} {recipient:<18} ({msg_type:<6})  t={t_off}  proc={proc}  llm={llm}")
        for child in children.get(span["msg_id"], []):
            render(child, depth + 1)

    for r in roots:
        render(r, 0)


def _print_sender_breakdown(spans: list[dict]) -> None:
    """Per-sender aggregate: count, sum(processing), sum(llm), mean(processing)."""
    buckets: dict[str, dict] = defaultdict(lambda: {"count": 0, "proc_sum": 0.0, "llm_sum": 0.0})
    for s in spans:
        sender = s.get("sender") or "?"
        b = buckets[sender]
        b["count"] += 1
        if s.get("processing_ms") is not None:
            b["proc_sum"] += s["processing_ms"]
        if s.get("llm_latency_ms") is not None:
            b["llm_sum"] += s["llm_latency_ms"]

    print()
    print(f"{'sender':<24} {'hops':>5} {'sum_proc':>11} {'sum_llm':>11} {'mean_proc':>11}")
    print("-" * 65)
    for sender, b in sorted(buckets.items(), key=lambda kv: -kv[1]["proc_sum"]):
        count = b["count"]
        proc_sum = b["proc_sum"]
        llm_sum = b["llm_sum"]
        mean = proc_sum / count if count else 0.0
        print(f"{sender:<24} {count:>5} {proc_sum:>9.1f}ms {llm_sum:>9.1f}ms {mean:>9.1f}ms")


def _print_totals(spans: list[dict]) -> None:
    if not spans:
        return
    end_offset = max((s.get("t_offset_ms") or 0) + (s.get("processing_ms") or 0) for s in spans)
    total_llm = sum(s.get("llm_latency_ms") or 0 for s in spans)
    total_proc = sum(s.get("processing_ms") or 0 for s in spans)
    in_tok = sum(s.get("input_tokens") or 0 for s in spans)
    out_tok = sum(s.get("output_tokens") or 0 for s in spans)
    print()
    print(f"hops={len(spans)}  end-to-end={end_offset:.1f}ms  total_proc={total_proc:.1f}ms"
          f"  total_llm={total_llm:.1f}ms  tokens_in={in_tok}  tokens_out={out_tok}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Render an event cascade trace from eval results.")
    ap.add_argument("--results", required=True, help="Path to *_results.jsonl from evaluate.py")
    ap.add_argument("--tc-id", default=None, help="Filter to a specific test-case id")
    ap.add_argument("--event-id", default=None, help="Render a specific event_id")
    args = ap.parse_args()

    rows = _load_jsonl(Path(args.results))
    pick = _pick_event(rows, args.tc_id, args.event_id)
    if pick is None:
        print("No event with a cascading trace found.")
        return
    tc_id, run_idx, evt = pick
    spans = evt.get("trace") or []
    root_id = evt.get("trace_root_id")

    print(f"tc_id={tc_id}  run={run_idx}  event_id={evt.get('event_id')}  trace_root_id={root_id}")
    print(f"passed={evt.get('passed')}  hops={len(spans)}")
    print()
    print("Cascade:")
    _print_cascade_tree(spans)
    _print_sender_breakdown(spans)
    _print_totals(spans)


if __name__ == "__main__":
    main()
