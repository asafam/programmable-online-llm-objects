"""Stage 1b validator: grade the object graph of each Workflow.

For each Workflow.objects list, compute deterministic health issues
(structural checks) and call an LLM judge to grade quality (per-object
+ graph-level). Output: one ObjectGraphValidation per workflow as JSONL.

Usage:
    python -m src.data.validate_workflow_objects \\
        --workflows outputs/.../workflows.jsonl \\
        --provider azure --judge-model gpt-5.4 \\
        --workers 4 \\
        --output workflows__object_validation.jsonl
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

from src.data.llm import create_llm
from src.data.schema import (
    ObjectGraphJudgement,
    ObjectGraphValidation,
    ObjectVerdict,
    SingleObjectQuality,
    Workflow,
)
from src.data.utils import generate_with_retries, load_jsonl

load_dotenv()


_PROMPT_PATH = Path(__file__).parent.parent.parent / "config" / "prompts" / "data-gen" / "validate_workflow_objects.yaml"


def _load_prompt() -> str:
    with open(_PROMPT_PATH) as f:
        return yaml.safe_load(f)["prompt"]


def _format_steps(workflow: Workflow) -> str:
    if not workflow.steps:
        return "(no steps)"
    lines = []
    for i, st in enumerate(workflow.steps, 1):
        lines.append(f"  [{i}] target={st.target}  text: {st.text}")
    return "\n".join(lines)


def _format_objects(workflow: Workflow) -> str:
    """Render the workflow's object graph for the prompt."""
    lines = []
    for obj in workflow.objects:
        lines.append(f"### {obj.object_id}")
        lines.append(f"  role: {obj.role}")
        lines.append(f"  behavior: {obj.behavior}")
        if obj.state_description:
            lines.append(f"  state: {obj.state_description}")
        if obj.peers:
            peer_lines = [f"    - {p.object_id}: {p.relationship}" for p in obj.peers]
            lines.append("  peers:")
            lines.extend(peer_lines)
        if obj.skills:
            lines.append(f"  skills: {obj.skills}")
        if obj.subscriptions:
            lines.append(f"  subscriptions: {obj.subscriptions}")
        if obj.event_sources:
            lines.append(f"  event_sources: {obj.event_sources}  (← entry point)")
        lines.append("")
    return "\n".join(lines).rstrip()


def _health_check_object(workflow: Workflow, obj_index: int) -> list[str]:
    """Deterministic per-object structural checks."""
    issues: list[str] = []
    obj = workflow.objects[obj_index]
    obj_ids = {o.object_id for o in workflow.objects}
    step_targets = {st.target for st in workflow.steps}

    if not (obj.object_id or "").strip():
        issues.append("object_id is empty")
    if not (obj.role or "").strip():
        issues.append("role is empty")
    if not (obj.behavior or "").strip():
        issues.append("behavior is empty")
    for p in obj.peers:
        if p.object_id not in obj_ids:
            issues.append(f"peer '{p.object_id}' does not exist in workflow.objects")
        elif p.object_id == obj.object_id:
            issues.append("peer references self (object_id == own object_id)")
    if obj.event_sources and obj.object_id not in step_targets:
        # Entry-point objects should be addressed by at least one step.
        issues.append(
            f"object has event_sources ({obj.event_sources}) but no step targets it"
        )
    return issues


def _aggregate_health(verdicts: list[ObjectVerdict]) -> str:
    return "OK" if all(not v.health_issues for v in verdicts) else "ISSUES"


def _aggregate_quality(verdicts: list[ObjectVerdict], graph_q: str) -> str:
    scores = [v.quality for v in verdicts]
    if graph_q == "POOR" or any(q == "POOR" for q in scores):
        return "POOR"
    if graph_q == "ADEQUATE" or any(q == "ADEQUATE" for q in scores):
        return "ADEQUATE"
    return "GOOD"


def _validate_workflow_objects(
    llm,
    workflow: Workflow,
    prompt_template: str,
) -> ObjectGraphValidation:
    """Run health checks on each object + one LLM call for quality."""
    if not workflow.objects:
        return ObjectGraphValidation(
            workflow_id=workflow.id,
            n_objects=0,
            object_verdicts=[],
            graph_quality="POOR",
            graph_issues=["workflow has zero objects"],
            graph_reasoning="No objects in graph.",
            aggregate_health="ISSUES",
            aggregate_quality="POOR",
        )

    # Deterministic health pass first
    health_lists = [_health_check_object(workflow, i) for i in range(len(workflow.objects))]

    # LLM judge — single call grades all objects + the graph
    prompt = (
        prompt_template
        .replace("{WORKFLOW_ID}", workflow.id)
        .replace("{WORKFLOW_NAME}", workflow.name)
        .replace("{STEPS}", _format_steps(workflow))
        .replace("{OBJECTS}", _format_objects(workflow))
    )
    expected_ids = [o.object_id for o in workflow.objects]
    judgement = generate_with_retries(
        llm=llm,
        prompt=prompt,
        response_model=ObjectGraphJudgement,
        item_id=f"{workflow.id}-objects",
        validator=lambda r: (
            r.graph_quality in ("GOOD", "ADEQUATE", "POOR")
            and {o.object_id for o in r.objects} == set(expected_ids)
        ),
    )

    if judgement is None:
        # Fall back to ADEQUATE/POOR placeholders so health rolls up cleanly.
        object_verdicts = [
            ObjectVerdict(
                workflow_id=workflow.id,
                object_id=obj.object_id,
                role=obj.role,
                is_entry_point=bool(obj.event_sources),
                health_issues=health_lists[i],
                quality="POOR",
                quality_issues=["(judge failed; quality not assessed)"],
            )
            for i, obj in enumerate(workflow.objects)
        ]
        return ObjectGraphValidation(
            workflow_id=workflow.id,
            n_objects=len(workflow.objects),
            object_verdicts=object_verdicts,
            graph_quality="POOR",
            graph_issues=["(judge failed)"],
            graph_reasoning="LLM judge failed to produce a verdict.",
            aggregate_health=_aggregate_health(object_verdicts),
            aggregate_quality="POOR",
        )

    quality_by_id = {sq.object_id: sq for sq in judgement.objects}
    object_verdicts: list[ObjectVerdict] = []
    for i, obj in enumerate(workflow.objects):
        sq = quality_by_id.get(obj.object_id)
        object_verdicts.append(ObjectVerdict(
            workflow_id=workflow.id,
            object_id=obj.object_id,
            role=obj.role,
            is_entry_point=bool(obj.event_sources),
            health_issues=health_lists[i],
            quality=(sq.quality if sq else "POOR"),
            quality_issues=list(sq.quality_issues) if sq else ["(judge did not return verdict for this object)"],
        ))

    return ObjectGraphValidation(
        workflow_id=workflow.id,
        n_objects=len(workflow.objects),
        object_verdicts=object_verdicts,
        graph_quality=judgement.graph_quality,
        graph_issues=list(judgement.graph_issues or []),
        graph_reasoning=judgement.reasoning,
        aggregate_health=_aggregate_health(object_verdicts),
        aggregate_quality=_aggregate_quality(object_verdicts, judgement.graph_quality),
    )


