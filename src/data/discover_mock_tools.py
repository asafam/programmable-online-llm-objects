"""
Discovery pass: run each sample through the LNL runtime to find which _data tools
are actually called but not yet mocked (falling through to PassthroughExecutor),
then generate mock data for those tools and patch the workflows-mods.jsonl.

Complements retrofit_mock_tools.py (static analysis) with dynamic discovery:
  - retrofit: infers needed tools from object descriptions  (no LNL runtime)
  - discover:  observes actual tool calls at runtime        (requires LNL brain)

Run retrofit first, then discover to catch anything it missed.

Usage:
    python -m src.data.discover_mock_tools \\
        -i outputs/data/zapier/20260405_002306/samples.jsonl \\
        --model gpt-4o

    # Preview discovered tools without patching:
    python -m src.data.discover_mock_tools \\
        -i outputs/data/zapier/20260405_002306/samples.jsonl \\
        --model gpt-4o --dry-run

    # Also patch workflows.jsonl:
    python -m src.data.discover_mock_tools \\
        -i outputs/data/zapier/20260405_002306/samples.jsonl \\
        --workflows outputs/data/zapier/20260405_002306/workflows.jsonl \\
        --model gpt-4o
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

from src.data.generate_workflows import _generate_mock_tool_data
from src.data.evaluate import _run_with_timeout, merge_mock_tools
from src.data.llm import create_llm
from src.data.schema import MockToolDef, Sample
from src.data.utils import add_common_args, infer_provider, load_jsonl, print_run_info
from src.data.evaluate import to_lnl_definition, _build_trigger_map


# ── No-op judge — skips LLM judge calls during discovery ─────────────────────

class _NoOpJudge:
    def evaluate_assertion(self, condition: str, evidence: str, prior_context: str = "") -> tuple[bool, str]:
        return True, "[discovery] skipped"


class _NoOpHarness:
    judge = _NoOpJudge()

    def evaluate_assertion(self, condition: str, evidence: str, prior_context: str = "") -> tuple[bool, str]:
        return True, "[discovery] skipped"


# ── Discovery execution ───────────────────────────────────────────────────────

def _discover_tool_calls(
    tc: Sample,
    brain,
    timeout_s: float = 60.0,
    max_chain_depth: int = 20,
) -> list[str]:
    """Run TC steps through the runtime and return all _data tool names that
    fell through to PassthroughExecutor (i.e., were called but not mocked).

    Always enables PassthroughExecutor regardless of whether mock_tools is
    populated, so every unmocked tool call is captured.
    """
    from src.lnl.gateway import EventGateway
    from src.lnl.runtime import Runtime
    from src.lnl.tools import CodeExecutor, MockInProcessExecutor, PassthroughExecutor, ToolRegistry
    import time

    passthrough = PassthroughExecutor()
    tool_registry = ToolRegistry()
    tool_registry.register("execute_code", CodeExecutor())
    tool_registry.register_fallback(passthrough)

    # Register existing mock tools so they respond correctly and don't appear
    # as false positives in the passthrough log.
    for mock_tool in tc.mock_tools:
        executor = MockInProcessExecutor(mock_tool)
        tool_registry.register(mock_tool.tool_name, executor, spec=executor.spec)

    rt = Runtime(
        brain,
        tool_registry=tool_registry,
        max_chain_depth=max_chain_depth,
    )
    gw = EventGateway(rt)

    for obj_def in tc.objects:
        rt.create_object(to_lnl_definition(obj_def))

    try:
        # Run steps only — cheaper and sufficient to surface data lookup patterns.
        for step in tc.steps:
            payload = json.dumps({"system": step.source, "content": step.text})
            _run_with_timeout(
                lambda s=step, p=payload: gw.dispatch(s.target, p, source=s.source),
                timeout_s,
            )
    finally:
        # Signal shutdown before pool cleanup so in-flight chained messages
        # don't try to submit new futures after the pool is gone.
        rt._shutdown.set()
        rt._pool.shutdown(wait=False)

    # Collect _data tool names that hit PassthroughExecutor
    unmocked = {
        entry["tool"]
        for entry in passthrough.call_log
        if entry["tool"].endswith("_data")
    }
    return sorted(unmocked)


# ── Main ──────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    if args.provider is None:
        args.provider = infer_provider(args.model)

    if not args.input.exists():
        print(f"Error: {args.input} not found", file=sys.stderr)
        sys.exit(1)

    print_run_info(args.provider, args.model, getattr(args, "seed", None), {})

    test_cases: list[Sample] = load_jsonl(args.input, Sample)
    print(f"Loaded {len(test_cases)} test cases from {args.input}")

    # Group by sample_id; run one representative TC per sample
    by_sample: dict[str, list[int]] = defaultdict(list)
    for i, tc in enumerate(test_cases):
        key = tc.sample_id or tc.id
        by_sample[key].append(i)
    print(f"Workflows to probe: {len(by_sample)}")

    # Build brain (objects need real LLM); judge is no-op
    def _make_brain(provider, model):
        if provider == "openai":
            from src.lnl.brain import OpenAIBrain
            return OpenAIBrain(model=model)
        else:
            from src.lnl.brain import AnthropicBrain
            return AnthropicBrain(model=model)

    brain = _make_brain(args.provider, args.model)
    llm = create_llm(args.provider, args.model)

    timeout_s: float = getattr(args, "timeout", 60.0)

    # Discover unmocked _data tools per sample group
    new_tools_by_sample: dict[str, list[MockToolDef]] = {}

    with tqdm(total=len(by_sample), unit="sample", desc="Discovering") as pbar:
        for sid, idxs in by_sample.items():
            representative = test_cases[idxs[0]]
            pbar.set_postfix_str(representative.id[:50], refresh=True)

            try:
                unmocked = _discover_tool_calls(
                    representative, brain,
                    timeout_s=timeout_s,
                    max_chain_depth=getattr(args, "max_chain_depth", 20),
                )
            except Exception as e:
                tqdm.write(f"  SKIP {representative.id}: {e}", file=sys.stderr)
                pbar.update(1)
                continue

            if not unmocked:
                pbar.update(1)
                continue

            already_mocked = {t.tool_name for t in representative.mock_tools}
            missing = [t for t in unmocked if t not in already_mocked]

            if not missing:
                pbar.update(1)
                continue

            tqdm.write(f"\n  {representative.id}")
            for tool_name in missing:
                tqdm.write(f"    discovered: {tool_name}")

            if args.dry_run:
                pbar.update(1)
                continue

            # Generate mock data for each newly discovered tool
            step_texts = [s.text for s in representative.steps if s.text]
            tools: list[MockToolDef] = list(representative.mock_tools)
            for tool_name in missing:
                # Derive description from the object whose behavior references this tool
                description = next(
                    (
                        (obj.state_description or obj.role)
                        for obj in representative.objects
                        if tool_name in (obj.behavior or "")
                    ),
                    tool_name.replace("_", " "),
                )
                tool = _generate_mock_tool_data(llm, tool_name, description, step_texts)
                if tool:
                    tools.append(tool)
                    tqdm.write(f"    + generated {tool_name}")

            if tools != list(representative.mock_tools):
                new_tools_by_sample[sid] = tools

            pbar.update(1)

    if args.dry_run:
        print("\nDry run complete — no files written.")
        return

    if not new_tools_by_sample:
        print("No new tools discovered. workflows-mods.jsonl is up to date.")
        return

    # Apply to all TC variants in each sample group
    patched = 0
    for sid, idxs in by_sample.items():
        tools = new_tools_by_sample.get(sid)
        if not tools:
            continue
        for i in idxs:
            test_cases[i] = test_cases[i].model_copy(update={"mock_tools": tools})
            patched += 1

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
        description="Discovery pass: run TCs to find unmocked _data tool calls",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.data.discover_mock_tools -i outputs/.../workflows-mods.jsonl --model gpt-4o
  python -m src.data.discover_mock_tools -i outputs/.../workflows-mods.jsonl --model gpt-4o --dry-run
  python -m src.data.discover_mock_tools -i outputs/.../workflows-mods.jsonl \\
      --workflows outputs/.../workflows.jsonl --model gpt-4o
""",
    )
    parser.add_argument("--input", "-i", type=Path, required=True,
                        help="Path to workflows-mods.jsonl to patch")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Output path (default: overwrites input)")
    parser.add_argument("--workflows", type=Path, default=None,
                        help="Also patch a workflows.jsonl alongside the test cases")
    parser.add_argument("--workflows-output", type=Path, default=None,
                        help="Output path for patched samples (default: overwrites --workflows)")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Discover tools only; do not generate data or write files")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="Per-step timeout in seconds (default: 60)")
    parser.add_argument("--max-chain-depth", type=int, default=20)
    add_common_args(parser)
    return parser


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
