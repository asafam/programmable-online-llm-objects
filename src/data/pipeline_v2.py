"""
Two-phase data-generation pipeline (agent graph decoupled, derived downstream).

Phase 1 — object-agnostic SPEC:
    generate_spec            templates.yaml      -> spec.jsonl
    generate_state_constraints (opt-in)          -> spec-constraints.jsonl   (infuse state)
    generate_mods            (modifications)     -> spec-mods.jsonl

Phase 2 — binding:
    bind_spec                derive graph + bind -> workflows-mods.jsonl  (UNIFIED, eval-compatible)

Artifacts persisted in --target-dir: spec.jsonl, spec-constraints.jsonl, spec-mods.jsonl,
agent-graph is derived inside bind_spec, and workflows-mods.jsonl is the unified file.

Usage:
    python -m src.data.pipeline_v2 -i data/zapier/raw/templates.yaml \
        --target-dir outputs/data/zapier/<run> --state-constraint --mod-type expansion
"""
from __future__ import annotations

import argparse
from datetime import datetime  # noqa: F401  (kept for parity; not used for naming here)
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

load_dotenv()

from src.data import generate_spec, generate_state_constraints, generate_mods, bind_spec
from src.data.utils import infer_provider


def _validate_object_free(path: Path) -> int:
    """Deterministic: a Phase-1 artifact must carry NO object reference."""
    import json
    leaked = 0
    for line in open(path):
        d = json.loads(line)
        bad = [k for k in ("object_id", "recipient", "\"target\"") if k in line]
        if bad:
            leaked += 1
            print(f"  [object-free] {d.get('id')}: leaked {bad}")
    print(f"  object-free invariant: {'OK' if not leaked else f'{leaked} spec(s) leaked object refs'}")
    return leaked


def _validate_unified(path: Path) -> int:
    """Deterministic: custodian-graph checks on the bound output (incl. invariant→custodian,
    custodian reachability) + recipient/target membership."""
    from src.data.schema import Sample
    from src.data.validate_workflow_objects import _custodian_graph_issues
    flagged = 0
    for line in open(path):
        s = Sample.model_validate_json(line)
        ids = {o.object_id for o in s.objects}
        issues = list(_custodian_graph_issues(s))
        issues += [f"event {e.id} recipient '{e.recipient}' ∉ objects" for e in s.events if e.recipient not in ids]
        issues += [f"mod {m.id} target '{m.target}' ∉ objects" for m in s.modifications if m.target not in ids]
        if issues:
            flagged += 1
            for i in issues:
                print(f"  [graph] {s.id}: {i}")
    print(f"  graph/membership: {'OK' if not flagged else f'{flagged} workflow(s) flagged'}")
    return flagged


def _common(args, **over):
    base = dict(provider=args.provider, model=args.model, seed=args.seed,
                temperature=args.temperature, force=args.force, limit=args.limit,
                workers=args.workers, ids=args.ids)
    base.update(over)
    return SimpleNamespace(**base)


def _upload_to_firestore(unified_path: Path, service_account: Path | None) -> None:
    """Upload workflows-mods.jsonl to Firestore (samples + sample_summaries collections)."""
    import json
    import firebase_admin
    from firebase_admin import credentials, firestore

    if not firebase_admin._apps:
        if service_account:
            cred = credentials.Certificate(str(service_account))
        else:
            cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)

    db = firestore.client()
    lines = [l for l in unified_path.read_text().splitlines() if l.strip()]
    samples = [json.loads(l) for l in lines]
    print(f"\nUploading {len(samples)} samples to Firestore...")

    BATCH_SIZE = 500
    full_entries, summary_entries = [], []
    for idx, sample in enumerate(samples):
        first_mod = sample.get("modifications", [{}])[0] if sample.get("modifications") else {}
        full_entries.append((db.collection("samples").document(sample["id"]), sample))
        summary_entries.append((db.collection("sample_summaries").document(sample["id"]), {
            "id": sample["id"],
            "sample_id": sample.get("sample_id", ""),
            "name": sample.get("name", ""),
            "domain": sample.get("domain", ""),
            "source_type": sample.get("source_type", ""),
            "link": sample.get("link", ""),
            "mod_type": first_mod.get("mod_type"),
            "ambiguity": first_mod.get("ambiguity"),
            "order": idx,
        }))

    def commit_batches(entries, label):
        total = 0
        for i in range(0, len(entries), BATCH_SIZE):
            batch = db.batch()
            for ref, data in entries[i:i + BATCH_SIZE]:
                batch.set(ref, data)
            batch.commit()
            total += len(entries[i:i + BATCH_SIZE])
            print(f"  {label}: {total}/{len(entries)} committed")

    commit_batches(full_entries, "samples")
    commit_batches(summary_entries, "sample_summaries")
    print("  Upload complete.")


