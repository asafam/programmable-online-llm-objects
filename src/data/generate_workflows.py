"""
Workflow generator for live NL programming.

Generates concrete samples from raw Zapier automation templates using a three-stage
LLM pipeline:
  1. Ground  — replace abstract placeholders with specific concrete values
  2. Objects — design the distributed LLM-object system from the grounded scenario
  3. Steps   — write the external trigger steps

Each stage is a focused LLM call, producing higher-quality output than attempting
all three tasks in a single prompt.

Usage:
    python -m src.data.generate_workflows \\
        --input data/zapier/raw/examples.yaml \\
        --output outputs/data/zapier/generated/workflows.jsonl \\
        --model claude-sonnet-4-6 \\
        --seed 42 \\
        --workflows-per-template 3
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

from src.data.schema import (
    GroundedTemplate,
    MockToolDef,
    ObjectGraph,
    Workflow,
    WorkflowSteps,
)
from src.data.llm import create_llm
from src.lnl.parser import slugify
from src.data.llm.base import ChatMessage
from src.data.utils import (
    infer_provider,
    load_prompt_template,
    load_yaml,
    load_completed_keys,
    generate_with_retries,
    add_common_args,
    validate_paths,
    setup_output,
    print_run_info,
)

# ── Prompt directories ────────────────────────────────────────────────────────

_PROMPTS_DIR = Path("config/prompts/data-gen")
_GROUND_PROMPT = _PROMPTS_DIR / "ground_template.yaml"
_OBJECTS_PROMPT = _PROMPTS_DIR / "identify_objects.yaml"
_STEPS_PROMPT = _PROMPTS_DIR / "write_steps.yaml"


# ── Stage helpers ─────────────────────────────────────────────────────────────

def _format_template(template: dict) -> str:
    steps = "\n".join(f"- {s}" for s in template["raw_steps"])
    return (
        f"ID: {template['id']}\n"
        f"Name: {template['name']}\n"
        f"Domain: {template.get('domain', 'general')}\n"
        f"Source: {template['source_type']}\n"
        f"Seed: {template.get('seed_utterance') or template.get('link', '')}\n\n"
        f"Raw Steps:\n{steps}"
    )


def _format_objects(graph: ObjectGraph) -> str:
    lines = []
    for obj in graph.objects:
        lines.append(f"- {obj.object_id} ({obj.role})")
        lines.append(f"  behavior: {obj.behavior[:200]}")
        if obj.peers:
            peer_ids = ", ".join(p.object_id for p in obj.peers)
            lines.append(f"  peers: {peer_ids}")
        if obj.event_sources:
            lines.append(f"  event_sources: {'; '.join(obj.event_sources)}")
    return "\n".join(lines)


def _ground_template(llm, template: dict, prompt_cfg: dict) -> GroundedTemplate | None:
    """Stage 1a: resolve abstract placeholders into specific concrete values."""
    prompt = (
        prompt_cfg["prompt"]
        .replace("{TEMPLATE}", _format_template(template))
    )
    return generate_with_retries(
        llm=llm,
        prompt=prompt,
        response_model=GroundedTemplate,
        item_id=f"{template['id']}:ground",
        validator=lambda r: bool(r.grounded_steps),
    )


def _identify_objects(llm, grounded: GroundedTemplate, template: dict, prompt_cfg: dict) -> ObjectGraph | None:
    """Stage 1b: design the distributed LLM-object system from the grounded scenario."""
    steps_text = "\n".join(f"- {s}" for s in grounded.grounded_steps)
    prompt = (
        prompt_cfg["prompt"]
        .replace("{NAME}", grounded.name)
        .replace("{DOMAIN}", grounded.domain)
        .replace("{GROUNDED_STEPS}", steps_text)
    )
    def _validate_object_graph(r: ObjectGraph) -> bool:
        if not r.objects:
            raise ValueError("No objects were generated")
        # Every entry-point object (has event_sources) must declare at least one peer.
        # Without a peer, incoming events dead-end and the automation never runs.
        for obj in r.objects:
            if obj.event_sources and not obj.peers:
                raise ValueError(
                    f"Object '{obj.object_id}' has event_sources but no peers — "
                    f"incoming events will dead-end and the automation will never run. "
                    f"Add at least one peer (the business logic object it forwards to)."
                )
        return True

    return generate_with_retries(
        llm=llm,
        prompt=prompt,
        response_model=ObjectGraph,
        item_id=f"{template['id']}:objects",
        validator=_validate_object_graph,
    )


def _write_steps(llm, grounded: GroundedTemplate, graph: ObjectGraph, template: dict, prompt_cfg: dict) -> WorkflowSteps | None:
    """Stage 1c: write the external trigger steps."""
    steps_text = "\n".join(f"- {s}" for s in grounded.grounded_steps)
    prompt = (
        prompt_cfg["prompt"]
        .replace("{NAME}", grounded.name)
        .replace("{GROUNDED_STEPS}", steps_text)
        .replace("{OBJECTS}", _format_objects(graph))
    )
    valid_entry_points = {obj.object_id for obj in graph.objects if obj.event_sources}

    def _validate_steps(r: WorkflowSteps) -> bool:
        if not r.steps:
            raise ValueError("No steps were generated")
        bad = [s.target for s in r.steps if s.target not in valid_entry_points]
        if bad:
            raise ValueError(
                f"Steps target non-entry-point object(s): {bad}. "
                f"Valid entry-points (objects with event_sources) are: {sorted(valid_entry_points)}. "
                f"Steps must only target objects that have event_sources defined."
            )
        return True

    return generate_with_retries(
        llm=llm,
        prompt=prompt,
        response_model=WorkflowSteps,
        item_id=f"{template['id']}:steps",
        validator=_validate_steps,
    )


_KB_TOOL_RE = re.compile(r"(knowledge[_-]?base|faq|kb|article|solution|help[_-]?center)", re.IGNORECASE)


def _infer_data_hint(tool_name: str) -> str:
    """Return a data-type hint based on the tool name so the LLM generates appropriate content."""
    name = tool_name.lower()
    if _KB_TOOL_RE.search(name):
        return (
            "This is a KNOWLEDGE BASE. Generate realistic articles/entries with titles, "
            "problem descriptions, step-by-step solutions, tags, and issue categories. "
            "Do NOT generate employee records — this stores articles, not people."
        )
    if any(k in name for k in ("directory", "org", "team", "roster", "member")):
        return (
            "This is an ORG DIRECTORY. Generate employee records with names, roles, "
            "reporting chains, expertise areas, and availability."
        )
    if any(k in name for k in ("policy", "rule", "approval", "threshold")):
        return (
            "This is a POLICY DATABASE. Generate policy rules with conditions, "
            "thresholds, approval tiers, and authority mappings."
        )
    if any(k in name for k in ("hubspot", "deal", "quote", "crm", "pipeline")):
        return (
            "This is a CRM RECORD STORE (e.g. HubSpot). Generate deal or contact records "
            "with fields: id, status (use 'submitted_for_approval', 'approved', 'rejected'), "
            "owner, amount, stage, and relevant identifiers."
        )
    if any(k in name for k in ("salesforce", "lead", "opportunity", "prospect")):
        return (
            "This is a SALESFORCE-STYLE RECORD STORE. Generate lead or opportunity records "
            "with fields: id, status (use 'new', 'contacted', 'qualified'), source, "
            "assignee, company, and contact details."
        )
    if any(k in name for k in ("zendesk", "ticket", "support", "helpdesk", "jira", "issue")):
        return (
            "This is a SUPPORT TICKET STORE. Generate ticket records with fields: "
            "ticket_id, status (use 'open', 'pending', 'resolved'), priority, "
            "assignee, requester, subject, and created_at."
        )
    if any(k in name for k in ("airtable", "zapier", "google_sheet", "spreadsheet")):
        return (
            "This is a STRUCTURED DATA TABLE (e.g. Airtable, Zapier Tables, Google Sheets). "
            "Generate rows with an id/record_id, status, created_at, and domain-specific "
            "fields inferred from the tool description. Use realistic field names and values."
        )
    if any(k in name for k in ("store", "log", "record", "db", "database", "table", "feedback")):
        return (
            "This is a DATA STORE or LOG. Generate structured records with id, status, "
            "created_at, and domain-specific fields matching the tool description. "
            "Use consistent field names that downstream objects can reference."
        )
    if any(k in name for k in ("slack", "channel", "message", "thread")):
        return (
            "This is a SLACK MESSAGE STORE. Generate message records with fields: "
            "channel, thread_ts, user, text, and timestamp."
        )
    # Generic fallback — let description drive the format
    return (
        "Generate static reference data that matches the tool's purpose as described above. "
        "Use consistent, realistic field names that match how the object's behavior "
        "description refers to the data. "
        "Do NOT include transactional outcomes (approvals, action history) from the automation run."
    )


def _generate_mock_tool_data(llm, tool_name: str, description: str, step_texts: list[str]) -> MockToolDef | None:
    """Generate a mock tool for a read-service object.

    tool_name: the exact tool name (e.g. ``org_directory_data``)
    description: what the service stores (from state_description or role)
    """
    step_context = ""
    if step_texts:
        step_context = (
            "\n\nThe automation references these specific people, items, or identifiers:\n"
            + "\n".join(f"  - {t}" for t in step_texts)
            + "\n\nYour data MUST include entries for every person, item, or entity "
            "mentioned above (using exactly the same names), structured to match the "
            "tool's data type described in the IMPORTANT note below."
        )

    data_hint = _infer_data_hint(tool_name)

    messages = [
        ChatMessage(
            role="user",
            content=(
                f"Generate realistic reference data for a read-service mock API tool.\n\n"
                f"Tool: {tool_name}\n"
                f"What it stores: {description}"
                f"{step_context}\n\n"
                f"IMPORTANT: {data_hint}\n\n"
                "Use field names and value formats that match how the description above "
                "refers to the data — downstream objects will access fields by exact name. "
                "Use realistic identifier formats: Slack user IDs must be 9 alphanumeric "
                "characters (e.g. 'U01ABCDEF', not 'U4821'); ticket IDs should follow "
                "realistic conventions (e.g. 'PROJ-1042', not 'PROJ-42'); KB article IDs "
                "should be realistic (e.g. 'KB-10092', not 'KB-0092'). When a person can "
                "be identified by name, prefer the name over an opaque system ID. "
                "Respond with ONLY a raw JSON object (no markdown, no explanation). "
                "Structure the data logically."
            ),
        )
    ]
    try:
        text = llm.generate_text(messages=messages)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        label = tool_name.replace("_", " ")
        return MockToolDef(
            tool_name=tool_name,
            description=f"Retrieve reference data from {label}. {description}",
            arguments_schema={"type": "object", "additionalProperties": True},
            response_template=json.dumps(data, ensure_ascii=False),
        )
    except Exception:
        return None


def _assemble_sample(template: dict, grounded: GroundedTemplate, graph: ObjectGraph, steps: WorkflowSteps) -> Workflow:
    """Combine stage outputs into a Workflow. Slugify ids. Mock tools generated separately."""
    for obj in graph.objects:
        obj.object_id = slugify(obj.object_id)
        for peer in obj.peers:
            peer.object_id = slugify(peer.object_id)
    for step in steps.steps:
        step.target = slugify(step.target)

    return Workflow(
        id=template["id"],
        name=grounded.name,
        domain=grounded.domain,
        source_type=template["source_type"],
        link=template.get("link") or template.get("seed_utterance", ""),
        raw_steps=template["raw_steps"],
        objects=graph.objects,
        steps=steps.steps,
    )


_DATA_TOOL_RE = re.compile(r"call the `([a-z][a-z0-9_]*_data)` tool", re.IGNORECASE)


def _add_mock_tools(llm, sample: Workflow) -> None:
    """Post-process: generate mock tools for read-service objects.

    Mutates sample.mock_tools (adds the tool def) AND obj.skills (adds the
    tool name so the LNL runtime exposes it to the LLM).

    Read services are detected by the mandatory behavior phrase:
      "call the `{object_id}_data` tool to retrieve data"
    This is more reliable than checking state_description, which is intentionally
    empty for read services (they hold no mutable state of their own).
    """
    step_texts = [s.text for s in sample.steps if s.text]
    existing_tool_names = {t.tool_name for t in sample.mock_tools}
    for obj in sample.objects:
        match = _DATA_TOOL_RE.search(obj.behavior or "")
        if not match:
            continue
        tool_name = match.group(1)  # e.g. "org_directory_data"
        # Ensure the skill is declared on the object so the runtime exposes
        # the tool to the LLM. Without this, the object's behavior says to
        # call the tool but the LLM doesn't actually have access to it.
        if tool_name not in obj.skills:
            obj.skills.append(tool_name)
        if tool_name in existing_tool_names:
            continue  # already generated for this sample
        # Use state_description if present, otherwise fall back to role
        description = (obj.state_description or "").strip() or obj.role
        tool = _generate_mock_tool_data(llm, tool_name, description, step_texts)
        if tool:
            sample.mock_tools.append(tool)
            existing_tool_names.add(tool_name)


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate samples from raw Zapier automation templates",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.data.generate_workflows -i data/zapier/raw/examples.yaml
  python -m src.data.generate_workflows -i data/zapier/raw/examples.yaml --model gpt-4o
  python -m src.data.generate_workflows -i data/zapier/raw/examples.yaml --workflows-per-template 5
""",
    )
    parser.add_argument("--input", "-i", type=Path, required=True,
                        help="Path to raw templates YAML file")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Output JSONL path (default: derived from input filename)")
    parser.add_argument("--workflows-per-template", type=int, default=1,
                        help="Number of samples to generate per template (default: 1)")
    parser.add_argument("--id", dest="ids", metavar="ID", action="append", default=None,
                        help="Only process template(s) with this ID (repeatable)")
    parser.add_argument("--workers", "-w", type=int, default=1,
                        help="Number of parallel template workers (default: 1)")
    add_common_args(parser)
    return parser


