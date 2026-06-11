"""
Iteration orchestration for state-scenario datasets — the two operations that were previously
ad-hoc shell scripts, now first-class and verified:

  rebuild   Re-run the DETERMINISTIC layers (base events, modification scenario, analysis
            publication) from a stored spec.jsonl over an existing unified sample — no LLM
            calls. Use after a builder/contract fix that doesn't change LLM-authored content.
            CAVEAT: all events are re-bound to the sample's FIRST entry recipient — for the
            (rare) multi-entry sample, re-run the full pipeline bind instead.

  merge     Combine keeper samples with regenerated ones into one release file (plain ids,
            one entry per template — later sources win), then run the blocking verifier.

Usage:
    python -m src.data.assemble rebuild --spec <dir-or-spec.jsonl> --unified <workflows-mods.jsonl> [--id ID ...]
    python -m src.data.assemble merge -o <out.jsonl> <input1.jsonl> <input2.jsonl> ...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _rebuild_one(spec, tc: dict) -> dict:
    """Re-derive base events + the modification scenario for one sample from its spec,
    preserving the bound recipients/objects/graph and keeping seed→mock tools in sync."""
    from src.data.schema import GeneratedScenarioSpec
    from src.data.generate_state_constraints import (
        _build_scenario, build_mod_scenario, publish_analysis_results,
    )
    gen = GeneratedScenarioSpec(
        constraint_type=spec.state_constraint.type,
        threshold=spec.state_constraint.threshold,
        description=spec.state_constraint.description,
        **{k: getattr(spec, k) for k in (
            "seed", "phrasings", "decorations", "key", "unit", "entities", "keys",
            "key_contents", "key_contacts", "analysis_field", "analysis_label",
            "analysis_terms", "analysis_values", "irrelevant_deco", "branch_demos",
            "cap_scope", "person_caps", "qty_noun")},
    )
    spec.base_events = _build_scenario(gen)
    mt = tc["modifications"][0]["mod_type"] if tc.get("modifications") else "correction"
    intent, mod_when, post = build_mod_scenario(spec, mt)
    publish_analysis_results(spec, post)
    tc["seed"] = spec.seed
    for t in tc.get("tools", []):
        if t["tool_name"].endswith("_data"):
            t["response_template"] = spec.seed
    entry = next(e["recipient"] for e in tc["events"] if e["role"] == "base")
    # mirror bind: base events are renamed SC### in (when, id) order — builder/insert ids
    # (E001/E050/E07x) must never reach the released sample
    base_sorted = sorted(spec.base_events, key=lambda e: (e.when or "", e.id))
    base = []
    for i, e in enumerate(base_sorted, 1):
        d = dict(e.model_dump(), recipient=entry)
        d["id"] = f"SC{i:03d}"
        base.append(d)
    tc["events"] = base + [dict(e.model_dump(), recipient=entry) for e in post]
    if tc.get("modifications"):
        tc["modifications"] = [{**tc["modifications"][0], "intent": intent, "when": mod_when}]
    return tc


def rebuild(args) -> int:
    from src.data.schema import WorkflowSpec
    spec_path = Path(args.spec)
    if spec_path.is_dir():
        spec_path = spec_path / "spec.jsonl"
    specs = {json.loads(l)["id"]: WorkflowSpec.model_validate_json(l) for l in open(spec_path)}
    unified = Path(args.unified)
    rows = {json.loads(l)["sample_id"]: json.loads(l) for l in open(unified)}
    targets = args.id or [sid for sid in rows if sid in specs]
    for sid in targets:
        if sid not in specs:
            print(f"  SKIP {sid}: no spec in {spec_path}")
            continue
        rows[sid] = _rebuild_one(specs[sid], rows[sid])
        print(f"  rebuilt {sid}")
    with open(unified, "w") as f:
        for d in rows.values():
            f.write(json.dumps(d) + "\n")
    from src.data.verify_samples import verify
    return verify(unified)


def merge(args) -> int:
    rows: dict = {}
    for src in args.inputs:                       # later sources WIN per template
        for l in open(src):
            d = json.loads(l)
            d["id"] = d["sample_id"]              # plain ids, always
            rows[d["sample_id"]] = d
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        for d in rows.values():
            f.write(json.dumps(d) + "\n")
    print(f"merged {len(rows)} samples → {out}")
    from src.data.verify_samples import verify
    return verify(out)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("rebuild", help="re-run deterministic layers from a spec over a unified file")
    pr.add_argument("--spec", required=True, help="run dir or spec.jsonl")
    pr.add_argument("--unified", required=True, help="workflows-mods.jsonl to patch in place")
    pr.add_argument("--id", action="append", help="template id(s); default: all present in both")
    pm = sub.add_parser("merge", help="combine sample files (later sources win), verify")
    pm.add_argument("inputs", nargs="+", help="workflows-mods.jsonl files, keepers first")
    pm.add_argument("--output", "-o", required=True)
    a = p.parse_args()
    blocked = rebuild(a) if a.cmd == "rebuild" else merge(a)
    sys.exit(1 if blocked else 0)


if __name__ == "__main__":
    main()
