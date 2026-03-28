"""
Test case generator for live NL programming.

Generates test cases (scenarios with modifications and events) from sample
instances using LLM-based generation.

Usage:
    python -m src.data.generate_test_cases \\
        --input outputs/data/zapier/generated/samples.jsonl \\
        --output outputs/data/zapier/generated/test_cases.jsonl \\
        --model claude-sonnet-4-5-20250929 \\
        --seed 42 \\
        --scenario-count 1
"""
from __future__ import annotations

import argparse
import random
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

# Load environment variables from .env file
load_dotenv()

from src.data.schema import Sample, Scenarios, TestCase, Modification, ModType, Ambiguity
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


def format_sample(sample: Sample) -> str:
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
        f"- [{step.target}] {step.text}" for step in sample.steps
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
    sample: Sample,
    scenario_count: int,
    events_before: int,
    events_after: int,
    events_unrelated: int,
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


def scenario_to_test_case(
    sample: Sample, scenario, index: int, mod_types: list[ModType], ambiguity: Ambiguity,
) -> TestCase:
    """Convert a scenario to a TestCase by merging with sample metadata and script-assigned fields."""
    modifications = [
        Modification(**gen_mod.model_dump(), mod_type=mt, ambiguity=ambiguity)
        for gen_mod, mt in zip(scenario.modifications, mod_types)
    ]
    return TestCase(
        id=f"{sample.id}-TC{index:03d}",
        name=sample.name,
        domain=sample.domain,
        source_type=sample.source_type,
        link=sample.link,
        objects=sample.objects,
        steps=sample.steps,
        modifications=modifications,
        events=scenario.events,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for generate_test_cases."""
    parser = argparse.ArgumentParser(
        description="Generate test cases from sample instances",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate with default model (provider inferred from model)
  python -m src.data.generate_test_cases -i outputs/data/zapier/generated/samples.jsonl

  # Generate with OpenAI
  python -m src.data.generate_test_cases -i outputs/data/zapier/generated/samples.jsonl --model gpt-4o

  # Custom scenario and event counts
  python -m src.data.generate_test_cases -i outputs/data/zapier/generated/samples.jsonl --scenario-count 2 --events-before 2 --events-after 3
""",
    )

    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        required=True,
        help="Path to samples JSONL file (output from generate_samples.py)",
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
        default=Path("config/prompts/data-gen/generate_test_cases.yaml"),
        help="Path to prompt template (default: config/prompts/data-gen/generate_test_cases.yaml)",
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
        default=1,
        help="Number of events unaffected by modification (default: 1)",
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
    samples = load_jsonl(args.input, Sample)
    prompt_template = load_prompt_template(args.prompt_template)["user_prompt"]

    # Apply limit if specified (0 or None means no limit)
    if args.limit:
        samples = samples[: args.limit]

    print(f"Loaded {len(samples)} samples from {args.input}")

    # Setup output and determine completed items
    # Key by sample.id (unique per generated sample). Test case IDs are "{sample_id}-TC{NNN}",
    # so we strip the trailing "-TC{NNN}" suffix to recover the sample ID.
    # Using 'link' would collide when --samples-per-template > 1 generates multiple samples
    # from the same template (they share the same link).
    completed, file_mode = setup_output(
        args.output,
        args.force,
        lambda: load_completed_keys(
            args.output,
            lambda d: d["id"].rsplit("-TC", 1)[0] if d.get("id") and "-TC" in d["id"] else d.get("id"),
        ),
    )

    pending = [s for s in samples if s.id not in completed]

    if not pending:
        print("All samples already generated. Use --force to regenerate.")
        return args.output

    if completed:
        print(f"Resuming: {len(completed)} already completed, {len(pending)} remaining")
    else:
        print(f"Processing {len(pending)} samples")

    # Determine which modification types to generate
    # Concrete mod types (excluding "mixed") for random sampling
    concrete_mod_types = [k for k in MODIFICATION_TYPES.keys() if k != "mixed"]
    if args.mod_type:
        mod_types_to_generate = [args.mod_type]
    else:
        mod_types_to_generate = concrete_mod_types

    # Concrete ambiguity values for random sampling
    ambiguity_values = [a.value for a in Ambiguity]

    print_run_info(
        args.provider,
        args.model,
        args.seed,
        {
            "Scenario count": str(args.scenario_count),
            "Modification types": ", ".join(mod_types_to_generate),
            "Ambiguity": args.ambiguity,
            "Events": f"{args.events_before} before, {args.events_after} after, {args.events_unrelated} unrelated",
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

    # Calculate total iterations for progress bar
    total_iterations = len(pending) * len(mod_types_to_generate)

    with open(args.output, file_mode) as f:
        with tqdm(total=total_iterations, desc="Generating test cases") as pbar:
            for sample in pending:
                for mod_type in mod_types_to_generate:
                    # Resolve mod types for each modification in the scenario
                    if mod_type == "mixed":
                        resolved_mod_types = [
                            random.choice(concrete_mod_types)
                            for _ in range(args.mods_per_scenario)
                        ]
                        # Build combined description for the prompt
                        mod_type_label = ", ".join(resolved_mod_types)
                        mod_description = "\n".join(
                            f"- Modification {i+1} ({mt}): {MODIFICATION_TYPES[mt]}"
                            for i, mt in enumerate(resolved_mod_types)
                        )
                    else:
                        resolved_mod_types = [mod_type] * args.mods_per_scenario
                        mod_type_label = mod_type
                        mod_description = MODIFICATION_TYPES[mod_type]

                    # Resolve ambiguity for this iteration
                    if args.ambiguity == "random":
                        ambiguity_constraint = random.choice(ambiguity_values)
                    else:
                        ambiguity_constraint = args.ambiguity
                    ambiguity_description = AMBIGUITY_DESCRIPTIONS[ambiguity_constraint]

                    # Format prompt
                    prompt = format_prompt(
                        prompt_template,
                        sample,
                        args.scenario_count,
                        args.events_before,
                        args.events_after,
                        args.events_unrelated,
                        modification_type=mod_type_label,
                        modification_type_description=mod_description,
                        mods_per_scenario=args.mods_per_scenario,
                        ambiguity_constraint=ambiguity_constraint,
                        ambiguity_description=ambiguity_description,
                    )

                    # Generate scenarios
                    result = generate_with_retries(
                        llm=llm,
                        prompt=prompt,
                        response_model=Scenarios,
                        item_id=f"{sample.id}-{mod_type}",
                        validator=lambda r: bool(r.scenarios),
                    )

                    if result:
                        # Convert scenarios to test cases and write each as a separate line
                        for i, scenario in enumerate(result.scenarios, start=1):
                            test_case = scenario_to_test_case(
                                sample, scenario, i,
                                mod_types=[ModType(mt) for mt in resolved_mod_types],
                                ambiguity=Ambiguity(ambiguity_constraint),
                            )
                            f.write(test_case.model_dump_json() + "\n")
                        f.flush()
                        success_count += len(result.scenarios)
                    else:
                        fail_count += 1

                    pbar.update(1)

    print()
    print(f"Complete. Output: {args.output}")
    print(f"Test cases generated: {success_count} (failed: {fail_count})")
    return args.output


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