def run(args: argparse.Namespace) -> Path:
    if args.provider is None:
        args.provider = infer_provider(args.model)
    td = args.target_dir
    td.mkdir(parents=True, exist_ok=True)

    infused_path = td / "spec-infused.jsonl"
    spec_path = td / "spec.jsonl"
    mods_path = td / "spec-mods.jsonl"
    unified_path = td / "workflows-mods.jsonl"

    # ── Phase 1a: INFUSE state FIRST (pre-grounding, from the raw template) ───
    print("\n" + "=" * 60 + "\nPHASE 1a: INFUSE STATE (pre-grounding)\n" + "=" * 60)
    generate_state_constraints.run(_common(args, input=args.input, output=infused_path))

    # ── Phase 1b: GROUND (steps + the infused base scenario together) ─────────
    print("\n" + "=" * 60 + "\nPHASE 1b: GROUND (steps + base scenario)\n" + "=" * 60)
    generate_spec.run(_common(args, input=infused_path, output=spec_path))

    # ── Phase 1c: modifications ──────────────────────────────────────────────
    print("\n" + "=" * 60 + "\nPHASE 1c: MODIFICATIONS\n" + "=" * 60)
    generate_mods.run(_common(
        args, input=spec_path, output=mods_path,
        mod_type=args.mod_type, mods_per_scenario=args.mods_per_scenario,
        ambiguity=args.ambiguity, events_before=args.events_before,
        events_after=args.events_after, events_unrelated=args.events_unrelated,
    ))

    print("\n--- Phase 1 validation ---")
    _validate_object_free(mods_path)

    # ── Phase 2: derive graph + bind → unified ───────────────────────────────
    print("\n" + "=" * 60 + "\nPHASE 2: DERIVE GRAPH + BIND → UNIFIED\n" + "=" * 60)
    bind_spec.run(_common(args, input=mods_path, output=unified_path))

    print("\n--- Phase 2 validation ---")
    _validate_unified(unified_path)

    print(f"\nDone. Unified (eval-compatible) output: {unified_path}")
    print(f"Artifacts: {infused_path.name}, {spec_path.name}, {mods_path.name}, {unified_path.name}")

    if args.upload:
        _upload_to_firestore(unified_path, args.service_account)

    return unified_path


def build_parser() -> argparse.ArgumentParser:
    from src.data.generate_samples import MODIFICATION_TYPES, AMBIGUITY_DESCRIPTIONS
    p = argparse.ArgumentParser(description="Two-phase pipeline: object-agnostic spec → derived agent graph.")
    p.add_argument("--input", "-i", type=Path, required=True, help="Raw templates YAML")
    p.add_argument("--target-dir", "-t", type=Path, required=True, help="Directory for all artifacts")
    p.add_argument("--mod-type", type=str, choices=list(MODIFICATION_TYPES.keys()) + ["mixed"], default=None)
    p.add_argument("--mods-per-scenario", type=int, default=1)
    p.add_argument("--ambiguity", type=str, choices=list(AMBIGUITY_DESCRIPTIONS.keys()) + ["random"], default="random")
    p.add_argument("--events-before", type=int, default=1)
    p.add_argument("--events-after", type=int, default=2)
    p.add_argument("--events-unrelated", type=int, default=None)
    p.add_argument("--id", dest="ids", metavar="ID", action="append", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--workers", "-w", type=int, default=1)
    p.add_argument("--provider", "-p", choices=["openai", "azure", "anthropic", "google"], default="azure")
    p.add_argument("--model", "-m", default="gpt-5.4")
    p.add_argument("--seed", "-s", type=int, default=None)
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--force", action="store_true")
    p.add_argument("--upload", action="store_true",
                   help="Upload unified output to Firestore (samples + sample_summaries) after pipeline completes")
    p.add_argument("--service-account", type=Path, default=None,
                   help="Path to Firebase service account JSON (default: uses GOOGLE_APPLICATION_CREDENTIALS or ADC)")
    return p


def main():
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