def _print_summary(results: list[ObjectGraphValidation]) -> None:
    from collections import Counter
    health = Counter(r.aggregate_health for r in results)
    quality = Counter(r.aggregate_quality for r in results)
    graph_q = Counter(r.graph_quality for r in results)

    print("\n" + "=" * 70)
    print(f"Object graph validation — {len(results)} workflows")
    print("=" * 70)
    print("Health (deterministic):")
    print(f"  OK:        {health.get('OK', 0):3d}")
    print(f"  ISSUES:    {health.get('ISSUES', 0):3d}")
    print("Graph quality (LLM):")
    print(f"  GOOD:      {graph_q.get('GOOD', 0):3d}")
    print(f"  ADEQUATE:  {graph_q.get('ADEQUATE', 0):3d}")
    print(f"  POOR:      {graph_q.get('POOR', 0):3d}")
    print("Aggregate (per workflow):")
    print(f"  GOOD:      {quality.get('GOOD', 0):3d}")
    print(f"  ADEQUATE:  {quality.get('ADEQUATE', 0):3d}")
    print(f"  POOR:      {quality.get('POOR', 0):3d}")
    print()

    flagged = [
        r for r in results
        if r.aggregate_health == "ISSUES" or r.aggregate_quality == "POOR"
    ]
    if flagged:
        print(f"Flagged for review ({len(flagged)}):")
        for r in flagged:
            n_health = sum(len(v.health_issues) for v in r.object_verdicts)
            n_poor = sum(1 for v in r.object_verdicts if v.quality == "POOR")
            print(
                f"  {r.workflow_id:<55} "
                f"health={r.aggregate_health:<6} graph={r.graph_quality:<8} "
                f"quality={r.aggregate_quality:<8} "
                f"(health_issues={n_health} poor_objects={n_poor}/{r.n_objects})"
            )
        print()


def main_with_args(args: argparse.Namespace) -> int:
    workflows: list[Workflow] = load_jsonl(args.workflows, Workflow)
    prompt_template = _load_prompt()

    if args.filter:
        workflows = [w for w in workflows if w.id in set(args.filter)]
    if args.limit:
        workflows = workflows[: args.limit]

    if not workflows:
        print("No workflows to validate.", file=sys.stderr)
        return 1

    output_path = args.output or args.workflows.with_name(
        args.workflows.stem + "__object_validation.jsonl"
    )

    llm = create_llm(provider=args.provider, model=args.judge_model, temperature=0.0)

    results: list[ObjectGraphValidation] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_validate_workflow_objects, llm, w, prompt_template): w
            for w in workflows
        }
        for fut in as_completed(futures):
            w = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # pragma: no cover
                print(f"  ✗ {w.id}: judge crashed — {exc}", file=sys.stderr)
                continue
            results.append(result)
            marker = {"GOOD": "✓", "ADEQUATE": "·", "POOR": "✗"}.get(result.aggregate_quality, "?")
            print(f"  {marker} {result.workflow_id:<55} {result.aggregate_quality}")

    results.sort(key=lambda r: r.workflow_id)

    with open(output_path, "w") as f:
        for r in results:
            f.write(r.model_dump_json() + "\n")

    _print_summary(results)
    print(f"Wrote {len(results)} validation records to {output_path}")

    if any(r.aggregate_quality == "POOR" for r in results) and not args.no_fail:
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--workflows", required=True, type=Path,
                        help="Path to workflows JSONL (Workflow records).")
    parser.add_argument("--provider", default=os.environ.get("LLM_PROVIDER", "azure"))
    parser.add_argument("--judge-model", default="gpt-5.4")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--filter", nargs="+", default=None)
    parser.add_argument("--no-fail", action="store_true",
                        help="Exit 0 even when POOR workflows are present.")
    args = parser.parse_args()
    sys.exit(main_with_args(args))


if __name__ == "__main__":
    main()
