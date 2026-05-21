"""
Retrofit mock tools into existing test cases.

For each sample group (TCs sharing the same sample_id) that has no mock_tools,
uses an LLM to identify what external read-service data lookups are needed, generates
realistic mock data for each, and patches the JSONL in-place.

Mock tools are determined at the sample level (all 6 TC variants share the same objects
and trigger steps), so the LLM is only called once per sample group.

Usage:
    python -m src.data.retrofit_mock_tools \\
        -i outputs/data/zapier/20260405_002306/samples.jsonl \\
        --model gpt-4o

    # Preview identified tools without generating data or writing:
    python -m src.data.retrofit_mock_tools \\
        -i outputs/data/zapier/20260405_002306/samples.jsonl \\
        --model gpt-4o --dry-run

    # Also patch workflows.jsonl alongside samples.jsonl:
    python -m src.data.retrofit_mock_tools \\
        -i outputs/data/zapier/20260405_002306/samples.jsonl \\
        --workflows outputs/data/zapier/20260405_002306/workflows.jsonl \\
        --model gpt-4o
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

from src.data.generate_samples import _generate_mock_tool_data
from src.data.llm import create_llm
from src.data.llm.base import ChatMessage
from src.data.schema import MockToolDef, Sample
from src.data.utils import add_common_args, infer_provider, load_jsonl, print_run_info


# ── Tool identification ────────────────────────────────────────────────────────

_IDENTIFY_PROMPT = """\
You are analyzing a distributed LLM-object automation system to identify what \
external read-only data sources are required but missing from the mock tool configuration.

## Objects in this automation

{OBJECTS}

## External trigger steps

{STEPS}

## Already-defined mock tools

{EXISTING_TOOLS}

## Task

Identify every external READ-ONLY data lookup that any object in this automation must \
perform to complete its task, but that is NOT already covered by the existing mock tools.

Focus on:
- Org charts, reporting chains, approval hierarchies
- Employee/user directories (names, roles, emails, teams)
- Product/service catalogs and pricing tables
- Customer records, account data
- Policy documents, rule tables, SLA definitions
- Any other reference data an object needs to look up by name or ID

Do NOT include:
- Internal computations (formatting, validation, classification) — these are skills
- Transactional data created during the automation run (approvals, tickets, logs)
- Data that arrives in the trigger event payload itself

For each missing data source, return:
- tool_name: snake_case ending in _data (e.g. org_directory_data, product_catalog_data)
- description: one sentence — what reference data this source contains
- used_by: the object_id that needs this lookup

Return ONLY a JSON array. If no tools are missing, return [].
Example: [{{"tool_name": "org_directory_data", "description": "Employee org chart with reporting chains and approval authorities.", "used_by": "approval-policy"}}]
"""


def _identify_missing_tools(
    llm,
    tc: Sample,
) -> list[dict]:
    """Ask LLM what read-service data tools this TC's objects still need."""
    objects_text = "\n".join(
        f"[{obj.object_id}] {obj.role}\n  behavior: {obj.behavior[:300]}"
        for obj in tc.objects
    )
    steps_text = "\n".join(f"  - {s.text}" for s in tc.steps)
    existing = (
        "\n".join(f"  - {t.tool_name}: {t.description}" for t in tc.mock_tools)
        or "  (none)"
    )
    prompt = (
        _IDENTIFY_PROMPT
        .replace("{OBJECTS}", objects_text)
        .replace("{STEPS}", steps_text)
        .replace("{EXISTING_TOOLS}", existing)
    )
    messages = [ChatMessage(role="user", content=prompt)]
    try:
        text = llm.generate_text(messages=messages).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        if isinstance(result, list):
            return [r for r in result if isinstance(r, dict) and "tool_name" in r]
    except Exception:
        pass
    return []


