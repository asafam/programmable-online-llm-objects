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


def _upload_to_firestore(unified_path: Path, service_account: Path | None, run_id: str | None = None,
                         dataset_name: str | None = None, dataset_version: str | None = None) -> None:
    """Upload workflows-mods.jsonl to Firestore.

    A dataset is identified by a unique (name, version) pair, enforced here.

    Versioning:
    - datasets/{name@version} one doc per dataset version — must be UNIQUE (refuses dupes)
    - samples/{id}          frozen on first write — never overwritten
    - sample_summaries/{id} always refreshed (order, run_id)
    - runs/{run_id}         metadata doc per upload
    - annotations/          never touched
    """
    import json
    from datetime import datetime, timezone
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
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # Dataset identity = unique (name, version). Refuse a duplicate so each upload is a
    # distinct, named, versioned dataset.
    ds_key = None
    if dataset_name and dataset_version:
        ds_key = f"{dataset_name}@{dataset_version}"
        if db.collection("datasets").document(ds_key).get().exists:
            raise ValueError(
                f"dataset '{dataset_name}' version '{dataset_version}' already uploaded "
                f"(datasets/{ds_key}). Bump the version to upload a new one."
            )
    print(f"\nUploading {len(samples)} samples to Firestore "
          f"(dataset={dataset_name or '?'} v={dataset_version or '?'}, run={run_id})...")

    # Fetch existing IDs to avoid overwriting frozen docs
    existing_ids = {ref.id for ref in db.collection("samples").list_documents()}
    new_samples = [s for s in samples if s["id"] not in existing_ids]
    skipped = len(samples) - len(new_samples)
    print(f"  {len(new_samples)} new, {skipped} already exist (skipped).")

    BATCH_SIZE = 500

    def commit_batches(entries, label):
        if not entries:
            print(f"  {label}: nothing to write")
            return
        total = 0
        for i in range(0, len(entries), BATCH_SIZE):
            batch = db.batch()
            for ref, data in entries[i:i + BATCH_SIZE]:
                batch.set(ref, data)
            batch.commit()
            total += len(entries[i:i + BATCH_SIZE])
            print(f"  {label}: {total}/{len(entries)} committed")

    # Full sample docs are ~24KB; use small batches to stay under Firestore's 10MB/batch limit
    FULL_BATCH_SIZE = 20
    full_entries = [(db.collection("samples").document(s["id"]), s) for s in new_samples]
    if not full_entries:
        print("  samples: nothing to write")
    else:
        total = 0
        for i in range(0, len(full_entries), FULL_BATCH_SIZE):
            batch = db.batch()
            for ref, data in full_entries[i:i + FULL_BATCH_SIZE]:
                batch.set(ref, data)
            batch.commit()
            total += len(full_entries[i:i + FULL_BATCH_SIZE])
            print(f"  samples: {total}/{len(full_entries)} committed")

    # Replace sample_summaries entirely so stale entries from old runs don't linger
    old_summary_refs = list(db.collection("sample_summaries").list_documents())
    if old_summary_refs:
        for i in range(0, len(old_summary_refs), BATCH_SIZE):
            batch = db.batch()
            for ref in old_summary_refs[i:i + BATCH_SIZE]:
                batch.delete(ref)
            batch.commit()
        print(f"  Deleted {len(old_summary_refs)} old summary docs.")

    # Build version map: sample_id → all ids (ordered as in JSONL)
    versions_by_sample_id: dict[str, list[str]] = {}
    for s in samples:
        sid = s.get("sample_id", s["id"])
        versions_by_sample_id.setdefault(sid, []).append(s["id"])

    # Deduplicate by sample_id — keep only the last entry per group
    latest: dict[str, tuple[int, dict]] = {}
    for idx, sample in enumerate(samples):
        latest[sample.get("sample_id", sample["id"])] = (idx, sample)
    summary_entries = []
    for idx, sample in latest.values():
        first_mod = (sample.get("modifications") or [{}])[0]
        sid = sample.get("sample_id", sample["id"])
        summary_entries.append((db.collection("sample_summaries").document(sample["id"]), {
            "id": sample["id"],
            "sample_id": sid,
            "name": sample.get("name", ""),
            "domain": sample.get("domain", ""),
            "source_type": sample.get("source_type", ""),
            "link": sample.get("link", ""),
            "mod_type": first_mod.get("mod_type"),
            "ambiguity": first_mod.get("ambiguity"),
            "order": idx,
            "run_id": run_id,
            "versions": versions_by_sample_id.get(sid, [sample["id"]]),
        }))
    print(f"  {len(samples)} samples → {len(summary_entries)} unique workflows (deduplicated by sample_id).")
    commit_batches(summary_entries, "sample_summaries")

    # Record run metadata
    db.collection("runs").document(run_id).set({
        "run_id": run_id,
        "dataset_name": dataset_name,
        "dataset_version": dataset_version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_file": str(unified_path),
        "total_samples": len(samples),
        "new_samples": len(new_samples),
        "skipped_samples": skipped,
    })
    print(f"  Recorded run metadata → runs/{run_id}")

    # Record the dataset (name, version) — unique by construction (checked above).
    if ds_key:
        db.collection("datasets").document(ds_key).set({
            "name": dataset_name,
            "version": dataset_version,
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sample_ids": [s["id"] for s in samples],
            "templates": versions_by_sample_id,
        })
        print(f"  Recorded dataset → datasets/{ds_key}")
    print(f"  Upload complete. {len(new_samples)} new, {skipped} frozen (skipped).")


