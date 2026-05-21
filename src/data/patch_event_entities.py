"""
Patch event-entity mock gaps in test cases.

After Stage 3 (generate_seed), mock tool response templates are seeded with data
for entities referenced in step texts and event inputs.  In practice the LLM
sometimes misses entities or generates slightly different identifiers, leaving
gaps where an event references EMP-5502 but the tool only has EMP-4821.

At eval time this causes the LLM to query the tool for EMP-5502, receive EMP-4821
data, find nothing relevant, and retry until timeout.

This script detects those gaps with ``find_event_entity_mock_gaps`` (already used
by the validator) and calls an LLM to add the missing records to each tool's
``response_template``.

Usage:
    # Dry-run: report gaps without patching
    python -m src.data.patch_event_entities \\
        -i outputs/data/zapier/my-run/test_cases.jsonl --dry-run

    # Patch in-place
    python -m src.data.patch_event_entities \\
        -i outputs/data/zapier/my-run/test_cases.jsonl --model gpt-4o

    # Write to separate file
    python -m src.data.patch_event_entities \\
        -i outputs/data/zapier/my-run/test_cases.jsonl \\
        -o outputs/data/zapier/my-run/test_cases_patched.jsonl \\
        --model gpt-4o --workers 4
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

from src.data.llm import create_llm
from src.data.llm.base import ChatMessage
from src.data.schema import MockToolDef, Sample
from src.data.utils import add_common_args, infer_provider, load_jsonl, print_run_info
from src.data.validate_test_cases import (
    _COMPANY_NAME_RE,
    _EMAIL_RE,
    _ENTITY_ID_RE,
    find_event_entity_mock_gaps,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_entity_context(tc: Sample, entity: str) -> str:
    """Return the first event input that mentions *entity* (truncated to 500 chars)."""
    for evt in tc.events:
        text = (evt.input or "") + (" " + evt.expect.action if evt.expect else "")
        if entity in text:
            # Return a window of text around the entity
            idx = text.find(entity)
            start = max(0, idx - 80)
            end = min(len(text), idx + 420)
            snippet = text[start:end].strip()
            return snippet
    return entity


def _assign_entities_to_tools(
    tc: Sample, gap_issues: list[str]
) -> dict[str, list[tuple[str, str]]]:
    """Return {tool_name: [(entity, event_context), ...]} by matching entity types to tools.

    Heuristic: an entity goes to the tool whose existing response_template already
    contains entities of the same type (EMP-XXXX → tools with ID patterns, emails →
    tools with email patterns, company names → tools with company patterns).  Falls
    back to the first mock tool when no better match is found.
    """
    if not tc.mock_tools:
        return {}

    # Collect entities from gap issues
    all_gaps_text = " ".join(gap_issues)
    entity_ids: list[str] = []
    emails: list[str] = []
    companies: list[str] = []

    for m in _ENTITY_ID_RE.finditer(all_gaps_text):
        e = m.group()
        if e not in entity_ids:
            entity_ids.append(e)

    for m in _EMAIL_RE.finditer(all_gaps_text):
        e = m.group()
        if e not in emails:
            emails.append(e)

    for m in _COMPANY_NAME_RE.finditer(all_gaps_text):
        e = m.group()
        if e not in companies:
            companies.append(e)

    # Build affinity map: tool_name → (has_ids, has_emails, has_companies)
    tool_affinity: dict[str, tuple[bool, bool, bool]] = {}
    for tool in tc.mock_tools:
        text = tool.response_template
        tool_affinity[tool.tool_name] = (
            bool(_ENTITY_ID_RE.search(text)),
            bool(_EMAIL_RE.search(text)),
            bool(_COMPANY_NAME_RE.search(text)),
        )

    first_tool = tc.mock_tools[0].tool_name

    def _best_tool(entity_type: str) -> str:
        idx = {"entity_id": 0, "email": 1, "company": 2}[entity_type]
        matches = [t for t, aff in tool_affinity.items() if aff[idx]]
        return matches[0] if matches else first_tool

    assignments: dict[str, list[tuple[str, str]]] = defaultdict(list)

    for entity in entity_ids:
        ctx = _extract_entity_context(tc, entity)
        assignments[_best_tool("entity_id")].append((entity, ctx))

    for entity in emails:
        ctx = _extract_entity_context(tc, entity)
        assignments[_best_tool("email")].append((entity, ctx))

    for entity in companies:
        ctx = _extract_entity_context(tc, entity)
        assignments[_best_tool("company")].append((entity, ctx))

    return dict(assignments)


_PATCH_PROMPT = """\
You are generating new records to add to a mock API tool used for automated testing.

