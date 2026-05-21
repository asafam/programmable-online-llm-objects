"""
Data generation pipeline — runs both stages in sequence.

Usage:
    # Full pipeline into a target folder
    python -m src.data.pipeline -i data/zapier/raw/templates.yaml --target-dir outputs/my-run

    # Continue an existing run (skips stage 1 if workflows.jsonl already exists)
    python -m src.data.pipeline -i data/zapier/raw/templates.yaml --target-dir outputs/my-run

    # Skip stage 1 explicitly with a specific samples file
    python -m src.data.pipeline --workflows outputs/data/zapier/templates_samples_object.jsonl

    # With options
    python -m src.data.pipeline -i data/zapier/raw/templates.yaml --target-dir outputs/my-run \\
        --workflows-per-template 3 --scenario-count 2 --mod-type temporal
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import signal

from src.data import generate_workflows, generate_samples, generate_seed
from src.data.validate_test_cases import (
    validate_sample, validate_test_case, print_validation_report,
    BLOCKING_VALIDATORS, WARNING_VALIDATORS,
)
from src.data.utils import load_jsonl

WORKFLOWS_FILENAME = "workflows.jsonl"
WORKFLOWS_MODS_FILENAME = "workflows-mods.jsonl"


def _blocking_issues(issues_by_validator: dict) -> dict:
    """Filter to only blocking-severity issues."""
    return {k: v for k, v in issues_by_validator.items() if k in BLOCKING_VALIDATORS}


def _warning_issues(issues_by_validator: dict) -> dict:
    """Filter to only warning-severity issues."""
    return {k: v for k, v in issues_by_validator.items() if k in WARNING_VALIDATORS}


def _save_samples(path: Path, samples: list) -> None:
    with open(path, "w") as f:
        for s in samples:
            f.write(s.model_dump_json() + "\n")


def _invalidate_test_cases(samples_path: Path, sample_ids: set[str]) -> int:
    """
    Remove test cases whose sample_id is in sample_ids from samples_path.
    Returns number of test cases removed.  No-op if the file doesn't exist.
    """
    if not samples_path or not samples_path.exists() or not sample_ids:
        return 0
    from src.data.schema import Sample
    tcs = load_jsonl(samples_path, Sample)
    kept = [tc for tc in tcs if tc.sample_id not in sample_ids]
    removed = len(tcs) - len(kept)
    if removed:
        with open(samples_path, "w") as f:
            for tc in kept:
                f.write(tc.model_dump_json() + "\n")
        print(f"  [validate] Invalidated {removed} stale test case(s) for {len(sample_ids)} repaired sample(s).")
    return removed


def _ask(prompt: str, choices: tuple[str, ...], default: str) -> str:
    """Interactive single-character prompt. Waits indefinitely for input."""
    opts = "/".join(c.upper() if c == default else c for c in choices)
    print(f"  {prompt} [{opts}] ", end="", flush=True)
    while True:
        try:
            raw = input().strip().lower()
        except EOFError:
            print()
            return default
        if raw == "":
            return default
        if raw in choices:
            return raw
        print(f"  Invalid choice. [{opts}] ", end="", flush=True)


def _autopatch_sample(sample, blocking: dict) -> tuple[bool, dict]:
    """
    Apply deterministic structural patches for known blocking issue types.

    Returns (changed, still_blocking) where still_blocking is the subset of
    blocking issues that could not be fixed automatically.

    Patches applied:
    - find_read_write_misclassifications: add _data skill + basic mock tool to
      the target object; remove any residual write-only behavior text.
    - find_invalid_peer_declarations: remove dangling peer references.
    """
    import re
    from src.data.schema import MockToolDef

    changed = False
    remaining = dict(blocking)
    object_map = {o.object_id: o for o in sample.objects}

    # --- Patch: read_write_misclassifications ---
    if "find_read_write_misclassifications" in remaining:
        existing_tool_names = {t.tool_name for t in sample.mock_tools}
        unfixed = []
        for issue in remaining["find_read_write_misclassifications"]:
            m = re.search(r"'([^']+)' cannot respond", issue)
            if not m:
                unfixed.append(issue)
                continue
            target = object_map.get(m.group(1))
            if not target:
                unfixed.append(issue)
                continue

            # Remove write-only language from behavior
            cleaned = re.sub(
                r"\bDo not reply[^.]*\.\s*|\bwrite.only[^.]*\.\s*|\bnever respond[^.]*\.\s*",
                "", target.behavior, flags=re.IGNORECASE,
            ).strip()
            if cleaned != target.behavior:
                target.behavior = cleaned
                changed = True

            # Add _data skill
            skill = f"{target.object_id}_data"
            if skill not in target.skills:
                target.skills.append(skill)
                changed = True

            # Add a basic mock tool so the skill can actually execute
            if skill not in existing_tool_names:
                description = f"Query {target.object_id} for stored data."
                if target.state_description:
                    description += f" {target.state_description}"
                sample.mock_tools.append(MockToolDef(
                    tool_name=skill,
                    description=description.strip(),
                    arguments_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string", "description": "What to look up"}},
                        "required": [],
                    },
                    response_template=(
                        target.state_description
                        or f"No data found for {{query}} in {target.object_id}."
                    ),
                ))
                existing_tool_names.add(skill)
                changed = True

        if unfixed:
            remaining["find_read_write_misclassifications"] = unfixed
        else:
            remaining.pop("find_read_write_misclassifications", None)

    # --- Patch: sequential_confirmation_chains ---
    # "After X confirms" is valid when a mock tool trigger fires a callback.
    # Auto-patch: for each write-service peer that the object waits on, add a
    # trigger to that peer's mock tool so it sends a confirmation back.
    if "find_sequential_confirmation_chains" in remaining:
        from src.data.schema import MockToolTrigger
        CONFIRMATION_RE = re.compile(
            r"\b(after|when|once|following|upon)\b"
            r"(?!\s+(?:a|an|the)\b)"
            r"(?!\s+\w+ing\b)"
            r".{0,40}"
            r"\b(confirm(?!ation)\w*|responds?\b|acknowledg\w*|repl(?:ies|ied|y\b)|complet(?:es?|ed)\b|send.?back\b|'s\s+(?:reply|response))",
            re.IGNORECASE,
        )
        existing_tool_names = {t.tool_name for t in sample.mock_tools}
        for issue in remaining["find_sequential_confirmation_chains"]:
            # Extract the object with the problematic behavior
            m = re.search(r"Object '([^']+)' behavior contains", issue)
            if not m:
                continue
            waiter_id = m.group(1)
            waiter = object_map.get(waiter_id)
            if not waiter:
                continue
            # For each peer of the waiter, add a trigger if one doesn't exist
            for peer in waiter.peers:
                peer_obj = object_map.get(peer.object_id)
                if not peer_obj:
                    continue
                # Find or create a mock tool for this peer
                peer_tool = next(
                    (t for t in sample.mock_tools if peer.object_id in t.tool_name),
                    None,
                )
                if peer_tool is None:
                    # Create a basic write mock tool for this peer
                    tool_name = f"{peer.object_id}_write"
                    if tool_name not in existing_tool_names:
                        from src.data.schema import MockToolDef
                        peer_tool = MockToolDef(
                            tool_name=tool_name,
                            description=f"Write or notify {peer.object_id}.",
                            arguments_schema={"type": "object", "properties": {}, "required": []},
                            response_template=f"Delivered to {peer.object_id}.",
                        )
                        sample.mock_tools.append(peer_tool)
                        existing_tool_names.add(tool_name)
                        changed = True
                # Add a trigger that fires a confirmation back to the waiter
                already_triggers_waiter = any(
                    t.target_object_id == waiter_id for t in peer_tool.triggers
                )
                if not already_triggers_waiter:
                    peer_tool.triggers.append(MockToolTrigger(
                        target_object_id=waiter_id,
                        message_template=f"Confirmed: action completed by {peer.object_id}.",
                        source=peer.object_id,
                    ))
                    changed = True
        remaining.pop("find_sequential_confirmation_chains")

    # --- Patch: unreachable_objects (wire missing peer links) ---
    # Pattern: object A has no peers (dead-end) and object B is unreachable.
    # If A's behavior text mentions B's object_id, A should forward to B.
    # This catches the common data-gen mistake where an intermediate processor
    # forgets to declare its downstream write/notify service as a peer.
    if "find_unreachable_objects" in remaining:
        from src.data.schema import PeerDecl
        from collections import deque

        def _reachable(objs):
            entries = {o.object_id for o in objs if o.event_sources}
            peer_map = {o.object_id: {p.object_id for p in o.peers} for o in objs}
            visited, q = set(entries), deque(entries)
            while q:
                n = q.popleft()
                for nb in peer_map.get(n, set()):
                    if nb not in visited:
                        visited.add(nb); q.append(nb)
            return visited

        fixed_unreachable = []
        for issue in remaining["find_unreachable_objects"]:
            m = re.search(r"Object '([^']+)' is not reachable", issue)
            if not m:
                fixed_unreachable.append(issue)
                continue
            unreachable_id = m.group(1)

            # Find dead-end objects whose behavior text mentions the unreachable id
            dead_ends = [
                o for o in sample.objects
                if not o.peers and not o.event_sources
                and unreachable_id.replace("-", "[ -]") and
                re.search(re.escape(unreachable_id), o.behavior, re.IGNORECASE)
            ]
            if not dead_ends:
                fixed_unreachable.append(issue)
                continue

            # Wire the first matching dead-end → unreachable object
            donor = dead_ends[0]
            unreachable_obj = object_map.get(unreachable_id)
            if not unreachable_obj:
                fixed_unreachable.append(issue)
                continue

            donor.peers.append(PeerDecl(
                object_id=unreachable_id,
                relationship=f"Forward processed output to {unreachable_id}",
            ))
            changed = True

            # Check if this resolved the unreachability
            if unreachable_id in _reachable(sample.objects):
                pass  # fixed — don't re-add to fixed_unreachable
            else:
                fixed_unreachable.append(issue)

        if fixed_unreachable:
            remaining["find_unreachable_objects"] = fixed_unreachable
        else:
            remaining.pop("find_unreachable_objects")

    # --- Patch: peer_graph_cycles (remove back-edges into entry points) ---
    # Strategy: for each cycle A → B → … → A, identify edges that point back
    # to an entry point (has event_sources). Remove those edges from the
    # non-entry-point objects. This breaks the cycle with minimal disruption:
    # downstream workers can still send outward, just not back to the trigger.
    if "find_peer_graph_cycles" in remaining:
        entry_ids = {o.object_id for o in sample.objects if o.event_sources}
        unfixed_cycles = []
        for issue in remaining["find_peer_graph_cycles"]:
            # Parse "Peer graph cycle detected: A → B → A — ..."
            m = re.search(r"Peer graph cycle detected: ([\w →-]+?) —", issue)
            if not m:
                unfixed_cycles.append(issue)
                continue
            # Nodes in cycle (first and last are the same repeated entry)
            nodes = [n.strip() for n in re.split(r"\s*→\s*", m.group(1).strip())]
            # Find back-edges: edge X → Y where Y is an entry point
            fixed = False
            for i in range(len(nodes) - 1):
                src, dst = nodes[i], nodes[i + 1]
                if dst in entry_ids:
                    src_obj = object_map.get(src)
                    if src_obj:
                        before = len(src_obj.peers)
                        src_obj.peers = [p for p in src_obj.peers if p.object_id != dst]
                        if len(src_obj.peers) != before:
                            changed = True
                            fixed = True
            if not fixed:
                unfixed_cycles.append(issue)
        if unfixed_cycles:
            remaining["find_peer_graph_cycles"] = unfixed_cycles
        else:
            remaining.pop("find_peer_graph_cycles")

    # --- Patch: invalid_peer_declarations (remove dangling refs) ---
    if "find_invalid_peer_declarations" in remaining:
        existing_ids = {o.object_id for o in sample.objects}
        for obj in sample.objects:
            before = len(obj.peers)
            obj.peers = [p for p in obj.peers if p.object_id in existing_ids]
            if len(obj.peers) != before:
                changed = True
        remaining.pop("find_invalid_peer_declarations")  # re-checked after save

    return changed, remaining


def _edit_sample_interactive(sample, workflows_path: Path, sample_map: dict) -> dict:
    """
    Open the sample JSON in $EDITOR, reload on save, re-validate.
    Returns the (possibly empty) blocking issues dict after the edit.
    """
    from src.data.schema import Workflow

    raw = json.loads(sample.model_dump_json())
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix=f"{sample.id}_", delete=False
    ) as f:
        json.dump(raw, f, indent=2)
        tmp = f.name

    editor = os.environ.get("EDITOR", "vim")
    print(f"  Opening {tmp} in {editor} — save and quit when done.")
    os.system(f"{editor} {tmp}")

    try:
        with open(tmp) as f:
            edited = json.load(f)
        updated = Workflow(**edited)
    except Exception as e:
        print(f"  [edit] Could not reload sample: {e} — keeping original.")
        return _blocking_issues(validate_sample(sample))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    # Persist the edit and reload
    sample_map[updated.id] = updated
    all_samples = [sample_map.get(s.id, s) for s in load_jsonl(workflows_path, Workflow)]
    _save_samples(workflows_path, all_samples)

    return _blocking_issues(validate_sample(updated))


def _validate_and_repair_samples(
    workflows_path: Path,
    stage1_args: argparse.Namespace,
    max_fix_attempts: int,
) -> tuple[Path, set[str]]:
    """
    Validate Stage-1 samples using a three-tier repair strategy:

    1. Auto-patch — deterministic fixes (add _data skill, remove dangling peers).
       No LLM cost.  Applied silently.
    2. Retry generation — re-run Stage 1 for the sample (uses LLM).  Up to
       max_fix_attempts times per sample.
    3. Human decision — per-sample prompt: [r]etry · [k]eep flagged · [s]kip.
       Flagged samples proceed to Stage 2 but are marked for follow-up.
       Nothing is dropped without explicit human [s]kip.

    Returns (workflows_path, modified_sample_ids).
    modified_sample_ids: IDs of samples that were changed — their existing test
    cases must be invalidated so Stage 2 regenerates them from the fixed data.
    """
    from src.data.schema import Workflow

    print()
    print("  [validate] Checking Stage 1 samples...")

    modified_ids: set[str] = set()  # samples changed by any repair — their TCs must be regenerated

    try:
        samples = load_jsonl(workflows_path, Workflow)
    except Exception as e:
        print(f"  [validation skipped] Could not load samples: {e}")
        return workflows_path, modified_ids

    all_issues = {s.id: validate_sample(s) for s in samples}
    blocking = {sid: _blocking_issues(v) for sid, v in all_issues.items() if _blocking_issues(v)}
    warnings = {sid: _warning_issues(v) for sid, v in all_issues.items() if _warning_issues(v)}

    if not blocking and not warnings:
        print(f"  [validate] All {len(samples)} sample(s) passed.")
        return workflows_path, modified_ids

    if warnings:
        w_total = sum(len(msgs) for vd in warnings.values() for msgs in vd.values())
        print(f"  [validate] {w_total} warning(s) in {len(warnings)} sample(s) — non-blocking, continuing:")
        for sid, vd in warnings.items():
            for vname, msgs in vd.items():
                print(f"    [{sid}] {vname.replace('find_', '')}: {msgs[0]}")

    # ── Warning auto-patch (sequential_confirmation_chains) ───────────────────
    # Reclassified as WARNING but still needs a deterministic fix: add mock tool
    # triggers so the confirmation callback is actually delivered at eval time.
    PATCHABLE_WARNINGS = {"find_sequential_confirmation_chains", "find_peer_graph_cycles"}
    warning_patchable = {
        sid: {k: v for k, v in vd.items() if k in PATCHABLE_WARNINGS}
        for sid, vd in warnings.items()
        if any(k in PATCHABLE_WARNINGS for k in vd)
    }
    if warning_patchable:
        sample_map = {s.id: s for s in samples}
        warn_patched = set()
        for sid, issues in warning_patchable.items():
            sample = sample_map[sid]
            changed, _ = _autopatch_sample(sample, issues)
            if changed:
                warn_patched.add(sid)
        if warn_patched:
            _save_samples(workflows_path, samples)
            modified_ids |= warn_patched
            print(f"  [auto-patch] Fixed warning (sequential_confirmation_chains) in {len(warn_patched)}: {', '.join(sorted(warn_patched))}")

    if not blocking:
        return workflows_path, modified_ids

    print(f"  [validate] {len(blocking)} sample(s) have blocking issues.")

    # ── Tier 1: Auto-patch ────────────────────────────────────────────────────
    sample_map = {s.id: s for s in samples}
    patched_ids = set()

    for sid, issues in blocking.items():
        sample = sample_map[sid]
        changed, _ = _autopatch_sample(sample, issues)
        if changed:
            patched_ids.add(sid)

    if patched_ids:
        _save_samples(workflows_path, samples)
        # Re-validate patched samples
        samples = load_jsonl(workflows_path, Workflow)
        sample_map = {s.id: s for s in samples}
        repaired = {sid: _blocking_issues(validate_sample(sample_map[sid])) for sid in patched_ids}
        fixed = {sid for sid, b in repaired.items() if not b}
        still_broken = {sid: b for sid, b in repaired.items() if b}
        if fixed:
            print(f"  [auto-patch] Fixed {len(fixed)}: {', '.join(sorted(fixed))}")
            modified_ids |= fixed
        modified_ids |= patched_ids  # patched but not fully fixed still need TC regeneration
        # Update blocking: remove fixed, update still-broken
        for sid in fixed:
            blocking.pop(sid)
        for sid, b in still_broken.items():
            blocking[sid] = b

    if not blocking:
        print(f"  [validate] All blocking issues resolved via auto-patch.")
        return workflows_path, modified_ids

    # ── Tier 2+3: Per-sample human decision ──────────────────────────────────
    print(f"\n  {len(blocking)} sample(s) still have blocking issues after auto-patch.")
    print("  [e]dit · [r]etry · [k]eep flagged · [s]kip · [n]ext · [p]rev\n")

    items = list(blocking.items())           # ordered list of (sid, issues)
    decisions: dict[str, tuple] = {}         # sid → (choice, current_issues)
    i = 0

    while True:
        # If we've passed the end, wrap up any undecided samples
        undecided = [j for j, (sid, _) in enumerate(items) if sid not in decisions]
        if i >= len(items):
            if not undecided:
                break
            print(f"\n  {len(undecided)} sample(s) still undecided — cycling back.")
            i = undecided[0]

        sid, issues = items[i]
        current_issues = decisions.get(sid, (None, issues))[1]
        prev_choice = decisions.get(sid, (None,))[0]

        total = len(items)
        decided = len(decisions)
        status = f"{decided}/{total} decided"
        marker = f"  ┌─ [{i+1}/{total}] [{sid}]"
        if prev_choice:
            marker += f"  (was: {prev_choice})"
        print(f"{marker} {status} {'─' * max(0, 55 - len(marker))}")
        for vname, msgs in current_issues.items():
            print(f"  │  {vname.replace('find_', '')}: {msgs[0]}")
        print(f"  └{'─' * 58}")

        choice = _ask("Action?", ("e", "r", "k", "s", "n", "p"), default="n")

        if choice == "p":
            i = max(0, i - 1)
            continue
        if choice == "n":
            i += 1
            continue
        if choice == "e":
            current_issues = _edit_sample_interactive(sample_map[sid], workflows_path, sample_map)
            modified_ids.add(sid)  # edited regardless of outcome
            if not current_issues:
                print(f"  [edit] Fixed — no more blocking issues.")
                decisions[sid] = ("fixed", {})
                i += 1
                continue
            print(f"  [edit] Still has blocking issues.")
            decisions[sid] = ("e", current_issues)
            continue

        decisions[sid] = (choice, current_issues)
        i += 1

    retry_queue: set[str] = set()
    keep_flagged: dict[str, dict] = {}
    skip_ids: set[str] = set()

    for sid, (choice, current_issues) in decisions.items():
        if choice == "fixed" or not current_issues:
            continue
        if choice == "r":
            retry_queue.add(sid)
        elif choice == "s":
            skip_ids.add(sid)
        else:
            keep_flagged[sid] = current_issues

    # Workflows with no decision default to keep-flagged
    for sid, issues in blocking.items():
        if sid not in decisions:
            keep_flagged[sid] = issues

    # ── Retry loop ────────────────────────────────────────────────────────────
    for attempt in range(1, max_fix_attempts + 1):
        if not retry_queue:
            break
        ids = list(retry_queue)
        print(f"\n  [retry {attempt}/{max_fix_attempts}] Regenerating {len(ids)} sample(s): {', '.join(ids)}")
        fix_args = argparse.Namespace(**vars(stage1_args))
        fix_args.ids = ids
        fix_args.force = True
        try:
            generate_workflows.run(fix_args)
        except Exception as e:
            print(f"  [retry] Generation failed: {e}")
            break

        modified_ids |= set(ids)  # retried samples always need TC regeneration
        samples = load_jsonl(workflows_path, Workflow)
        sample_map = {s.id: s for s in samples}
        still = {sid: _blocking_issues(validate_sample(sample_map[sid])) for sid in ids}
        fixed = {sid for sid, b in still.items() if not b}
        if fixed:
            print(f"  [retry] Fixed: {', '.join(sorted(fixed))}")
            retry_queue -= fixed

        # For samples still broken after this attempt, ask again (no more [r])
        for sid in list(retry_queue):
            current_issues = still[sid]
            while True:
                print(f"\n  [{sid}] still failing after retry {attempt}:")
                for vname, msgs in current_issues.items():
                    print(f"    {vname.replace('find_', '')}: {msgs[0]}")
                choice = _ask("Action?", ("e", "k", "s"), default="k")
                if choice == "e":
                    current_issues = _edit_sample_interactive(sample_map[sid], workflows_path, sample_map)
                    modified_ids.add(sid)
                    if not current_issues:
                        print(f"  [edit] Fixed.")
                        break
                    continue
                break
            retry_queue.discard(sid)
            if not current_issues:
                continue
            if choice == "s":
                skip_ids.add(sid)
            else:
                keep_flagged[sid] = current_issues

    # ── Mark flagged samples ──────────────────────────────────────────────────
    if keep_flagged:
        samples = load_jsonl(workflows_path, Workflow)
        sample_map = {s.id: s for s in samples}
        for sid, issues in keep_flagged.items():
            if sid in sample_map:
                sample_map[sid].flagged = True
                sample_map[sid].flag_reasons = [
                    f"{vname.replace('find_', '')}: {msgs[0]}"
                    for vname, msgs in issues.items()
                ]
        _save_samples(workflows_path, list(sample_map.values()))
        print(f"\n  [validate] {len(keep_flagged)} sample(s) kept as flagged: {', '.join(sorted(keep_flagged))}")

    # ── Drop skipped samples ──────────────────────────────────────────────────
    if skip_ids:
        samples = load_jsonl(workflows_path, Workflow)
        kept = [s for s in samples if s.id not in skip_ids]
        _save_samples(workflows_path, kept)
        print(f"  [validate] Dropped {len(skip_ids)} sample(s): {', '.join(sorted(skip_ids))}")

    total = len(load_jsonl(workflows_path, Workflow))
    print(f"  [validate] {total} sample(s) proceeding to Stage 2.")
    return workflows_path, modified_ids


def _validate_test_cases_final(samples_path: Path) -> None:
    """
    Validate fully-formed test cases after Stage 3.
    Blocking issues drop the test case from the file (logged).
    Warning issues are printed but the test case is kept.
    """
    from src.data.schema import Sample

    print()
    print("  [validate] Checking Stage 3 test cases...")

    try:
        test_cases = load_jsonl(samples_path, Sample)
    except Exception as e:
        print(f"  [validation skipped] Could not load test cases: {e}")
        return

    all_issues = {tc.id: validate_test_case(tc) for tc in test_cases}
    blocking_tcs = {tid: _blocking_issues(v) for tid, v in all_issues.items() if _blocking_issues(v)}
    warning_tcs  = {tid: _warning_issues(v)  for tid, v in all_issues.items() if _warning_issues(v)}

    if not blocking_tcs and not warning_tcs:
        print(f"  [validate] All checks passed ({len(test_cases)} test case(s)).")
        return

    if warning_tcs:
        w_total = sum(len(v) for vd in warning_tcs.values() for v in vd.values())
        print(f"  [validate] {w_total} warning(s) across {len(warning_tcs)} test case(s):")
        for tid, vd in warning_tcs.items():
            for vname, msgs in vd.items():
                print(f"    [{tid}] {vname.replace('find_', '')}: {msgs[0]}")

    if blocking_tcs:
        print(f"  [validate] Dropping {len(blocking_tcs)} test case(s) with blocking issues:")
        for tid, vd in blocking_tcs.items():
            for vname, msgs in vd.items():
                print(f"    [DROPPED] [{tid}] {vname.replace('find_', '')}: {msgs[0]}")
        kept = [tc for tc in test_cases if tc.id not in blocking_tcs]
        with open(samples_path, "w") as f:
            for tc in kept:
                f.write(tc.model_dump_json() + "\n")
        print(f"  [validate] {len(kept)}/{len(test_cases)} test case(s) kept.")


def main():
    parser = argparse.ArgumentParser(
        description="Run the full data generation pipeline (samples → test cases)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full pipeline into a target folder
  python -m src.data.pipeline -i data/zapier/raw/templates.yaml --target-dir outputs/my-run

  # Continue an existing run (stage 1 is skipped if workflows.jsonl already exists)
  python -m src.data.pipeline -i data/zapier/raw/templates.yaml --target-dir outputs/my-run

  # Skip stage 1 with a specific samples file (no target-dir)
  python -m src.data.pipeline --workflows outputs/data/zapier/templates_samples_object.jsonl

  # Full pipeline with custom options
  python -m src.data.pipeline -i data/zapier/raw/templates.yaml --target-dir outputs/my-run \\
      --workflows-per-template 3 --scenario-count 2 --mod-type temporal --ambiguity precise
""",
    )

    # --- Output targeting ---
    parser.add_argument(
        "--target-dir", "-t",
        type=Path,
        default=None,
        help=(
            "Directory for all pipeline outputs. Stage 1 writes workflows.jsonl here; "
            "stage 2 writes workflows-mods.jsonl here. If workflows.jsonl already exists, "
            "stage 1 is skipped automatically (continuation)."
        ),
    )

    # --- Stage selection ---
    parser.add_argument(
        "--workflows",
        type=Path,
        default=None,
        help="Skip stage 1 and use this specific samples JSONL file as input to stage 2",
    )

    # --- Stage 1 args ---
    stage1 = parser.add_argument_group("Stage 1: Generate Workflows")
    stage1.add_argument(
        "--input", "-i",
        type=Path,
        default=None,
        help="Path to raw templates YAML file (required for stage 1)",
    )
    stage1.add_argument(
        "--workflows-per-template",
        type=int,
        default=1,
        help="Number of samples per template (default: 1)",
    )
    stage1.add_argument(
        "--id",
        dest="ids",
        metavar="ID",
        action="append",
        default=None,
        help="Only process template(s) with this ID (repeatable: --id foo --id bar)",
    )

    # --- Stage 2 args ---
    stage2 = parser.add_argument_group("Stage 2: Generate Test Cases")
    stage2.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Output path for test cases JSONL (default: derived from samples path or target-dir)",
    )
    stage2.add_argument(
        "--scenario-count",
        type=int,
        default=1,
        help="Scenarios per modification type (default: 1)",
    )
    stage2.add_argument(
        "--events-before",
        type=int,
        default=1,
        help="Events before modification (default: 1)",
    )
    stage2.add_argument(
        "--events-after",
        type=int,
        default=2,
        help="Events after modification (default: 2)",
    )
    stage2.add_argument(
        "--events-unrelated",
        type=int,
        default=1,
        help="Events unaffected by modification (default: 1)",
    )
    stage2.add_argument(
        "--mod-type",
        type=str,
        choices=list(generate_samples.MODIFICATION_TYPES.keys()),
        default=None,
        help="Modification type (default: all types)",
    )
    stage2.add_argument(
        "--mods-per-scenario",
        type=int,
        default=1,
        help="Modifications per scenario (default: 1)",
    )
    stage2.add_argument(
        "--ambiguity",
        type=str,
        choices=list(generate_samples.AMBIGUITY_DESCRIPTIONS.keys()),
        default="random",
        help="Ambiguity level (default: random)",
    )
    stage2.add_argument(
        "--samples-prompt-template",
        type=Path,
        default=Path("config/prompts/data-gen/generate_samples.yaml"),
        help="Prompt template for stage 2",
    )

    # --- Shared args ---
    shared = parser.add_argument_group("Shared")
    shared.add_argument(
        "--provider", "-p",
        choices=["openai", "anthropic", "google"],
        default=None,
        help="LLM provider (inferred from model if not specified)",
    )
    shared.add_argument(
        "--model", "-m",
        default="claude-sonnet-4-6",
        help="Model name (default: claude-sonnet-4-6)",
    )
    shared.add_argument(
        "--seed", "-s",
        type=int,
        default=None,
        help="Random seed",
    )
    shared.add_argument(
        "--temperature",
        type=float,
        default=0.7,
        help="LLM temperature (default: 0.7)",
    )
    shared.add_argument(
        "--force",
        action="store_true",
        help="Regenerate all items (both stages)",
    )
    shared.add_argument(
        "--limit", "-n",
        type=int,
        default=None,
        help="Process only the first N items",
    )
    shared.add_argument(
        "--workers", "-w",
        type=int,
        default=1,
        help="Parallel workers per stage (default: 1)",
    )
    shared.add_argument(
        "--max-fix-attempts",
        type=int,
        default=2,
        help="Max auto-regeneration attempts per sample before human review (default: 2)",
    )
    shared.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip all validation (useful when iterating on known-bad samples)",
    )
    shared.add_argument(
        "--no-validate-workflow-steps",
        action="store_true",
        help="Skip Stage 1d (LLM-judge grading of grounded workflow steps vs templates.yaml raw_steps)",
    )
    shared.add_argument(
        "--workflow-step-judge-model",
        default="gpt-5.4",
        help="Judge model for Stage 1d workflow-step validation (default: gpt-5.4)",
    )
    shared.add_argument(
        "--workflow-step-judge-provider",
        default="azure",
        help="Judge provider for Stage 1d (default: azure)",
    )
    shared.add_argument(
        "--no-patch-gaps",
        action="store_true",
        help="Skip Stage 3.5 entity-gap patching (useful for debugging or when patching separately)",
    )

    args = parser.parse_args()

    # --- Resolve target dir (auto-timestamp if not specified) ---
    if args.target_dir is None and args.workflows is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.target_dir = Path("outputs/data/zapier") / timestamp
        print(f"Target directory: {args.target_dir}")

    # --- Resolve samples path and stage 1 continuation ---
    workflows_path: Path | None = None
    skip_stage1 = False

    if args.workflows is not None:
        # Explicit samples file provided — always skip stage 1
        workflows_path = args.workflows
        skip_stage1 = True
        if not workflows_path.exists():
            print(f"Error: Workflows file not found: {workflows_path}", file=sys.stderr)
            sys.exit(1)
    elif args.target_dir is not None:
        # Target dir provided — check for existing samples (continuation)
        workflows_path = args.target_dir / WORKFLOWS_FILENAME
        if workflows_path.exists() and not args.force and not args.ids:
            skip_stage1 = True
            print(f"Found existing samples: {workflows_path} (skipping stage 1)")
        else:
            skip_stage1 = False
    else:
        # No target dir, no explicit samples — derive path from input (original behaviour)
        if args.input is None:
            parser.error(
                "Either --input (for stage 1) or --workflows / --target-dir (to skip stage 1) is required"
            )

    # Validate stage 1 inputs when stage 1 will run
    if not skip_stage1 and args.input is None:
        parser.error("--input is required to run stage 1")

    # Resolve stage 2 output path
    samples_output: Path | None = args.output
    if samples_output is None and args.target_dir is not None:
        samples_output = args.target_dir / WORKFLOWS_MODS_FILENAME

    # --- Stage 1 ---
    print("=" * 60)
    if skip_stage1:
        print("STAGE 1: skipped (using existing samples)")
    else:
        print("STAGE 1: Generate Workflows")
    print("=" * 60)

    stage1_args = argparse.Namespace(
        input=args.input,
        output=workflows_path,
        workflows_per_template=args.workflows_per_template,
        ids=args.ids,
        provider=args.provider,
        model=args.model,
        seed=args.seed,
        temperature=args.temperature,
        force=args.force,
        limit=args.limit,
        workers=args.workers,
    )

    if not skip_stage1:
        workflows_path = generate_workflows.run(stage1_args)

    if not args.no_validate:
        workflows_path, modified_ids = _validate_and_repair_samples(
            workflows_path=workflows_path,
            stage1_args=stage1_args,
            max_fix_attempts=args.max_fix_attempts,
        )
    else:
        modified_ids = set()

    # --- Stage 1d: validate grounded workflow steps vs templates.yaml ---
    if not args.no_validate_workflow_steps and not skip_stage1:
        print()
        print("=" * 60)
        print("STAGE 1d: Validate Workflow Steps vs Templates")
        print("=" * 60)
        from src.data import validate_workflow_steps as _vws
        vws_args = argparse.Namespace(
            workflows=workflows_path,
            templates=args.input,
            provider=args.workflow_step_judge_provider,
            judge_model=args.workflow_step_judge_model,
            workers=args.workers,
            output=None,
            limit=None,
            filter=None,
            no_fail=True,  # don't abort the pipeline; report and continue
        )
        _vws.main_with_args(vws_args)

    # Any sample explicitly regenerated via --id must have its TCs invalidated,
    # even if the validation step made no repairs (Stage 2 would otherwise hit
    # the completion cache and skip regeneration entirely).
    if args.ids and not skip_stage1:
        modified_ids |= set(args.ids)

    if modified_ids and samples_output:
        _invalidate_test_cases(samples_output, modified_ids)

    # --- Stage 2 ---
    print()
    print("=" * 60)
    print("STAGE 2: Generate Test Cases")
    print("=" * 60)

    stage2_args = argparse.Namespace(
        input=workflows_path,
        output=samples_output,  # None → derived by generate_samples.run()
        prompt_template=args.test_cases_prompt_template,
        scenario_count=args.scenario_count,
        events_before=args.events_before,
        events_after=args.events_after,
        events_unrelated=args.events_unrelated,
        events_inter_mod=1,
        concurrent_events=0,
        mod_type=args.mod_type,
        mods_per_scenario=args.mods_per_scenario,
        ambiguity=args.ambiguity,
        ids=args.ids,
        provider=args.provider,
        model=args.model,
        seed=args.seed,
        temperature=args.temperature,
        force=args.force,
        limit=args.limit,
        workers=args.workers,
    )
    samples_path = generate_samples.run(stage2_args)

    # --- Stage 3 ---
    print()
    print("=" * 60)
    print("STAGE 3: Generate Seed Data + Expectations")
    print("=" * 60)

    stage3_args = argparse.Namespace(
        input=samples_path,
        samples=workflows_path,
        output=None,  # in-place by default
        provider=args.provider,
        model=args.model,
        seed=args.seed,
        temperature=args.temperature,
        force=args.force,
        limit=args.limit,
        workers=args.workers,
    )
    output_path = generate_seed.run(stage3_args)

    # --- Stage 3.5 ---
    print()
    print("=" * 60)
    print("STAGE 3.5: Patch Event-Entity Mock Gaps")
    print("=" * 60)
    if not args.no_patch_gaps:
        from src.data.patch_event_entities import patch_entity_gaps
        from src.data.schema import Sample
        from src.data.utils import load_jsonl
        from src.data.llm import create_llm as _create_llm
        _patch_llm = _create_llm(args.provider or "anthropic", args.model)
        _tcs = load_jsonl(output_path, Sample)
        _patched = patch_entity_gaps(_patch_llm, _tcs, workers=args.workers)
        with open(output_path, "w") as _f:
            for _tc in _patched:
                _f.write(_tc.model_dump_json() + "\n")
        print(f"  Entity gap patching complete → {output_path}")
    else:
        print("  Skipped (--no-patch-gaps)")

    if not args.no_validate:
        _validate_test_cases_final(output_path)

    print()
    print("=" * 60)
    print(f"Pipeline complete. Test cases: {output_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