def default_output_path(input_path: Path) -> Path:
    return Path("outputs/data/zapier") / f"{input_path.stem}_samples.jsonl"


def run(args: argparse.Namespace) -> Path:
    if args.output is None:
        args.output = default_output_path(args.input)
    if args.provider is None:
        args.provider = infer_provider(args.model)
    if args.seed is not None:
        random.seed(args.seed)

    validate_paths(args.input, _GROUND_PROMPT)
    for p in [_OBJECTS_PROMPT, _STEPS_PROMPT]:
        if not p.exists():
            print(f"Error: prompt file not found: {p}", file=sys.stderr)
            sys.exit(1)

    templates = load_yaml(args.input)
    ground_cfg = load_prompt_template(_GROUND_PROMPT)
    objects_cfg = load_prompt_template(_OBJECTS_PROMPT)
    steps_cfg = load_prompt_template(_STEPS_PROMPT)

    if args.ids:
        id_set = set(args.ids)
        templates = [t for t in templates if t["id"] in id_set]
        if not templates:
            print(f"Error: no templates found with ID(s): {', '.join(sorted(id_set))}", file=sys.stderr)
            sys.exit(1)

    if args.limit:
        templates = templates[: args.limit]

    print(f"Loaded {len(templates)} templates from {args.input}")

    completed, file_mode = setup_output(
        args.output,
        args.force,
        lambda: load_completed_keys(args.output, lambda d: d.get("id")),
    )
    pending = [t for t in templates if t["id"] not in completed]

    if not pending:
        print("All templates already generated. Use --force to regenerate.")
        return args.output

    if completed:
        print(f"Resuming: {len(completed)} already completed, {len(pending)} remaining")
    else:
        print(f"Processing {len(pending)} templates")

    workers = getattr(args, "workers", 1)
    print_run_info(args.provider, args.model, args.seed,
                   {"Workflows per template": str(args.workflows_per_template),
                    "Workers": str(workers)})

    llm = create_llm(provider=args.provider, model=args.model,
                     temperature=args.temperature, seed=args.seed)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    success_count = 0
    fail_count = 0
    write_lock = threading.Lock()

    def _process_template(template: dict) -> tuple[list, int]:
        """Run all attempts for one template; return (samples, fail_count)."""
        results = []
        fails = 0
        for _ in range(args.workflows_per_template):
            grounded = _ground_template(llm, template, ground_cfg)
            if not grounded:
                fails += 1
                continue
            graph = _identify_objects(llm, grounded, template, objects_cfg)
            if not graph:
                fails += 1
                continue
            sample_steps = _write_steps(llm, grounded, graph, template, steps_cfg)
            if not sample_steps:
                fails += 1
                continue
            sample = _assemble_sample(template, grounded, graph, sample_steps)
            # Auto-generate mock tools for any read-service objects whose
            # behavior contains the `call the \`{object_id}_data\` tool` phrase.
            # Mutates sample.mock_tools + adds the _data skill to each matched
            # object so the runtime can resolve the tool at eval time.
            _add_mock_tools(llm, sample)
            results.append(sample)
        return results, fails

    with open(args.output, file_mode) as f:
        with tqdm(total=len(pending), desc="Generating samples") as pbar:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(_process_template, t): t for t in pending}
                for future in as_completed(futures):
                    try:
                        samples, fails = future.result()
                    except Exception as e:
                        tqdm.write(f"  FAILED {futures[future]['id']}: {e}", file=sys.stderr)
                        samples, fails = [], 1
                    with write_lock:
                        for sample in samples:
                            f.write(sample.model_dump_json() + "\n")
                        f.flush()
                    success_count += len(samples)
                    if not samples:
                        fail_count += 1
                    pbar.update(1)

    print()
    print(f"Complete. Output: {args.output}")
    print(f"Workflows generated: {success_count}, Templates failed: {fail_count}")
    return args.output


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
