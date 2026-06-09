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
    Event,
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
_GROUNDED_STEPS_PROMPT = _PROMPTS_DIR / "write_grounded_steps.yaml"


# ── Stage helpers ─────────────────────────────────────────────────────────────

def _format_template(template: dict) -> str:
    steps = "\n".join(f"- {s}" for s in (template.get("template") or template.get("raw_steps", [])))
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


def _format_mock_data_for_steps(tools) -> str:
    """Format mock tool seed data so steps can use ground-truth names/IDs."""
    if not tools:
        return "(no mock data available — invent realistic but consistent names)"
    chunks = []
    for t in tools:
        tmpl = (t.response_template or "").strip()
        if not tmpl:
            continue
        chunks.append(f"### {t.tool_name}\n{tmpl[:1500]}")
    return "\n\n".join(chunks) if chunks else "(no seed data found in mock tools)"


def _write_steps(llm, grounded: GroundedTemplate, graph: ObjectGraph, template: dict, prompt_cfg: dict, tools: list | None = None) -> WorkflowSteps | None:
    """Stage 1c: write the external trigger steps."""
    steps_text = "\n".join(f"- {s}" for s in grounded.grounded_steps)
    prompt = (
        prompt_cfg["prompt"]
        .replace("{NAME}", grounded.name)
        .replace("{GROUNDED_STEPS}", steps_text)
        .replace("{OBJECTS}", _format_objects(graph))
        .replace("{MOCK_DATA}", _format_mock_data_for_steps(tools or []))
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


def _generate_mock_tool_data(llm, tool_name: str, description: str, step_texts: list[str],
                             seed: str = "") -> MockToolDef | None:
    """Generate a mock tool for a read-service object.

    tool_name: the exact tool name (e.g. ``org_directory_data``)
    description: what the service stores (from state_description or role)
    seed: the authoritative initial reference state — if this tool holds part of it (roster,
          catalog, approvers, starting totals), the data MUST match the seed exactly.
    """
    seed_context = ""
    if seed:
        seed_context = (
            "\n\nAUTHORITATIVE SEED — initial reference state of the whole automation:\n"
            f"{seed}\n\nIf any of this seed is data THIS tool would serve (e.g. the roster, the "
            "catalog, the approvers, the starting totals), reproduce it EXACTLY — same entities, "
            "same positions/order, same numeric values. Do not invent different names or numbers "
            "for state the seed already fixes. (Ignore the parts of the seed another tool serves.)"
        )
    step_context = ""
    if step_texts:
        step_context = (
            "\n\nThe automation references these specific people, items, or identifiers "
            "(some appear inside descriptions of how requests were HANDLED):\n"
            + "\n".join(f"  - {t}" for t in step_texts)
            + "\n\nInclude every such person/item/entity in your data (exact same names), "
            "structured to match the tool's data type below — but as a STATIC reference "
            "record (identity + standing attributes only). Do NOT carry over any "
            "assignment, decision, status, outcome, or 'what happened' from the text above."
        )

    data_hint = _infer_data_hint(tool_name)

    messages = [
        ChatMessage(
            role="user",
            content=(
                f"Generate realistic reference data for a read-service mock API tool.\n\n"
                f"Tool: {tool_name}\n"
                f"What it stores: {description}"
                f"{seed_context}"
                f"{step_context}\n\n"
                f"IMPORTANT: {data_hint}\n\n"
                "CRITICAL — your data is the reference state the system READS, never the results "
                "it PRODUCES. Do NOT include any field that resolves, pre-decides, or records the "
                "outcome of a request: no assignments/'assigned_to', no approval/'approved' status, "
                "no held/blocked/pending/'eligible_for_assignment' decision flags, no per-entity "
                "running counts ('received today', 'reorders this week', 'count so far', 'remaining'), "
                "no event/assignment log, no 'roster_history' or 'assignment_reference'. Provide the "
                "directory/roster/catalog AS IT STANDS BEFORE any request is processed — standing "
                "facts only (who/what exists, positions, caps, prices, reorder levels). Including "
                "outcomes or accumulated counts hands the system the answer and makes the test invalid.\n\n"
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


def _generate_grounded_steps(llm, template_steps: list[str], objects: list) -> list[str]:
    """LLM call: ground each abstract template step using the object system."""
    prompt_template = load_prompt_template(_GROUNDED_STEPS_PROMPT)["prompt"]
    template_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(template_steps))
    objects_text = "\n\n".join(
        f"[{obj.object_id}] {obj.role}\n  behavior: {obj.behavior}"
        for obj in objects
    )
    prompt = (
        prompt_template
        .replace("{TEMPLATE}", template_text)
        .replace("{OBJECTS}", objects_text)
    )
    messages = [ChatMessage(role="user", content=prompt)]
    try:
        text = llm.generate_text(messages=messages).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        if isinstance(result, list) and all(isinstance(s, str) for s in result):
            return result
    except Exception:
        pass
    return list(template_steps)  # fallback: return template as-is


def _assemble_sample(template: dict, grounded: GroundedTemplate, graph: ObjectGraph, steps: WorkflowSteps) -> Workflow:
    """Combine stage outputs into a Workflow. Slugify ids. Mock tools generated separately."""
    for obj in graph.objects:
        obj.object_id = slugify(obj.object_id)
        # A custodian decides from its own state in one message and only
        # replies — it must have no peers, so nothing can suspend its decision
        # mid-flight. Strip any peers the architect emitted (a correctly-defined
        # custodian has none); requesters peer TO it, not the other way around.
        if obj.is_custodian:
            obj.peers = []
        for peer in obj.peers:
            peer.object_id = slugify(peer.object_id)
    for step in steps.steps:
        step.target = slugify(step.target)

    # Convert external-trigger steps to base events
    base_events = [
        Event(
            id=f"S{i+1:03d}",
            call_type="send",
            source=s.source,
            recipient=s.target,
            input=s.text,
            when="W00-1T00:00",
            expect=s.expect,
            role="base",
        )
        for i, s in enumerate(steps.steps)
    ]

    return Workflow(
        id=template["id"],
        name=grounded.name,
        domain=grounded.domain,
        source_type=template["source_type"],
        link=template.get("link") or template.get("seed_utterance", ""),
        template=template.get("raw_steps", []),
        objects=graph.objects,
        steps=[],       # filled by _add_grounded_steps after assembly
        events=base_events,
    )


# Tolerate hyphens in the captured tool name (LLMs sometimes write
# `sales-team-rotation-sheet_data` instead of the underscore convention); the name
# is normalized to underscores in _add_mock_tools so behavior/skill/mock all agree.
_DATA_TOOL_RE = re.compile(r"call the `([a-z][a-z0-9_-]*_data)` tool", re.IGNORECASE)

# ── Write-tool identification ─────────────────────────────────────────────────

_IDENTIFY_WRITE_TOOLS_PROMPT = """\
You are analyzing a distributed LLM-object automation system to identify what \
external write-side API calls are required but missing from the mock tool configuration.

## Objects in this automation

{OBJECTS}

## Already-defined mock tools

{EXISTING_TOOLS}

## Task

Identify every external WRITE-SIDE API call that any object in this automation must \
make to produce observable side-effects, but that is NOT already covered by the \
existing mock tools.

Focus on:
- Sending emails (e.g. send_email, send_approval_email)
- Posting to Slack or other messaging platforms (e.g. post_slack_message, send_slack_dm)
- Writing records to a CRM or database (e.g. update_hubspot_record, create_salesforce_opportunity)
- Creating tickets or tasks (e.g. create_jira_ticket, create_asana_task)
- Logging or recording actions in external systems (e.g. log_audit_event, append_sheet_row)
- Any other outbound API call that persists or transmits data externally

Do NOT include:
- Read-only data lookups (covered by _data tools)
- Internal state updates within the object itself
- Peer-to-peer message passing between objects in this system

For each missing write-side tool, return:
- tool_name: snake_case verb_noun (e.g. send_email, post_slack_message, update_hubspot_record)
- description: one sentence — what external write operation this tool performs
- used_by: the object_id that makes this call
- arguments: list of argument names this tool requires (e.g. ["recipient", "subject", "body"])

Return ONLY a JSON array. If no write-side tools are missing, return [].
Example: [{"tool_name": "send_email", "description": "Send an email to the specified recipient.", "used_by": "email-notifications", "arguments": ["recipient", "subject", "body", "quote_id"]}]
"""


def _parse_llm_tool_list(text: str) -> list[dict]:
    """Parse a JSON array from LLM output, stripping code fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return [r for r in result if isinstance(r, dict) and "tool_name" in r]
    except Exception:
        pass
    return []


def _identify_missing_write_tools(llm, sample) -> list[dict]:
    """Ask LLM what write-side API tools this sample's objects still need."""
    objects_text = "\n".join(
        f"[{obj.object_id}] {obj.role}\n  behavior: {obj.behavior[:300]}"
        for obj in sample.objects
    )
    existing = (
        "\n".join(f"  - {t.tool_name}: {t.description}" for t in sample.tools)
        or "  (none)"
    )
    prompt = (
        _IDENTIFY_WRITE_TOOLS_PROMPT
        .replace("{OBJECTS}", objects_text)
        .replace("{EXISTING_TOOLS}", existing)
    )
    messages = [ChatMessage(role="user", content=prompt)]
    try:
        return _parse_llm_tool_list(llm.generate_text(messages=messages))
    except Exception:
        pass
    return []


def _make_write_tool(entry: dict) -> "MockToolDef | None":
    """Build a MockToolDef stub for a write-side tool from the LLM-identified entry."""
    tool_name = entry.get("tool_name", "").strip()
    if not tool_name:
        return None
    description = entry.get("description", "").strip() or f"Perform {tool_name}."
    args = entry.get("arguments", [])
    if isinstance(args, list) and args:
        properties = {a: {"type": "string"} for a in args if isinstance(a, str)}
        args_schema = {"type": "object", "properties": properties, "additionalProperties": True}
    else:
        args_schema = {"type": "object", "additionalProperties": True}
    return MockToolDef(
        tool_name=tool_name,
        description=description,
        arguments_schema=args_schema,
        response_template=json.dumps({"status": "success", "tool": tool_name}),
    )


def _add_mock_tools(llm, sample: Workflow, seed: str = "") -> None:
    """Post-process: generate read-side mock tools and stub write-side tools.

    Mutates sample.tools AND obj.skills (adds the tool name so the runtime exposes it).
    Read services are detected by: "call the `{object_id}_data` tool to retrieve data"
    `seed` (optional) is the authoritative initial reference state the read-services must hold.
    """
    # Workflow.steps are plain strings (schema); tolerate legacy Step objects too.
    step_texts = [(_s if isinstance(_s, str) else getattr(_s, "text", "")) for _s in sample.steps]
    step_texts = [t for t in step_texts if t]
    existing_tool_names = {t.tool_name for t in sample.tools}

    # Read-side: generate realistic mock data per read-service object
    for obj in sample.objects:
        match = _DATA_TOOL_RE.search(obj.behavior or "")
        if not match:
            continue
        raw_name = match.group(1)
        # Normalize to the underscore convention; keep behavior/skill/mock consistent.
        tool_name = raw_name.replace("-", "_")
        if tool_name != raw_name:
            obj.behavior = (obj.behavior or "").replace(raw_name, tool_name)
        if tool_name not in obj.skills:
            obj.skills.append(tool_name)
        if tool_name in existing_tool_names:
            continue
        description = (obj.state_description or "").strip() or obj.role
        tool = _generate_mock_tool_data(llm, tool_name, description, step_texts, seed=seed)
        if tool:
            sample.tools.append(tool)
            existing_tool_names.add(tool_name)

    # Write-side: identify and stub write-side API tools
    for entry in _identify_missing_write_tools(llm, sample):
        tool_name = entry.get("tool_name", "").strip()
        if not tool_name or tool_name in existing_tool_names:
            continue
        tool = _make_write_tool(entry)
        if tool:
            sample.tools.append(tool)
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
            # Ground the template steps using the object system.
            sample.steps = _generate_grounded_steps(llm, sample.template, sample.objects)
            # Auto-generate mock tools (read-side data + write-side API stubs).
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
