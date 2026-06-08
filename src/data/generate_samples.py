"""
Test case generator for live NL programming.

Generates test cases (scenarios with modifications and events) from sample
instances using LLM-based generation.

Usage:
    python -m src.data.generate_samples \\
        --input outputs/data/zapier/generated/workflows.jsonl \\
        --output outputs/data/zapier/generated/samples.jsonl \\
        --model claude-sonnet-4-5-20250929 \\
        --seed 42 \\
        --scenario-count 1
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

# Load environment variables from .env file
load_dotenv()

from src.data.schema import (
    Workflow, Scenarios, Sample, Modification, ModType, Ambiguity,
    Event, EventExpect, EventExpectations, ConcurrentGroupEvents,
)
from src.data.llm import create_llm
from src.data.utils import (
    infer_provider,
    load_prompt_template,
    load_jsonl,
    load_completed_keys,
    generate_with_retries,
    add_common_args,
    validate_paths,
    setup_output,
    print_run_info,
)

# Modification type definitions for test case generation
AMBIGUITY_DESCRIPTIONS = {
    "precise": "Fully specified with exact values, names, dates, and conditions. No room for interpretation.",
    "semantic": "Uses meaningful terms that require domain understanding but are unambiguous in context.",
    "vague": "Underspecified or fuzzy language that could be interpreted multiple ways.",
    "implicit": "Implied through context or tone rather than stated directly. Requires inference.",
    "random": (
        "Randomly assign an ambiguity level (precise, semantic, vague, or implicit) "
        "to each modification independently."
    ),
}

MODIFICATION_TYPES = {
    "temporal": (
        'Time-bound rule ("Sarah is out until Friday").\n'
        "Note: The modification `when` is when the rule is introduced. The rule's "
        'condition (e.g., "after 5 PM") determines when it applies. Events must '
        "respect both."
    ),
    "contextual": 'Conditional rule ("Enterprise customers get priority")',
    "exception": 'Override for specific entity ("Acme Corp always goes to Jake")',
    "correction": 'Fix prior logic, may be retroactive ("Threshold should be $100 not $50")',
    "expansion": 'Add behavior ("Also send Slack notification")',
    "removal": 'Deactivate behavior ("Stop sending notifications")',
    "mixed": (
        "Any combination of modification types. Choose types that create interesting interactions:\n"
        "- temporal: Time-bound rules\n"
        "- contextual: Conditional rules\n"
        "- exception: Entity-specific overrides\n"
        "- correction: Logic fixes\n"
        "- expansion: Additional behaviors\n"
        "- removal: Deactivated behaviors"
    ),
}


def format_sample(sample: Workflow) -> str:
    """Format a sample instance for the prompt, including object definitions."""
    # Format object definitions
    objects_lines = []
    for obj in sample.objects:
        objects_lines.append(f"  - `{obj.object_id}`: {obj.role}")
        objects_lines.append(f"    State: {obj.state_description}")
        objects_lines.append(f"    Behavior: {obj.behavior}")
        if obj.peers:
            peers_str = ", ".join(
                f"{p.object_id} ({p.relationship})" for p in obj.peers
            )
            objects_lines.append(f"    Peers: {peers_str}")
        if obj.skills:
            objects_lines.append(f"    Skills: {', '.join(obj.skills)}")
        if obj.subscriptions:
            objects_lines.append(f"    Subscriptions: {', '.join(obj.subscriptions)}")
    objects_str = "\n".join(objects_lines)

    steps = "\n".join(
        f"- {s}" for s in sample.steps
    )
    return f"""ID: {sample.id}
Name: {sample.name}
Domain: {sample.domain}
Source: {sample.source_type}
Link: {sample.link}

Objects:
{objects_str}