Tool: {TOOL_NAME}
Description: {DESCRIPTION}

Schema reference — existing records (use these ONLY to understand the field schema):
{SCHEMA_SAMPLE}

The following entities are referenced in test events but are NOT present in the tool data.
Generate a NEW record for EACH missing entity listed below.

Missing entities and their event context:
{MISSING_ENTITIES}

Instructions:
1. Generate a record for EACH missing entity listed above.
2. Use EXACTLY the identifier shown (e.g. "EMP-5502", "linda.tran@acmecorp.com").
3. Follow the SAME schema as existing records — use identical field names and value formats.
4. Populate fields realistically using the event context provided.
5. Return ONLY the new records, NOT the existing ones.

Return a JSON object in the same format as the existing data but containing ONLY the new records.
Return ONLY the raw JSON — no markdown fences, no explanation.
"""


def _merge_json_records(original: str, new_records: str) -> str:
    """Merge new_records JSON into original JSON.

    Both are expected to be JSON objects or arrays. Handles common patterns:
    - dict with a list value (e.g. {"employees": [...]}): append to the list
    - plain list [...]: extend
    - flat dict of id->record: update with new records
    Returns original unchanged on any parse failure.
    """
    try:
        orig = json.loads(original)
        new = json.loads(new_records)
    except (json.JSONDecodeError, ValueError):
        return original

    if isinstance(orig, list) and isinstance(new, list):
        return json.dumps(orig + new, indent=2)

    if isinstance(orig, dict) and isinstance(new, dict):
        # Check if orig has a single list-valued key (common pattern: {"records": [...]})
        list_keys = [k for k, v in orig.items() if isinstance(v, list)]
        if len(list_keys) == 1 and isinstance(new.get(list_keys[0]), list):
            merged = dict(orig)
            merged[list_keys[0]] = orig[list_keys[0]] + new[list_keys[0]]
            return json.dumps(merged, indent=2)
        # Otherwise treat as flat dict (id -> record mapping) and update
        merged = dict(orig)
        merged.update(new)
        return json.dumps(merged, indent=2)

    return original  # incompatible shapes — keep original


def _patch_tool_with_entities(
    llm,
    tool: MockToolDef,
    entities_with_context: list[tuple[str, str]],
) -> MockToolDef:
    """Ask LLM to generate new records for missing entities and merge into *tool*.

    Sends a schema sample (up to 1500 chars) for context rather than the full
    template, so large templates are never truncated or replaced.
    """
    missing_lines = "\n".join(
        f'  - [{entity}] from event: "{context}"'
        for entity, context in entities_with_context
    )

    # Use a schema sample for context — never replace the full template
    schema_sample = tool.response_template
    if len(schema_sample) > 1500:
        schema_sample = schema_sample[:1500] + "\n... (truncated for brevity)"

    prompt = (
        _PATCH_PROMPT
        .replace("{TOOL_NAME}", tool.tool_name)
        .replace("{DESCRIPTION}", tool.description or "")
        .replace("{SCHEMA_SAMPLE}", schema_sample)
        .replace("{MISSING_ENTITIES}", missing_lines)
    )

    messages = [ChatMessage(role="user", content=prompt)]
    try:
        text = llm.generate_text(messages=messages).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        # Validate new records parse as JSON
        json.loads(text)
        # Merge new records into the ORIGINAL template (never replace it entirely)
        merged = _merge_json_records(tool.response_template, text)
        return tool.model_copy(update={"response_template": merged})
    except Exception:
        return tool  # keep original on failure


# ── Main patch logic ──────────────────────────────────────────────────────────

def patch_entity_gaps(
    llm,
    test_cases: list[Sample],
    workers: int = 1,
    verbose: bool = False,
) -> list[Sample]:
    """Detect entity gaps and patch mock tool response_templates in-place.

    Returns the updated test_cases list (same order, same length).
    TCs with no gaps are returned unchanged.
    """
    # Find TCs with gaps
    tc_gaps: list[tuple[int, Sample, list[str]]] = []
    for i, tc in enumerate(test_cases):
        issues = find_event_entity_mock_gaps(tc)
        if issues:
            tc_gaps.append((i, tc, issues))

    if not tc_gaps:
        tqdm.write("No entity gaps found.")
        return test_cases

    # Build (tc_index, tool_name, entities_with_context) work units
    WorkUnit = tuple[int, str, list[tuple[str, str]]]
    work_units: list[WorkUnit] = []
    for idx, tc, issues in tc_gaps:
        assignments = _assign_entities_to_tools(tc, issues)
        for tool_name, entities in assignments.items():
            work_units.append((idx, tool_name, entities))

    tqdm.write(
        f"Patching {len(tc_gaps)} TCs with gaps "
        f"({len(work_units)} tool-patch operations)"
    )

    # Build a mutable copy of test_cases (shallow; TCs themselves are replaced)
    result: list[Sample] = list(test_cases)

    # Track updated tool maps per TC (may get multiple patch calls for the same TC)
    updated_tools: dict[int, dict[str, MockToolDef]] = defaultdict(dict)
    for idx, tc, _ in tc_gaps:
        for tool in tc.mock_tools:
            updated_tools[idx][tool.tool_name] = tool

    def _do_patch(unit: WorkUnit) -> tuple[int, str, MockToolDef]:
        idx, tool_name, entities = unit
        tc = result[idx]
        tool = next((t for t in tc.mock_tools if t.tool_name == tool_name), None)
        if tool is None:
            return idx, tool_name, None  # type: ignore[return-value]
        patched = _patch_tool_with_entities(llm, tool, entities)
        return idx, tool_name, patched

    with tqdm(total=len(work_units), unit="patch", desc="Patching entity gaps") as pbar:
        if workers == 1:
            for unit in work_units:
                idx, tool_name, patched_tool = _do_patch(unit)
                if patched_tool is not None:
                    updated_tools[idx][tool_name] = patched_tool
                    if verbose:
                        n = len(unit[2])
                        tqdm.write(f"  ✓ {result[idx].id[:60]} / {tool_name} (+{n} entities)")
                pbar.update(1)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_do_patch, unit): unit for unit in work_units}
                for fut in as_completed(futures):
                    try:
                        idx, tool_name, patched_tool = fut.result()
                        if patched_tool is not None:
                            updated_tools[idx][tool_name] = patched_tool
                    except Exception as e:
                        unit = futures[fut]
                        tqdm.write(f"  WARN: patch failed for {result[unit[0]].id}: {e}", file=sys.stderr)
                    pbar.update(1)

    # Apply updated tools back to TCs
    patched_count = 0
    for idx, tool_map in updated_tools.items():
        tc = result[idx]
        new_mock_tools = [tool_map.get(t.tool_name, t) for t in tc.mock_tools]
        result[idx] = tc.model_copy(update={"mock_tools": new_mock_tools})
        patched_count += 1

    tqdm.write(f"Patched {patched_count} test cases.")
    return result


# ── Dry-run report ────────────────────────────────────────────────────────────

def _dry_run_report(test_cases: list[Sample]) -> None:
    """Print a gap analysis report without making any LLM calls."""
    from collections import Counter

    total_gaps = 0
    by_domain: dict[str, int] = Counter()
    by_type: dict[str, int] = Counter()

    for tc in test_cases:
        issues = find_event_entity_mock_gaps(tc)
        if not issues:
            continue
        domain = tc.sample_id or tc.id
        by_domain[domain] += len(issues)
        total_gaps += len(issues)
        for issue in issues:
            if "references entity" in issue:
                by_type["entity_id"] += 1
            elif "references email" in issue:
                by_type["email"] += 1
            elif "references company" in issue:
                by_type["company"] += 1

    tcs_with_gaps = sum(1 for tc in test_cases if find_event_entity_mock_gaps(tc))
    print(f"\nEntity gap report ({len(test_cases)} test cases)")
    print(f"  Total gaps:       {total_gaps}")
    print(f"  TCs with gaps:    {tcs_with_gaps}/{len(test_cases)} ({100*tcs_with_gaps//len(test_cases)}%)")
    print()
    print("By type:")
    for t, cnt in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"  {cnt:5d}  {t}")
    print()
    print("Top domains by gap count:")
    for d, cnt in sorted(by_domain.items(), key=lambda x: -x[1])[:15]:
        print(f"  {cnt:5d}  {d}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Patch event-entity mock gaps in test cases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run: report gaps without patching
  python -m src.data.patch_event_entities -i outputs/.../test_cases.jsonl --dry-run

  # Patch in-place (overwrites input)
  python -m src.data.patch_event_entities -i outputs/.../test_cases.jsonl --model gpt-4o

  # Patch to separate output file
  python -m src.data.patch_event_entities \\
      -i outputs/.../test_cases.jsonl \\
      -o outputs/.../test_cases_patched.jsonl \\
      --model gpt-4o --workers 4
""",
    )
    parser.add_argument("--input", "-i", type=Path, required=True,
                        help="Path to test_cases.jsonl")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Output path (default: overwrites input)")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Report gaps without patching or calling LLM")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel workers (default: 4)")
    parser.add_argument("--verbose", "-v", action="store_true", default=False,
                        help="Print each patched TC")
    parser.add_argument("--id", dest="ids", metavar="ID", action="append", default=None,
                        help="Restrict patching to TCs whose id starts with ID or sample_id matches (repeatable)")
    add_common_args(parser)
    return parser