def run(args: argparse.Namespace) -> Path:
    if args.provider is None:
        args.provider = infer_provider(args.model)
    td = args.target_dir
    td.mkdir(parents=True, exist_ok=True)

    # Dataset identity = (name, version). Defaults to the target-dir name + "v1" so the
    # run is always a named, versioned dataset; the sample ids embed it.
    dataset_name = getattr(args, "dataset_name", None) or td.name
    dataset_version = getattr(args, "dataset_version", None) or "v1"
    run_tag = f"{dataset_name}-{dataset_version}"

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
    # run_tag = {dataset_name}-{version} versions the sample id ({template}__{run_tag}) so
    # each dataset version uploads as new, re-annotatable samples (upload freezes existing ids).
    bind_spec.run(_common(args, input=mods_path, output=unified_path, run_tag=run_tag))

    print("\n--- Phase 2 validation ---")
    _validate_unified(unified_path)

    print(f"\nDone. Unified (eval-compatible) output: {unified_path}")
    print(f"Artifacts: {infused_path.name}, {spec_path.name}, {mods_path.name}, {unified_path.name}")

    if args.upload:
        _upload_to_firestore(unified_path, args.service_account, getattr(args, "run_id", None),
                             dataset_name=dataset_name, dataset_version=dataset_version)

    return unified_path


def build_parser() -> argparse.ArgumentParser:
    from src.data.generate_samples import MODIFICATION_TYPES, AMBIGUITY_DESCRIPTIONS
    p = argparse.ArgumentParser(description="Two-phase pipeline: object-agnostic spec → derived agent graph.")
    p.add_argument("--input", "-i", type=Path, required=True, help="Raw templates YAML")
    p.add_argument("--target-dir", "-t", type=Path, required=True, help="Directory for all artifacts")
    p.add_argument("--dataset-name", dest="dataset_name", default=None,
                   help="Dataset name (default: target-dir name). With --dataset-version forms the unique dataset identity and the sample-id suffix.")
    p.add_argument("--dataset-version", dest="dataset_version", default=None,
                   help="Dataset version, e.g. v1/v2 (default: v1). A (name, version) pair must be unique on upload.")
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
    p.add_argument("--run-id", dest="run_id", default=None,
                   help="Run identifier for this upload (default: UTC timestamp)")
    return p


def main():
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
