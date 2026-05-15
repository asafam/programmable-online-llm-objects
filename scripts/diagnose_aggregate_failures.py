#!/usr/bin/env python3
"""Diagnose why LNL underperforms on aggregate / conditional_aggregate probes.

For each FAILED probe of the requested type(s), classify into one of:

  refusal_or_empty   — response text is empty or contains refusal phrasing
  entity_missing     — at least one expected entity ID is absent from state
  entity_stale       — entities present in state, but at least one stale
                       (state value ≠ latest value implied by `expected`)
  arithmetic_error   — entities + values present and correct, but the final
                       sum/count in the response is wrong
  unclassified       — none of the above (printed via --dump for review)

Outputs:
  • Per-system × depth taxonomy table
  • Stale-vs-arithmetic split (the "is the gap retention or computation?" question)
  • Cardinality slice (2-entity vs 3-entity probes)
  • --dump N: prints N raw examples per (system, category)

Usage:
    python scripts/diagnose_aggregate_failures.py \\
        --tcs outputs/.../probe_dataset_full.jsonl \\
        --lnl outputs/.../probes_lnl.jsonl \\
        --baseline outputs/.../probes_baseline.jsonl \\
        --probe-types aggregate conditional_aggregate \\
        --dump 5
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

PROBE2_RE = re.compile(r"-probe2-D(\d+)-S(\d+)-TC")
ENTITY_ID_RE = re.compile(r"\b([A-Z][A-Z0-9_]*-\d{2,})\b")
PROBE_TYPES = ("direct_lookup", "aggregate", "conditional_aggregate")
CATEGORIES = ("refusal_or_empty", "entity_missing", "entity_stale",
              "arithmetic_error", "unclassified")
REFUSAL_PATTERNS = re.compile(
    r"(i (don'?t|do not) know|insufficient (information|data)|"
    r"unable to (determine|answer|compute|find)|cannot (determine|answer|find|compute)|"
    r"no (information|data) available|not enough (information|data)|"
    r"i can'?t (compute|determine|answer|find|tell)|"
    r"(do not|don'?t) have (a |any |the )?(employee_?count|recorded|entries|records)|"
    r"do not currently have|i do not have|"
    r"none of the .* (have|has|are|is)|"
    r"no (tracked )?(records?|entities|tickets?|deals?|leads?|posts?|items?) (have|has|are|is|with|that|matching|meeting|qualify)|"
    r"^[^.!?\n]{0,80}:\s*$)",
    re.IGNORECASE | re.MULTILINE,
)


# ── Loading ────────────────────────────────────────────────────────────────────

def load_tcs(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if "id" in d and "events" in d:
                out[d["id"]] = d
    return out


def load_results(path: Path) -> dict[str, dict[str, dict]]:
    """Load eval results JSONL → {tc_id → {event_id → EventResult dict}}."""
    out: dict[str, dict[str, dict]] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("record_type") == "run_config":
                continue
            tc_id = d.get("tc_id")
            if not tc_id or d.get("run_index", 0) != 0:
                continue
            out[tc_id] = {er["event_id"]: er for er in d.get("events", [])}
    return out


# ── Evidence parsing (LNL vs baseline) ─────────────────────────────────────────

def split_evidence(evidence: str, system: str) -> tuple[str, str]:
    """Return (response_text, state_text). Heuristic — handles both formats."""
    if not evidence:
        return ("", "")
    # Baseline: "Response:\n...\n\nUpdated state:\n..."
    if "Updated state:" in evidence and "Response:" in evidence:
        head, _, state = evidence.partition("Updated state:")
        resp = head.split("Response:", 1)[1].strip()
        return (resp.strip(), state.strip())
    # LNL: "=== THIS EVENT ===\n...Replies:\n  [obj]: text\n\n=== OBJECT STATES ==="
    if "=== OBJECT STATES" in evidence:
        before_states, _, states = evidence.partition("=== OBJECT STATES")
        resp = ""
        if "Replies:" in before_states:
            replies_block = before_states.split("Replies:", 1)[1]
            # Take all reply lines: "  [object_name]: reply text"
            replies = []
            for ln in replies_block.splitlines():
                ln = ln.rstrip()
                if not ln.strip():
                    continue
                if ln.lstrip().startswith("[") and "]:" in ln:
                    replies.append(ln.split("]:", 1)[1].strip())
            resp = " | ".join(replies)
        return (resp, states.strip())
    return (evidence.strip(), "")


def extract_entities(text: str) -> list[str]:
    """Extract entity IDs (e.g. EMAIL-1002, LEAD-1001) preserving order, deduped."""
    seen: dict[str, None] = {}
    for m in ENTITY_ID_RE.finditer(text or ""):
        seen.setdefault(m.group(1), None)
    return list(seen.keys())


def _expected_numbers_per_entity(expected: str) -> dict[str, list[tuple[str, str]]]:
    """For each entity ID in expected text, return the numeric (field, value) pairs
    associated with it. Splits on entity-boundary so a number "stays" with the
    nearest preceding entity ID — more robust than the union-of-pairs heuristic."""
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    if not expected:
        return out
    # Find entity positions, then for each entity take the slice up to next entity
    matches = list(ENTITY_ID_RE.finditer(expected))
    pair_re = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([\-\d][\d,\.]*)")
    for i, m in enumerate(matches):
        ent = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(expected)
        chunk = expected[start:end]
        for f, v in pair_re.findall(chunk):
            pair = (f, v.rstrip("."))
            if pair not in out[ent]:
                out[ent].append(pair)
        # Also accept "ent: <number>" form (no field name)
        m2 = re.match(r"\s*[:\-—]\s*\$?([\-\d][\d,\.]*)", chunk)
        if m2:
            out[ent].append(("(value)", m2.group(1).rstrip(".")))
    return out


def extract_entity_values(expected: str) -> dict[str, list[tuple[str, str]]]:
    """For each entity ID in expected text, return [(field, value), ...] pairs.

    Heuristic: split on ';' then look for `entity ... field=value` or
    `field=value` near the entity name. Best-effort — used only as evidence
    for the entity_stale check.
    """
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    if not expected:
        return out
    pair_re = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^,;)\s][^,;)]*)")
    for chunk in re.split(r"[;\n]+", expected):
        ents = extract_entities(chunk)
        if not ents:
            continue
        pairs = [(f.strip(), v.strip().rstrip(".")) for f, v in pair_re.findall(chunk)]
        for ent in ents:
            for fv in pairs:
                if fv not in out[ent]:
                    out[ent].append(fv)
    return out


def extract_total_number(text: str) -> str | None:
    """Pull the final answer number from `expected` (after 'total', 'Result', '=', etc.)."""
    if not text:
        return None
    # Try patterns like "total ... = N" or "total ... is N"
    patterns = [
        r"total[^=]*=\s*([\-\d][\d,\.\$\s]*)\b",
        r"total[^:]*:\s*([\-\d][\d,\.\$\s]*)\b",
        r"combined[^=]*=\s*([\-\d][\d,\.\$\s]*)\b",
        r"sum[^=]*=\s*([\-\d][\d,\.\$\s]*)\b",
        r"count[^=]*=\s*([\-\d][\d,\.\$\s]*)\b",
        r"\bResult:\s*[^.]*?([\-\d][\d,\.\$\s]*)\b",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(1).strip().rstrip(".")
    return None


def normalize_number(s: str) -> str:
    """Normalize a number for comparison: strip $, commas, spaces, % suffix."""
    if s is None:
        return ""
    s = s.replace(",", "").replace("$", "").replace(" ", "")
    s = s.rstrip(".%")
    return s


# ── Classification ─────────────────────────────────────────────────────────────

def classify_failure(
    expected: str,
    response: str,
    state: str,
) -> tuple[str, dict]:
    """Return (category, details_dict)."""
    details: dict = {}

    if not response.strip() or REFUSAL_PATTERNS.search(response):
        details["reason"] = "empty response" if not response.strip() else "refusal pattern"
        return ("refusal_or_empty", details)

    expected_entities = extract_entities(expected)
    if not expected_entities:
        details["reason"] = "no entities in expected text"
        return ("unclassified", details)

    missing = [e for e in expected_entities if e not in state]
    if missing:
        details["missing_entities"] = missing
        return ("entity_missing", details)

    # Per-entity expected numeric values from the `expected` text
    expected_per_entity_numbers = _expected_numbers_per_entity(expected)
    stale: list[tuple[str, str, str]] = []  # (entity, field_or_marker, comparison)
    # Find all entity-ID positions in state to bound per-entity slices
    state_entity_positions = [(m.start(), m.group(1)) for m in ENTITY_ID_RE.finditer(state)]
    for ent, exp_nums in expected_per_entity_numbers.items():
        if ent not in state or not exp_nums:
            continue
        state_nums: set[str] = set()
        for i, (pos, ent_at) in enumerate(state_entity_positions):
            if ent_at != ent:
                continue
            # Slice up to next entity occurrence (any entity), capped at 800 chars
            next_pos = state_entity_positions[i + 1][0] if i + 1 < len(state_entity_positions) else len(state)
            end = min(pos + 800, next_pos)
            slice_ = state[pos:end]
            for n in re.findall(r"[\-]?\d[\d,\.]*", slice_):
                state_nums.add(normalize_number(n))
        for f, v in exp_nums:
            nv = normalize_number(v)
            if nv and nv not in state_nums:
                stale.append((ent, f, f"expected={v} state_nums={sorted(state_nums)[:6]}"))
                break
    if stale:
        details["stale"] = stale[:5]
        return ("entity_stale", details)

    expected_total = extract_total_number(expected)
    if expected_total:
        norm_exp = normalize_number(expected_total)
        # Search response for any number matching expected; if no match → arithmetic error
        nums_in_response = re.findall(r"[\-\d][\d,\.]*", response)
        norm_resp_nums = {normalize_number(n) for n in nums_in_response}
        if norm_exp not in norm_resp_nums:
            details["expected_total"] = expected_total
            details["response_numbers"] = sorted(norm_resp_nums)[:8]
            return ("arithmetic_error", details)

    details["reason"] = "all heuristics passed but probe failed per judge"
    return ("unclassified", details)


# ── Probe metadata helpers ─────────────────────────────────────────────────────

def parse_depth(tc_id: str) -> int | None:
    m = PROBE2_RE.search(tc_id)
    return int(m.group(1)) if m else None


def probe_type_of(tc_event: dict) -> str:
    reason = (tc_event.get("expect") or {}).get("reason", "")
    return reason if reason in PROBE_TYPES else "unknown"


def cardinality_of(expected: str) -> int:
    return len(extract_entities(expected))


# ── Iterate failed probes & classify ───────────────────────────────────────────

def diagnose_system(
    tcs: dict[str, dict],
    results: dict[str, dict[str, dict]],
    system: str,
    probe_types: tuple[str, ...],
) -> list[dict]:
    """Yield one record per FAILED probe of requested types, with classification."""
    rows: list[dict] = []
    for tc_id, tc in tcs.items():
        depth = parse_depth(tc_id)
        if depth is None:
            continue
        ev_results = results.get(tc_id)
        if not ev_results:
            continue
        for tc_ev in tc.get("events", []):
            if tc_ev.get("role") != "post_mod":
                continue
            ptype = probe_type_of(tc_ev)
            if ptype not in probe_types:
                continue
            er = ev_results.get(tc_ev["id"])
            if er is None or er.get("passed", False):
                continue  # only failed probes
            response, state = split_evidence(er.get("evidence", ""), system)
            category, details = classify_failure(
                er.get("expected") or "",
                response,
                state,
            )
            rows.append({
                "system": system,
                "tc_id": tc_id,
                "event_id": tc_ev["id"],
                "depth": depth,
                "probe_type": ptype,
                "cardinality": cardinality_of(er.get("expected") or ""),
                "category": category,
                "expected": er.get("expected") or "",
                "response": response,
                "state_excerpt": state[:600],
                "judge_reasoning": er.get("reasoning") or "",
                "details": details,
            })
    return rows


# ── Tables ─────────────────────────────────────────────────────────────────────

def _line(w: int) -> str:
    return "─" * w


CATEGORY_LABELS = {
    "refusal_or_empty": "refusal",
    "entity_missing":   "missing",
    "entity_stale":     "stale",
    "arithmetic_error": "arith",
    "unclassified":     "other",
}


def print_taxonomy_table(failures: list[dict], probe_types: tuple[str, ...]) -> None:
    systems = sorted({f["system"] for f in failures}) or ["(none)"]
    depths = sorted({f["depth"] for f in failures})

    cell_w = 8
    sys_w = max(8, max((len(s) for s in systems), default=8))
    inner = 5 + 2 + sys_w + 2 + len(CATEGORIES) * (cell_w + 2) + 6
    total_w = inner

    print("\n┌" + _line(total_w) + "┐")
    print("│" + "FAILURE TAXONOMY (failed probes only)".center(total_w) + "│")
    print("├" + _line(total_w) + "┤")

    header = (f"│{'Depth':>5}  {'System':<{sys_w}}  "
              + "".join(f"{CATEGORY_LABELS[c]:^{cell_w}}  " for c in CATEGORIES)
              + f"{'TOT':>4}")
    pad = total_w - (len(header) - 1)
    print(header + " " * pad + "│")
    print("├" + _line(total_w) + "┤")

    for depth in depths:
        for sys in systems:
            counts = {c: 0 for c in CATEGORIES}
            for f in failures:
                if f["depth"] == depth and f["system"] == sys:
                    counts[f["category"]] += 1
            tot = sum(counts.values())
            row = (f"│{depth:>5}  {sys:<{sys_w}}  "
                   + "".join(f"{counts[c]:^{cell_w}}  " for c in CATEGORIES)
                   + f"{tot:>4}")
            pad = total_w - (len(row) - 1)
            print(row + " " * pad + "│")
        if depth != depths[-1]:
            print("├" + _line(total_w) + "┤")
    print("└" + _line(total_w) + "┘")


def print_retention_vs_arithmetic(failures: list[dict]) -> None:
    """Step 2: of failed probes, what % is hidden retention vs pure arithmetic?"""
    systems = sorted({f["system"] for f in failures})
    print("\n┌" + _line(78) + "┐")
    print("│" + "RETENTION-vs-ARITHMETIC SPLIT (of failed probes)".center(78) + "│")
    print("├" + _line(78) + "┤")
    print(f"│{'Depth':>6}  {'System':<10}  {'stale (retention)':^20}  "
          f"{'arithmetic':^14}  {'other':^10}  {'N':^6}│")
    print("├" + _line(78) + "┤")
    depths = sorted({f["depth"] for f in failures})
    for depth in depths:
        for sys in systems:
            subset = [f for f in failures if f["depth"] == depth and f["system"] == sys]
            n = len(subset)
            if n == 0:
                continue
            stale = sum(1 for f in subset if f["category"] == "entity_stale")
            arith = sum(1 for f in subset if f["category"] == "arithmetic_error")
            other = n - stale - arith
            stale_pct = f"{stale}/{n} ({100*stale/n:4.0f}%)"
            arith_pct = f"{arith}/{n} ({100*arith/n:4.0f}%)"
            other_pct = f"{100*other/n:4.0f}%"
            print(f"│{depth:>6}  {sys:<10}  {stale_pct:^20}  "
                  f"{arith_pct:^14}  {other_pct:^10}  {n:^6}│")
    print("└" + _line(78) + "┘")


def print_cardinality_slice(failures: list[dict]) -> None:
    """Step 3: failure rate by number of target entities (2 vs 3+)."""
    systems = sorted({f["system"] for f in failures})
    print("\n┌" + _line(78) + "┐")
    print("│" + "FAILURE COUNTS BY TARGET-ENTITY CARDINALITY".center(78) + "│")
    print("├" + _line(78) + "┤")
    print(f"│{'System':<10}  {'cardinality':>12}  {'refusal':^9}  "
          f"{'missing':^9}  {'stale':^9}  {'arith':^9}  {'unclass':^9}  {'N':>5}│")
    print("├" + _line(78) + "┤")
    cards = sorted({f["cardinality"] for f in failures if f["cardinality"] > 0})
    for sys in systems:
        for c in cards:
            subset = [f for f in failures if f["system"] == sys and f["cardinality"] == c]
            n = len(subset)
            if n == 0:
                continue
            cnt = {cat: sum(1 for f in subset if f["category"] == cat) for cat in CATEGORIES}
            print(f"│{sys:<10}  {c:>12}  {cnt['refusal_or_empty']:^9}  "
                  f"{cnt['entity_missing']:^9}  {cnt['entity_stale']:^9}  "
                  f"{cnt['arithmetic_error']:^9}  {cnt['unclassified']:^9}  {n:>5}│")
    print("└" + _line(78) + "┘")


# ── Dump examples ──────────────────────────────────────────────────────────────

def dump_examples(failures: list[dict], n_per_cat: int) -> None:
    if n_per_cat <= 0:
        return
    print("\n" + "═" * 80)
    print(f"  DUMPED EXAMPLES (up to {n_per_cat} per system × category)")
    print("═" * 80)
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for f in failures:
        by_key[(f["system"], f["category"])].append(f)

    for sys in sorted({k[0] for k in by_key}):
        for cat in CATEGORIES:
            items = by_key.get((sys, cat), [])[:n_per_cat]
            if not items:
                continue
            print(f"\n──[ {sys} / {cat} ]──  (showing {len(items)} of {len(by_key[(sys, cat)])})")
            for f in items:
                print(f"\n  TC: {f['tc_id']}  |  {f['event_id']}  |  "
                      f"D={f['depth']}  ptype={f['probe_type']}  card={f['cardinality']}")
                print(f"  EXPECTED: {f['expected'][:240]}")
                print(f"  RESPONSE: {f['response'][:240] or '(empty)'}")
                if cat == "entity_missing":
                    print(f"  MISSING:  {f['details'].get('missing_entities')}")
                elif cat == "entity_stale":
                    for s in f["details"].get("stale", [])[:3]:
                        print(f"  STALE:    {s[0]} {s[1]}: {s[2]}")
                elif cat == "arithmetic_error":
                    print(f"  EXP_TOTAL: {f['details'].get('expected_total')}  "
                          f"RESP_NUMS: {f['details'].get('response_numbers')}")
                print(f"  JUDGE:    {f['judge_reasoning'][:240]}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--tcs", type=Path, required=True,
                   help="Probe-dataset TCs JSONL")
    p.add_argument("--lnl", type=Path, default=None,
                   help="LNL eval results JSONL")
    p.add_argument("--baseline", type=Path, default=None,
                   help="OpenClaw baseline eval results JSONL")
    p.add_argument("--probe-types", nargs="+", default=["aggregate", "conditional_aggregate"],
                   choices=PROBE_TYPES,
                   help="Which probe types to diagnose (default: aggregate + conditional_aggregate)")
    p.add_argument("--dump", type=int, default=0, metavar="N",
                   help="Print N example failures per (system, category)")
    p.add_argument("--output", type=Path, default=None,
                   help="Write classified failures to JSON file")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if not args.lnl and not args.baseline:
        print("ERROR: provide at least one of --lnl or --baseline", file=sys.stderr)
        sys.exit(1)

    print(f"Loading TCs from {args.tcs} …")
    tcs = load_tcs(args.tcs)
    print(f"  {len(tcs)} TCs loaded")

    failures: list[dict] = []
    if args.lnl:
        print(f"Loading LNL results from {args.lnl} …")
        r = load_results(args.lnl)
        print(f"  {len(r)} TC results loaded")
        failures.extend(diagnose_system(tcs, r, "lnl", tuple(args.probe_types)))
    if args.baseline:
        print(f"Loading baseline results from {args.baseline} …")
        r = load_results(args.baseline)
        print(f"  {len(r)} TC results loaded")
        failures.extend(diagnose_system(tcs, r, "baseline", tuple(args.probe_types)))

    if not failures:
        print("\nNo failed probes found for the requested probe types.")
        return

    print(f"\nClassified {len(failures)} failed probes "
          f"({', '.join(args.probe_types)}).")
    print_taxonomy_table(failures, tuple(args.probe_types))
    print_retention_vs_arithmetic(failures)
    print_cardinality_slice(failures)
    dump_examples(failures, args.dump)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"failures": failures}, indent=2))
        print(f"\nWrote classified failures → {args.output}")


if __name__ == "__main__":
    main()