def run(args: argparse.Namespace) -> None:
    if not args.input.exists():
        print(f"Error: {args.input} not found", file=sys.stderr)
        sys.exit(1)

    test_cases: list[Sample] = load_jsonl(args.input, Sample)
    print(f"Loaded {len(test_cases)} test cases from {args.input}")

    # Filter to specific IDs if requested
    if args.ids:
        filtered = [
            tc for tc in test_cases
            if any(tc.id.startswith(id_) or tc.sample_id == id_ for id_ in args.ids)
        ]
        print(f"Filtered to {len(filtered)}/{len(test_cases)} TCs matching --id filter(s): {args.ids}")
        if not filtered:
            print("No matching TCs found.", file=sys.stderr)
            sys.exit(1)
        # Keep index mapping to reintegrate back
        id_set = {tc.id for tc in filtered}
    else:
        filtered = test_cases
        id_set = None

    if args.dry_run:
        _dry_run_report(filtered)
        return

    if args.provider is None:
        args.provider = infer_provider(args.model)

    print_run_info(args.provider, args.model, getattr(args, "seed", None), {})

    llm = create_llm(args.provider, args.model)

    patched_subset = patch_entity_gaps(
        llm, filtered, workers=args.workers, verbose=args.verbose
    )

    # Merge back: replace only the filtered TCs; keep others unchanged
    if id_set is not None:
        patched_by_id = {tc.id: tc for tc in patched_subset}
        patched = [patched_by_id.get(tc.id, tc) for tc in test_cases]
    else:
        patched = patched_subset

    out_path = args.output or args.input
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        for tc in patched:
            f.write(tc.model_dump_json() + "\n")
    print(f"Written to {out_path}")

    # Show gap count after patching (only for the subset that was processed)
    print("\nPost-patch gap report (filtered subset):")
    _dry_run_report(patched_subset)


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
