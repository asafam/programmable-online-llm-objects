"""
Sample generator for live NL programming.

Generates concrete samples from raw Zapier automation templates using LLM-based
generation. Each sample instantiates a template with specific values and
decomposes it into LLM-objects with structured steps.

Usage:
    python -m src.data.generate_samples \\
        --input data/zapier/raw/examples.yaml \\
        --output outputs/data/zapier/generated/samples.jsonl \\
        --model claude-sonnet-4-5-20250929 \\
        --seed 42 \\
        --samples-per-template 3
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

# Load environment variables from .env file
load_dotenv()

from src.data.schema import Samples
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


def _seed_initial_state(llm, state_description: str, step_texts: list[str] | None = None) -> dict:
    """Convert a state_description text into a concrete JSON seed_data dict.

    step_texts: the text of all sample steps, so the LLM can use consistent
    names/identifiers in seed_data (preventing lookup mismatches at eval time).
    """
    step_context = ""
    if step_texts:
        step_context = (
            "\n\nThe automation's steps reference these specific people, items, or identifiers:\n"
            + "\n".join(f"  - {t}" for t in step_texts)
            + "\n\nYour seed data MUST include entries for every person, item, or entity "
            "mentioned in those steps, using exactly the same names or identifiers. "
            "If the steps use names (e.g., 'James Brown'), use those names. "
            "If they use IDs, create records with matching IDs."
        )

    messages = [
        ChatMessage(
            role="user",
            content=(
                "Generate concrete seed data for a read-service object. "
                "Invent plausible names, values, and relationships consistent with the scenario.\n\n"
                f"State description: {state_description}"
                f"{step_context}\n\n"
                "Respond with ONLY the raw JSON object (no markdown, no explanation)."
            ),
        )
    ]
    try:
        text = llm.generate_text(messages=messages)
        # Strip markdown code fences if present
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except Exception:
        return {}


def format_template(template: dict) -> str:
    """Format a template for the prompt."""
    steps = "\n".join(f"- {step}" for step in template["raw_steps"])
    return f"""ID: {template['id']}
Name: {template['name']}
Domain: {template.get('domain', 'general')}
Source: {template['source_type']}
Link: {template['link']}

Raw Steps:
{steps}"""


def format_prompt(prompt_template: dict, template: dict, samples_count: int) -> str:
    """Format prompt template with template data."""
    template_str = format_template(template)
    return (
        prompt_template["prompt"]
        .replace("{TEMPLATE}", template_str)
        .replace("{SAMPLES_COUNT}", str(samples_count))
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for generate_samples."""
    parser = argparse.ArgumentParser(
        description="Generate samples from raw Zapier automation templates",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate with default model (provider inferred from model)
  python -m src.data.generate_samples -i data/zapier/raw/examples.yaml

  # Generate with OpenAI
  python -m src.data.generate_samples -i data/zapier/raw/examples.yaml --model gpt-4o

  # Multiple samples per template
  python -m src.data.generate_samples -i data/zapier/raw/examples.yaml --samples-per-template 5
""",
    )

    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        required=True,
        help="Path to raw templates YAML file",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output JSONL path (default: derived from input filename)",
    )
    parser.add_argument(
        "--prompt-template",
        type=Path,
        default=Path("config/prompts/data-gen/generate_samples.yaml"),
        help="Path to prompt template (default: config/prompts/data-gen/generate_samples.yaml)",
    )
    parser.add_argument(
        "--samples-per-template",
        type=int,
        default=1,
        help="Number of samples to generate per template (default: 1)",
    )
    parser.add_argument(
        "--id",
        dest="ids",
        metavar="ID",
        action="append",
        default=None,
        help="Only process template(s) with this ID (repeatable: --id foo --id bar)",
    )
    add_common_args(parser)
    return parser


def default_output_path(input_path: Path) -> Path:
    """Derive the default output path from input filename."""
    return Path("outputs/data/zapier") / f"{input_path.stem}_samples.jsonl"


def run(args: argparse.Namespace) -> Path:
    """Run sample generation. Returns the output path."""
    if args.output is None:
        args.output = default_output_path(args.input)

    # Infer provider from model if not specified
    if args.provider is None:
        args.provider = infer_provider(args.model)

    # Initialize random seed
    if args.seed is not None:
        random.seed(args.seed)

    # Validate inputs
    validate_paths(args.input, args.prompt_template)

    # Load data
    templates = load_yaml(args.input)
    prompt_template = load_prompt_template(args.prompt_template)

    # Filter by ID if specified
    if args.ids:
        id_set = set(args.ids)
        templates = [t for t in templates if t["id"] in id_set]
        if not templates:
            print(f"Error: no templates found with ID(s): {', '.join(sorted(id_set))}", file=sys.stderr)
            sys.exit(1)

    # Apply limit if specified (0 or None means no limit)
    if args.limit:
        templates = templates[: args.limit]

    print(f"Loaded {len(templates)} templates from {args.input}")

    # Setup output and determine completed items
    completed, file_mode = setup_output(
        args.output,
        args.force,
        lambda: load_completed_keys(args.output, lambda d: d.get("link")),
    )

    pending = [t for t in templates if t["link"] not in completed]

    if not pending:
        print("All templates already generated. Use --force to regenerate.")
        return args.output

    if completed:
        print(f"Resuming: {len(completed)} already completed, {len(pending)} remaining")
    else:
        print(f"Processing {len(pending)} templates")

    print_run_info(
        args.provider,
        args.model,
        args.seed,
        {"Samples per template": str(args.samples_per_template)},
    )

    # Create LLM client
    llm = create_llm(
        provider=args.provider,
        model=args.model,
        temperature=args.temperature,
        seed=args.seed,
    )

    # Process templates
    args.output.parent.mkdir(parents=True, exist_ok=True)
    success_count = 0
    fail_count = 0

    with open(args.output, file_mode) as f:
        for template in tqdm(pending, desc="Generating samples"):
            prompt = format_prompt(prompt_template, template, args.samples_per_template)

            result = generate_with_retries(
                llm=llm,
                prompt=prompt,
                response_model=Samples,
                item_id=template["id"],
                validator=lambda r: bool(r.samples),
            )

            if result:
                # Trim to requested count (LLM may return more than asked)
                result.samples = result.samples[: args.samples_per_template]

                # Post-process: ensure all object_ids and step targets are slugified
                for sample in result.samples:
                    for obj in sample.objects:
                        obj.object_id = slugify(obj.object_id)
                        for peer in obj.peers:
                            peer.object_id = slugify(peer.object_id)
                    for step in sample.steps:
                        step.target = slugify(step.target)

                # Post-process: seed seed_data for read services that left it empty.
                # A read service has no peers and no event_sources but has state_description.
                for sample in result.samples:
                    step_texts = [s.text for s in sample.steps if s.text]
                    for obj in sample.objects:
                        # Only seed read services: they have no peers, no event_sources,
                        # and their behavior doesn't mark them as write services.
                        is_write_service = "do not reply" in (obj.behavior or "").lower()
                        if (
                            not obj.seed_data
                            and not obj.peers
                            and not obj.event_sources
                            and not is_write_service
                            and obj.state_description.strip()
                        ):
                            seeded = _seed_initial_state(llm, obj.state_description, step_texts=step_texts)
                            if seeded:
                                obj.seed_data = seeded

                for sample in result.samples:
                    f.write(sample.model_dump_json() + "\n")
                f.flush()
                success_count += len(result.samples)
            else:
                fail_count += 1

    print()
    print(f"Complete. Output: {args.output}")
    print(f"Samples generated: {success_count}, Templates failed: {fail_count}")
    return args.output


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