Steps:
{steps}"""


def format_prompt(
    prompt_template: str,
    sample: Workflow,
    scenario_count: int,
    events_before: int,
    events_after: int,
    events_unrelated: int,
    events_inter_mod: int,
    modification_type: str,
    modification_type_description: str,
    mods_per_scenario: int,
    ambiguity_constraint: str,
    ambiguity_description: str,
) -> str:
    """Format prompt template with sample data and parameters."""
    sample_str = format_sample(sample)
    substitutions = {
        "SAMPLE": sample_str,
        "SCENARIO_COUNT": str(scenario_count),
        "EVENTS_BEFORE_COUNT": str(events_before),
        "EVENTS_AFTER_COUNT": str(events_after),
        "EVENTS_UNRELATED_COUNT": str(events_unrelated),
        "EVENTS_INTER_MOD_COUNT": str(events_inter_mod),
        "MODIFICATION_TYPE": modification_type,
        "MODIFICATION_TYPE_DESCRIPTION": modification_type_description,
        "MODS_PER_SCENARIO": str(mods_per_scenario),
        "AMBIGUITY_CONSTRAINT": ambiguity_constraint,
        "AMBIGUITY_DESCRIPTION": ambiguity_description,
    }
    result = prompt_template
    for key, value in substitutions.items():
        result = result.replace(f"{{{key}}}", value)
    return result


def _ts_key(ts: str) -> tuple:
    """Parse 'W{w}-{d}T{HH:MM}' into a comparable tuple (week, day, hour, minute)."""
    import re as _re
    m = _re.match(r"W(\d+)-(\d+)T(\d+):(\d+)", ts or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))) if m else (0, 0, 0, 0)


def _active_mods_for(event_when: str, modifications: list) -> list[str]:
    """Return mod IDs that are active at event_when (mod.when <= event_when)."""
    mod_keys = [(_ts_key(m.when), m.id) for m in modifications]
    ek = _ts_key(event_when)
    return [mid for mk, mid in mod_keys if ek >= mk]


def _rewrite_event_expectations(llm, test_case: Sample, sample: Workflow) -> None:
    """Rewrite event expectations using the finalized mock data (mutates test_case.events).

    The scenario generator writes events without expectations. This function writes them
    using the actual data the read-service mocks will return at eval time, so names like
    "the identified manager" are replaced with the real person from the org directory.
    """
    import yaml as _yaml

    prompt_path = Path(__file__).parent.parent.parent / "config" / "prompts" / "data-gen" / "write_expectations.yaml"
    with open(prompt_path) as f:
        raw_prompt = _yaml.safe_load(f)["prompt"]

    steps_lines = "\n".join(
        f"{i+1}. {s}" for i, s in enumerate(sample.steps or [])
    ) or "(none)"
    read_tools = [t for t in test_case.tools if "_data" in t.tool_name.lower()]
    mock_lines = "\n\n".join(
        f"Tool: {t.tool_name}\n{t.response_template[:3000]}"
        for t in read_tools
    ) or "(none)"
    mod_lines = "\n".join(
        f"{m.id} at {m.when} → {m.target}: {m.intent}"
        for m in test_case.modifications
    ) or "(none)"

    mod_ids_set = {m.id for m in test_case.modifications}
    event_lines_parts = []
    for e in test_case.events:
        # Skip events whose expectation is already authored (e.g. the state-constraint
        # base events from Stage 1.5, whose expects encode cumulative reasoning the
        # mock-data rewrite would clobber). They are not sent to the LLM nor overwritten.
        if e.expect is not None:
            continue
        # Prefer after_mod_ids when populated; fall back to timestamp inference
        if e.after_mod_ids:
            active = [mid for mid in e.after_mod_ids if mid in mod_ids_set]
        else:
            active = _active_mods_for(e.when, test_case.modifications)
        mod_note = f" [active modifications: {', '.join(active)}]" if active else " [no modifications active — use baseline behavior]"
        event_lines_parts.append(
            f"{e.id} | {e.when}{mod_note} | recipient={e.recipient} | input: {e.input}"
        )
    if not event_lines_parts:
        # Every event already has an authored expectation (nothing to rewrite).
        return
    event_lines = "\n".join(event_lines_parts)

    prompt = (raw_prompt
        .replace("{STEPS}", steps_lines)
        .replace("{MOCK_DATA}", mock_lines)
        .replace("{MODIFICATIONS}", mod_lines)
        .replace("{EVENTS}", event_lines)
    )

    result = generate_with_retries(
        llm=llm,
        prompt=prompt,
        response_model=EventExpectations,
        item_id=f"{test_case.id}-expectations",
        validator=lambda r: len(r.expectations) > 0,
    )
    if not result:
        return

    expect_map = {item.event_id: item for item in result.expectations}
    for evt in test_case.events:
        if evt.id in expect_map:
            item = expect_map[evt.id]
            evt.expect = EventExpect(action=item.action, reason=item.reason)


_CONCURRENT_EVENTS_PROMPT_PATH = (
    Path(__file__).parent.parent.parent
    / "config" / "prompts" / "data-gen" / "generate_concurrent_events.yaml"
)


def _next_event_id(events: list[Event]) -> int:
    """Return the next available E{NNN} integer after all existing event IDs."""
    nums = [int(m.group(1)) for e in events if (m := re.match(r"E(\d+)$", e.id))]
    return max(nums, default=0) + 1


def _generate_concurrent_group(
    llm,
    sample: Workflow,
    tc: Sample,
    mod: Modification,
    group_type: str,   # "pre" or "post"
    n: int,
    start_id: int,
) -> list[Event]:
    """Generate N concurrent events for one (mod, group_type) pair via a focused LLM call.

    group_type="pre"  → cgroup_pre_{mod.id}: fires before the mod (1 pre_mod + n-1 irrelevant)
    group_type="post" → cgroup_post_{mod.id}: fires after the mod settles (1 post_mod + n-1 irrelevant)
    """
    import yaml as _yaml
    with open(_CONCURRENT_EVENTS_PROMPT_PATH) as f:
        prompt_template = _yaml.safe_load(f)["prompt"]

    group_name = f"cgroup_{group_type}_{mod.id}"
    role = "pre_mod" if group_type == "pre" else "post_mod"

    # Active mod IDs for the relevant event's after_mod_ids
    mod_index = next((i for i, m in enumerate(tc.modifications) if m.id == mod.id), 0)
    after_mod_ids: list[str] = (
        [] if group_type == "pre"
        else [m.id for m in tc.modifications[:mod_index + 1]]
    )
    after_mod_ids_str = "[]" if not after_mod_ids else str(after_mod_ids).replace("'", '"')

    role_description = (
        "before the modification fires — use baseline system behavior (no mods active)"
        if group_type == "pre"
        else "after the modification has settled — the change must be reflected in the outcome"
    )

    existing_inputs = "\n".join(
        f"- [{e.role}] {e.input[:120]}"
        for e in tc.events
        if not e.concurrent_group
    ) or "(none)"

    # Format sample summary (objects + steps)
    sample_str = format_sample(sample)

    prompt = (
        prompt_template
        .replace("{SAMPLE}", sample_str)
        .replace("{MOD_TARGET}", mod.target)
        .replace("{MOD_INTENT}", mod.intent)
        .replace("{GROUP_NAME}", group_name)
        .replace("{ROLE}", role)
        .replace("{ROLE_DESCRIPTION}", role_description)
        .replace("{AFTER_MOD_IDS}", after_mod_ids_str)
        .replace("{N}", str(n))
        .replace("{N_IRRELEVANT}", str(n - 1))
        .replace("{WHEN}", mod.when)
        .replace("{START_ID}", str(start_id))
        .replace("{EXISTING_EVENTS}", existing_inputs)
    )

    result = generate_with_retries(
        llm=llm,
        prompt=prompt,
        response_model=ConcurrentGroupEvents,
        item_id=f"{tc.id}-{group_name}",
        validator=lambda r: len(r.events) > 0,
    )
    if not result:
        return []

    # Convert GeneratedEvents → Events, enforce correct metadata, assign sequential IDs
    out: list[Event] = []
    for i, ge in enumerate(result.events):
        d = ge.model_dump()
        d["id"] = f"E{start_id + i:03d}"
        d["concurrent_group"] = group_name
        d["expect"] = None   # filled by _rewrite_event_expectations
        d["triggered_by"] = None
        # enforce role and after_mod_ids per position in the group
        if i == 0:
            d["role"] = role
            d["after_mod_ids"] = after_mod_ids
        else:
            d["role"] = "irrelevant"
            d["after_mod_ids"] = []
        out.append(Event(**d))
    return out


def _add_concurrent_events_to_tc(llm, sample: Workflow, tc: Sample, n: int, workers: int = 1) -> None:
    """Generate concurrent groups for all mods in tc and append to tc.events (mutates in place).

    All groups are generated in parallel (one LLM call per group) using pre-assigned IDs
    so parallel generation doesn't race on tc.events.
    """
    groups = [
        (mod, group_type)
        for mod in tc.modifications
        for group_type in ("pre", "post")
    ]
    if not groups:
        return

    # Pre-assign start IDs: each group gets exactly n event slots
    base_id = _next_event_id(tc.events)
    start_ids = {(mod.id, gt): base_id + i * n for i, (mod, gt) in enumerate(groups)}

    def _gen(mod_gt):
        mod, gt = mod_gt
        return _generate_concurrent_group(llm, sample, tc, mod, gt, n, start_ids[(mod.id, gt)])

    if workers > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(groups))) as pool:
            results = list(pool.map(_gen, groups))
    else:
        results = [_gen(g) for g in groups]

    for new_events in results:
        tc.events.extend(new_events)


def scenario_to_test_case(
    sample: Workflow, scenario, index: int, mod_types: list[ModType], ambiguity: Ambiguity,
) -> Sample:
    """Convert a scenario to a Sample by merging with sample metadata and script-assigned fields."""
    modifications = [
        Modification(**gen_mod.model_dump(), mod_type=mt, ambiguity=ambiguity)
        for gen_mod, mt in zip(scenario.modifications, mod_types)
    ]
    # Carry the workflow's base scenario (role="base" events injected by Stage 1.5)
    # ahead of the generated mod events, so the state constraint precedes any
    # modifications. Only present when a state constraint was generated.
    base_events = (
        [Event(**e.model_dump()) for e in sample.events if e.role == "base"]
        if sample.state_constraint else []
    )
    scenario_events = [Event(**e.model_dump()) for e in scenario.events]

    # Base events and scenario events are numbered independently (both start E001),
    # so merging them collides. Offset scenario event ids past the base block and
    # remap their cross-references, so the base events keep their cumulative-aware
    # expects (otherwise _rewrite_event_expectations clobbers them by id match).
    if base_events and scenario_events:
        def _enum(eid: str):
            return int(eid[1:]) if eid and eid[0] in "Ee" and eid[1:].isdigit() else None
        offset = max((_enum(e.id) or 0) for e in base_events)
        remap = {e.id: f"E{_enum(e.id) + offset:03d}" for e in scenario_events if _enum(e.id) is not None}
        for e in scenario_events:
            e.id = remap.get(e.id, e.id)
            if isinstance(e.triggered_by, str):
                e.triggered_by = remap.get(e.triggered_by, e.triggered_by)
            e.depends_on = [remap.get(d, d) for d in e.depends_on]

    events = base_events + scenario_events
    # Deterministic fix: clear triggered_by if it references a non-event ID
    # (LLMs sometimes generate triggered_by='M001' referencing a modification ID)
    event_ids = {e.id for e in events}
    for e in events:
        if e.triggered_by is not None and e.triggered_by not in event_ids:
            e.triggered_by = None

    # Include mod_type in TC ID so each (sample, mod_type) pair is unique and
    # the completion cache (which keys by stripping "-TC{NNN}") can track them independently.
    mod_type_slug = mod_types[0].value if mod_types else "mixed"
    return Sample(
        id=f"{sample.id}-{mod_type_slug}-TC{index:03d}",
        sample_id=sample.id,
        name=sample.name,
        domain=sample.domain,
        source_type=sample.source_type,
        link=sample.link,
        objects=sample.objects,
        steps=sample.steps,
        modifications=modifications,
        events=events,
        tools=list(sample.tools),
        state_constraint=sample.state_constraint,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for generate_samples."""
    parser = argparse.ArgumentParser(
        description="Generate test cases from sample instances",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate with default model (provider inferred from model)
  python -m src.data.generate_samples -i outputs/data/zapier/generated/workflows.jsonl

  # Generate with OpenAI
  python -m src.data.generate_samples -i outputs/data/zapier/generated/workflows.jsonl --model gpt-4o

  # Custom scenario and event counts
  python -m src.data.generate_samples -i outputs/data/zapier/generated/workflows.jsonl --scenario-count 2 --events-before 2 --events-after 3
""",
    )

    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        required=True,
        help="Path to samples JSONL file (output from generate_workflows.py)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output JSONL path (default: derived from input file, mod-type, and ambiguity)",
    )
    parser.add_argument(
        "--prompt-template",
        type=Path,
        default=Path("config/prompts/data-gen/generate_samples.yaml"),
        help="Path to prompt template (default: config/prompts/data-gen/generate_samples.yaml)",
    )
    parser.add_argument(
        "--scenario-count",
        type=int,
        default=1,
        help="Number of scenarios to generate per modification type (default: 1)",
    )
    parser.add_argument(
        "--events-before",
        type=int,
        default=1,
        help="Number of events before modification timestamp (default: 1)",
    )
    parser.add_argument(
        "--events-after",
        type=int,
        default=2,
        help="Number of events after modification timestamp (default: 2)",
    )
    parser.add_argument(
        "--events-unrelated",
        type=int,
        default=None,
        help="Number of events unaffected by modifications (default: mods-per-scenario, i.e. one per mod)",
    )
    parser.add_argument(
        "--events-inter-mod",
        type=int,
        default=1,
        help="Number of events per gap between consecutive modifications (default: 1)",
    )
    parser.add_argument(
        "--concurrent-events",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Number of events per concurrent group per modification window (default: 0 = none). "
            "When >0, generates a pre-mod and post-mod concurrent group for each modification: "
            "1 relevant event + N-1 irrelevant events tagged with concurrent_group. "
            "Use --concurrency at eval time to control how many actually fire concurrently."
        ),
    )
    parser.add_argument(
        "--mod-type",
        type=str,
        choices=list(MODIFICATION_TYPES.keys()) + ["mixed"],
        default=None,
        help="Modification type (or 'mixed' for random types). If not specified, generates for all types separately.",
    )
    parser.add_argument(
        "--mods-per-scenario",
        type=int,
        default=1,
        help="Number of modifications per scenario (default: 1)",
    )
    parser.add_argument(
        "--ambiguity",
        type=str,
        choices=list(AMBIGUITY_DESCRIPTIONS.keys()),
        default="random",
        help="Ambiguity level for modifications (default: random). Use 'random' for random assignment per iteration.",
    )
    parser.add_argument(
        "--id",
        dest="ids",
        metavar="ID",
        action="append",
        default=None,
        help="Only process sample(s) with this ID (repeatable: --id foo --id bar)",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=1,
        help="Number of parallel workers (default: 1). One worker per (sample, mod-type) unit.",
    )
    add_common_args(parser)
    return parser


def default_output_path(input_path: Path, mod_type: str | None, ambiguity: str) -> Path:
    """Derive the default output path from input file, mod-type, and ambiguity."""
    input_stem = input_path.stem
    mod_part = mod_type or "all"
    output_name = f"{input_stem}__{mod_part}__{ambiguity}.jsonl"
    return Path("outputs/data/zapier") / output_name


def run(args: argparse.Namespace) -> Path:
    """Run test case generation. Returns the output path."""
    # Derive default output path from input file, mod-type, and ambiguity
    if args.output is None:
        args.output = default_output_path(args.input, args.mod_type, args.ambiguity)

    # Infer provider from model if not specified
    if args.provider is None:
        args.provider = infer_provider(args.model)

    # Initialize random seed
    if args.seed is not None:
        random.seed(args.seed)

    # Validate inputs
    validate_paths(args.input, args.prompt_template)

    # Load data
    samples = load_jsonl(args.input, Workflow)
    prompt_template = load_prompt_template(args.prompt_template)["user_prompt"]

    # Apply ID filter if specified
    if getattr(args, "ids", None):
        id_set = set(args.ids)
        samples = [s for s in samples if s.id in id_set]

    # Apply limit if specified (0 or None means no limit)
    if args.limit:
        samples = samples[: args.limit]

    print(f"Loaded {len(samples)} samples from {args.input}")

    # Determine which modification types to generate
    # Concrete mod types (excluding "mixed") for random sampling
    concrete_mod_types = [k for k in MODIFICATION_TYPES.keys() if k != "mixed"]
    if args.mod_type:
        mod_types_to_generate = [args.mod_type]
    else:
        mod_types_to_generate = concrete_mod_types

    # Setup output and determine completed items.
    # TC IDs are "{sample_id}-{mod_type}-TC{NNN}", so stripping "-TC{NNN}" gives
    # "{sample_id}-{mod_type}" — the natural completion key for each (sample, mod_type) pair.
    completed, file_mode = setup_output(
        args.output,
        args.force,
        lambda: load_completed_keys(
            args.output,
            lambda d: d["id"].rsplit("-TC", 1)[0] if d.get("id") and "-TC" in d["id"] else d.get("id"),
        ),
    )

    # Flatten (sample, mod_type) work units, skipping already-completed pairs.
    # completed keys are "{sample_id}-{mod_type}", so check at that granularity.
    work_units = [
        (sample, mod_type)
        for sample in samples
        for mod_type in mod_types_to_generate
        if f"{sample.id}-{mod_type}" not in completed
    ]

    if not work_units:
        print("All samples already generated. Use --force to regenerate.")
        # Still run concurrent events pass if requested.
        if args.concurrent_events > 0:
            _run_concurrent_events_pass(args, samples)
        return args.output

    total_units = len(samples) * len(mod_types_to_generate)
    if completed:
        print(f"Resuming: {len(completed)} already completed, {len(work_units)} remaining")
    else:
        print(f"Processing {len(work_units)} work units")

    # Concrete ambiguity values for random sampling
    ambiguity_values = [a.value for a in Ambiguity]

    workers = getattr(args, "workers", 1)
    print_run_info(
        args.provider,
        args.model,
        args.seed,
        {
            "Scenario count": str(args.scenario_count),
            "Modification types": ", ".join(mod_types_to_generate),
            "Ambiguity": args.ambiguity,
            "Events": f"{args.events_before} before, {args.events_inter_mod} inter-mod, {args.events_after} after, {args.events_unrelated or args.mods_per_scenario} unrelated",
            "Workers": str(workers),
        },
    )

    # Create LLM client
    llm = create_llm(
        provider=args.provider,
        model=args.model,
        temperature=args.temperature,
        seed=args.seed,
    )

    # Process samples
    args.output.parent.mkdir(parents=True, exist_ok=True)
    success_count = 0
    fail_count = 0
    write_lock = threading.Lock()

    def _process_unit(sample: Workflow, mod_type: str) -> list[Sample]:
        """Generate test cases for one (sample, mod_type) pair."""
        if mod_type == "mixed":
            resolved_mod_types = [
                random.choice(concrete_mod_types)
                for _ in range(args.mods_per_scenario)
            ]
            mod_type_label = ", ".join(resolved_mod_types)
            mod_description = "\n".join(
                f"- Modification {i+1} ({mt}): {MODIFICATION_TYPES[mt]}"
                for i, mt in enumerate(resolved_mod_types)
            )
        else:
            resolved_mod_types = [mod_type] * args.mods_per_scenario
            mod_type_label = mod_type
            mod_description = MODIFICATION_TYPES[mod_type]

        if args.ambiguity == "random":
            ambiguity_constraint = random.choice(ambiguity_values)
        else:
            ambiguity_constraint = args.ambiguity
        ambiguity_description = AMBIGUITY_DESCRIPTIONS[ambiguity_constraint]

        events_unrelated = args.events_unrelated if args.events_unrelated is not None else args.mods_per_scenario
        prompt = format_prompt(
            prompt_template,
            sample,
            args.scenario_count,
            args.events_before,
            args.events_after,
            events_unrelated,
            args.events_inter_mod,
            modification_type=mod_type_label,
            modification_type_description=mod_description,
            mods_per_scenario=args.mods_per_scenario,
            ambiguity_constraint=ambiguity_constraint,
            ambiguity_description=ambiguity_description,
        )

        result = generate_with_retries(
            llm=llm,
            prompt=prompt,
            response_model=Scenarios,
            item_id=f"{sample.id}-{mod_type}",
            validator=lambda r: bool(r.scenarios),
        )

        if not result:
            return []

        scenarios = result.scenarios[:args.scenario_count]
        test_cases = [
            scenario_to_test_case(
                sample, scenario, i,
                mod_types=[ModType(mt) for mt in resolved_mod_types],
                ambiguity=Ambiguity(ambiguity_constraint),
            )
            for i, scenario in enumerate(scenarios, start=1)
        ]
        for tc in test_cases:
            _rewrite_event_expectations(llm, tc, sample)
        return test_cases

    with open(args.output, file_mode) as f:
        with tqdm(total=len(work_units), desc="Generating test cases") as pbar:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(_process_unit, sample, mod_type): (sample.id, mod_type)
                    for sample, mod_type in work_units
                }
                for future in as_completed(futures):
                    sample_id, mod_type = futures[future]
                    try:
                        test_cases = future.result()
                    except Exception as e:
                        tqdm.write(f"  FAILED {sample_id}-{mod_type}: {e}", file=sys.stderr)
                        test_cases = []
                    with write_lock:
                        for tc in test_cases:
                            f.write(tc.model_dump_json() + "\n")
                        if test_cases:
                            f.flush()
                    if test_cases:
                        success_count += len(test_cases)
                    else:
                        fail_count += 1
                    pbar.update(1)

    print()
    print(f"Complete. Output: {args.output}")
    print(f"Test cases generated: {success_count} (failed: {fail_count})")

    # Concurrent events pass — runs after scenario generation (or on continuation).
    # Loads all TCs from the output file, adds concurrent groups to any TC that is missing
    # them, and writes the file back in place. Safe to re-run: TCs that already have
    # concurrent groups are skipped.
    if args.concurrent_events > 0:
        _run_concurrent_events_pass(args, samples)

    return args.output


def _run_concurrent_events_pass(args: argparse.Namespace, samples: list[Workflow]) -> None:
    """Add concurrent groups to all TCs in the output file that don't have them yet."""
    import json as _json
    from src.data.llm import create_llm as _create_llm

    n = args.concurrent_events
    output_path = args.output
    sample_map = {s.id: s for s in samples}

    # Load all lines; deduplicate TCs keeping the last occurrence of each ID.
    raw_lines: list[str] = []
    seen_ids: dict[str, int] = {}   # tc_id → last line index

    with open(output_path) as f:
        for i, line in enumerate(f):
            line = line.rstrip("\n")
            raw_lines.append(line)
            if not line:
                continue
            try:
                d = _json.loads(line)
                if "id" in d and "events" in d:
                    seen_ids[d["id"]] = i
            except Exception:
                pass

    # Build deduplicated TC list (one entry per unique ID, at its last line position)
    tcs: list[tuple[int, Sample]] = []
    for tc_id, idx in seen_ids.items():
        try:
            tc = Sample.model_validate(_json.loads(raw_lines[idx]))
            tcs.append((idx, tc))
        except Exception:
            pass

    n_dupes = len(raw_lines) - len(tcs)
    if n_dupes > 0:
        print(f"Deduplicating: {len(raw_lines)} lines → {len(tcs)} unique TCs ({n_dupes} duplicates removed)")

    def _has_correct_groups(tc: Sample) -> bool:
        """True when tc already has correctly-named cgroup_pre/post groups for every mod."""
        expected = {f"cgroup_{gt}_{mod.id}" for mod in tc.modifications for gt in ("pre", "post")}
        actual = {e.concurrent_group for e in tc.events if e.concurrent_group}
        return expected.issubset(actual)

    needs_conc = [(i, tc) for i, tc in tcs if not _has_correct_groups(tc)]
    if not needs_conc:
        print("Concurrent events: all TCs already have concurrent groups.")
        _write_deduped(output_path, raw_lines, seen_ids)
        return

    workers = getattr(args, "workers", 1)
    print(f"Concurrent events pass: {len(needs_conc)} TCs need concurrent groups "
          f"({n} events/group, {workers} workers)")

    llm = _create_llm(model=args.model, provider=args.provider)
    write_lock = threading.Lock()
    done = 0
    failed = 0

    def _process_one(item: tuple[int, Sample]) -> tuple[int, Sample]:
        idx, tc = item
        sample = sample_map.get(tc.sample_id) or Workflow(
            id=tc.sample_id or tc.id, name=tc.name, domain=tc.domain,
            source_type=tc.source_type, link=tc.link,
            raw_steps=tc.steps, objects=tc.objects,
            steps=tc.steps, tools=tc.tools,
        )
        _add_concurrent_events_to_tc(llm, sample, tc, n, workers=1)
        _rewrite_event_expectations(llm, tc, sample)
        return idx, tc

    with open(output_path, "a") as append_f:
        with tqdm(total=len(needs_conc), desc="Adding concurrent events") as pbar:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(_process_one, item): item for item in needs_conc}
                for future in as_completed(futures):
                    try:
                        idx, tc = future.result()
                        line = tc.model_dump_json()
                        with write_lock:
                            # Append immediately so progress survives interruption.
                            # The final dedup step keeps only the last occurrence.
                            append_f.write(line + "\n")
                            append_f.flush()
                            raw_lines[idx] = line
                        done += 1
                    except Exception as e:
                        _, orig_tc = futures[future]
                        tqdm.write(f"  WARN: {orig_tc.id}: {e}", file=sys.stderr)
                        failed += 1
                    pbar.update(1)

    # Collapse duplicates now that all appends are done
    _write_deduped(output_path, raw_lines, seen_ids)
    print(f"Concurrent events pass: {done} updated, {failed} failed. Written to {output_path}")


def _write_deduped(output_path: Path, raw_lines: list[str], seen_ids: dict[str, int]) -> None:
    """Write output keeping only the last occurrence of each TC ID."""
    import json as _json
    canonical: set[int] = set(seen_ids.values())
    with open(output_path, "w") as f:
        for i, line in enumerate(raw_lines):
            if not line:
                continue
            try:
                d = _json.loads(line)
                if "id" in d and "events" in d and i not in canonical:
                    continue   # skip duplicate
            except Exception:
                pass
            f.write(line + "\n")


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