# ── Main ──────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    if args.provider is None:
        args.provider = infer_provider(args.model)

    if not args.input.exists():
        print(f"Error: {args.input} not found", file=sys.stderr)
        sys.exit(1)

    print_run_info(args.provider, args.model, getattr(args, "seed", None), {})

    llm = create_llm(args.provider, args.model)

    test_cases: list[Sample] = load_jsonl(args.input, Sample)
    print(f"Loaded {len(test_cases)} test cases from {args.input}")

    # Group by sample_id (all variants share the same objects + steps structure)
    by_sample: dict[str, list[int]] = defaultdict(list)
    for i, tc in enumerate(test_cases):
        key = tc.sample_id or tc.id
        by_sample[key].append(i)

    needs_work = dict(by_sample)
    print(f"Workflows to check: {len(needs_work)} ({len(test_cases)} TCs)")

    if not needs_work:
        print("All samples already have mock tools. Nothing to do.")
        return

    # Generate mock tools per sample group
    new_tools_by_sample: dict[str, list[MockToolDef]] = {}

    def _process_sample(sid: str, idxs: list[int]) -> tuple[str, list[MockToolDef] | None]:
        representative = test_cases[idxs[0]]
        identified = _identify_missing_tools(llm, representative)
        if args.dry_run:
            if identified:
                tqdm.write(f"\n  {representative.id}")
                for t in identified:
                    tqdm.write(f"    → {t['tool_name']}: {t.get('description', '')[:80]}")
            return sid, None
        step_texts = [s.text for s in representative.steps if s.text]
        tools: list[MockToolDef] = list(representative.mock_tools)
        for entry in identified:
            tool_name = entry.get("tool_name", "").strip()
            description = entry.get("description", "").strip() or entry.get("used_by", "")
            if not tool_name or any(t.tool_name == tool_name for t in tools):
                continue
            tool = _generate_mock_tool_data(llm, tool_name, description, step_texts)
            if tool:
                tools.append(tool)
                tqdm.write(f"  + {tool_name} ({representative.id[:40]})")
        return sid, tools

    workers = getattr(args, "workers", 1)
    with tqdm(total=len(needs_work), unit="sample", desc="Identifying") as pbar:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_process_sample, sid, idxs): sid for sid, idxs in needs_work.items()}
            for fut in as_completed(futures):
                try:
                    sid, tools = fut.result()
                    if tools is not None:
                        new_tools_by_sample[sid] = tools
                except Exception as e:
                    tqdm.write(f"  WARN: failed for {futures[fut]}: {e}", file=sys.stderr)
                pbar.update(1)

    if args.dry_run:
        print("\nDry run complete — no files written.")
        return

    if not new_tools_by_sample:
        print("No tools generated.")
        return

    # Apply to all TC variants in each sample group where new tools were added
    patched = 0
    for sid, idxs in by_sample.items():
        tools = new_tools_by_sample.get(sid)
        if not tools:
            continue
        for i in idxs:
            test_cases[i] = test_cases[i].model_copy(update={"mock_tools": tools})
            patched += 1

    # Write updated samples.jsonl (in-place, same file)
    out_path = args.output or args.input
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        for tc in test_cases:
            f.write(tc.model_dump_json() + "\n")
    print(f"Patched {patched} test cases → {out_path}")

    # Optionally patch workflows.jsonl too
    if args.workflows and args.workflows.exists():
        from src.data.schema import Workflow
        samples: list[Workflow] = load_jsonl(args.workflows, Workflow)
        sample_map = {s.id: i for i, s in enumerate(samples)}
        s_patched = 0
        for sid, tools in new_tools_by_sample.items():
            # sample_id may equal the sample's id
            if sid in sample_map:
                idx = sample_map[sid]
                samples[idx] = samples[idx].model_copy(update={"mock_tools": tools})
                s_patched += 1
        if s_patched:
            samples_out = args.workflows_output or args.workflows
            with open(samples_out, "w") as f:
                for s in samples:
                    f.write(s.model_dump_json() + "\n")
            print(f"Patched {s_patched} samples → {samples_out}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Retrofit mock tools into existing test cases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.data.retrofit_mock_tools -i outputs/.../samples.jsonl --model gpt-4o
  python -m src.data.retrofit_mock_tools -i outputs/.../samples.jsonl --model gpt-4o --dry-run
  python -m src.data.retrofit_mock_tools -i outputs/.../samples.jsonl \\
      --workflows outputs/.../workflows.jsonl --model gpt-4o
""",
    )
    parser.add_argument("--input", "-i", type=Path, required=True,
                        help="Path to samples.jsonl to patch")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Output path (default: overwrites input)")
    parser.add_argument("--workflows", type=Path, default=None,
                        help="Also patch a workflows.jsonl alongside the test cases")
    parser.add_argument("--workflows-output", type=Path, default=None,
                        help="Output path for patched samples (default: overwrites --workflows)")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Identify tools only; do not generate data or write files")
    parser.add_argument("--workers", type=int, default=4,
                        help="Number of parallel workers for sample processing (default: 4)")
    add_common_args(parser)
    return parser


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
