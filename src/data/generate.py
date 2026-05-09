"""
Test case generator for live NL programming.

Generates test cases with modifications and events for raw Zapier automation
examples using LLM-based generation.

Usage:
    python -m src.data.generate \\
        --input data/zapier/raw/examples.yaml \\
        --output outputs/data/zapier/generated/test_cases.jsonl \\
        --model claude-sonnet-4-5-20250929 \\
        --seed 42 \\
        --test-cases 5-10 \\
        --events-per-test-case 2-5
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Optional, Set, Tuple

import yaml
from dotenv import load_dotenv
from tqdm import tqdm

# Load environment variables from .env file
load_dotenv()

from src.data.schema import TestCases
from src.data.llm import create_llm, user_message


def parse_range(value: str) -> Tuple[int, int]:
    """Parse a range specification like '5' or '3-8' into (min, max) tuple."""
    if "-" in value:
        parts = value.split("-")
        return int(parts[0]), int(parts[1])
    else:
        n = int(value)
        return n, n


def infer_provider(model: str) -> str:
    """Infer the LLM provider from the model name."""
    if model.startswith("claude"):
        return "anthropic"
    elif model.startswith("gpt") or model.startswith("o1") or model.startswith("o3"):
        return "openai"
    else:
        raise ValueError(
            f"Cannot infer provider from model '{model}'. "
            f"Use --provider to specify 'openai', 'azure', or 'anthropic'."
        )


def load_prompt_template(path: Path) -> str:
    """Load prompt template from YAML file."""
    with open(path, "r") as f:
        config = yaml.safe_load(f)
    return config.get("user_prompt", "")


def load_raw_examples(path: Path) -> list:
    """Load raw examples from YAML file."""
    with open(path, "r") as f:
        return yaml.safe_load(f)


def load_completed_links(output_path: Path) -> Set[str]:
    """Load links of already-generated examples for resume support.

    Uses the 'link' field from test cases to identify which source examples
    have already been processed.
    """
    completed = set()
    if output_path.exists():
        with open(output_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if "link" in data:
                        completed.add(data["link"])
                except json.JSONDecodeError:
                    continue
    return completed


def format_base_specification(example: dict) -> str:
    """Format the BASE_SPECIFICATION from a raw example."""
    steps = "\n".join(f"- {step}" for step in example["raw_steps"])
    return f"""ID: {example['id']}
Name: {example['name']}
Source: {example['source_type']}
Link: {example['link']}

Steps:
{steps}"""


def format_prompt(
    template: str, example: dict, test_case_count: int, events_per_test_case: int
) -> str:
    """Format prompt template with example data and parameters."""
    base_spec = format_base_specification(example)
    return template.format(
        BASE_SPECIFICATION=base_spec,
        TEST_CASE_COUNT=test_case_count,
        EVENTS_PER_TEST_CASE=events_per_test_case,
    )


def generate_test_cases(
    llm,
    prompt: str,
    example: dict,
    max_retries: int = 3,
) -> Optional[TestCases]:
    """Generate test cases using LLM with retries and exponential backoff.

    Args:
        llm: LLM client instance.
        prompt: Formatted prompt string.
        example: Raw example dict (for error reporting).
        max_retries: Maximum number of retry attempts.

    Returns:
        TestCases instance or None if generation fails.
    """
    for attempt in range(max_retries):
        try:
            result = llm.generate_structured(
                messages=[user_message(prompt)],
                response_model=TestCases,
            )

            # Validate non-empty test cases
            if not result.test_cases:
                raise ValueError("Empty test_cases generated")

            return result

        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2**attempt  # Exponential backoff: 1s, 2s, 4s
                print(
                    f"  Retry {attempt + 1}/{max_retries} for '{example['id']}' "
                    f"in {wait}s: {e}",
                    file=sys.stderr,
                )
                time.sleep(wait)
            else:
                print(
                    f"  Failed '{example['id']}' after {max_retries} attempts: {e}",
                    file=sys.stderr,
                )
                return None


def main():
    parser = argparse.ArgumentParser(
        description="Generate test cases from raw Zapier automation examples",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate with default model (provider inferred from model)
  python -m src.data.generate -i data/zapier/raw/examples.yaml

  # Generate with OpenAI
  python -m src.data.generate -i data/zapier/raw/examples.yaml --model gpt-4o

  # Generate with Anthropic Opus
  python -m src.data.generate -i data/zapier/raw/examples.yaml --model claude-opus-4-5-20251101

  # Custom ranges and seed
  python -m src.data.generate -i data/zapier/raw/examples.yaml --seed 42 --test-cases 8-12 --events-per-test-case 3-5
""",
    )

    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        required=True,
        help="Path to raw examples YAML file",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("outputs/data/zapier/generated/test_cases.jsonl"),
        help="Output JSONL path (default: outputs/data/zapier/generated/test_cases.jsonl)",
    )
    parser.add_argument(
        "--prompt-template",
        type=Path,
        default=Path("config/prompts/data-gen/generate_samples.yaml"),
        help="Path to prompt template (default: config/prompts/data-gen/generate.yaml)",
    )
    parser.add_argument(
        "--provider",
        "-p",
        choices=["openai", "anthropic"],
        default=None,
        help="LLM provider (inferred from model if not specified)",
    )
    parser.add_argument(
        "--model",
        "-m",
        default="claude-sonnet-4-6",
        help="Model name (default: claude-sonnet-4-6). Provider is inferred: claude-* → anthropic, gpt-*/o1-*/o3-* → openai",
    )
    parser.add_argument(
        "--seed",
        "-s",
        type=int,
        default=None,
        help="Random seed for reproducibility",
    )
    parser.add_argument(
        "--test-cases",
        "-t",
        default="5",
        help="Number of test cases per example (single value or range like '5-10')",
    )
    parser.add_argument(
        "--events-per-test-case",
        "-e",
        default="3",
        help="Number of events per test case (single value or range like '2-5')",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.7,
        help="LLM temperature (default: 0.7)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate all examples, ignoring existing output",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue processing after failures instead of stopping",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=int,
        default=None,
        help="Process only the first N examples from the input file",
    )

    args = parser.parse_args()

    # Infer provider from model if not specified
    if args.provider is None:
        args.provider = infer_provider(args.model)

    # Initialize random seed
    if args.seed is not None:
        random.seed(args.seed)

    # Parse ranges
    tc_min, tc_max = parse_range(args.test_cases)
    events_min, events_max = parse_range(args.events_per_test_case)

    # Load inputs
    if not args.input.exists():
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    if not args.prompt_template.exists():
        print(
            f"Error: Prompt template not found: {args.prompt_template}",
            file=sys.stderr,
        )
        sys.exit(1)

    examples = load_raw_examples(args.input)
    template = load_prompt_template(args.prompt_template)

    # Apply limit if specified (0 or None means no limit)
    if args.limit:
        examples = examples[: args.limit]

    print(f"Loaded {len(examples)} examples from {args.input}")

    # Determine which examples to process
    if args.force:
        completed = set()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        file_mode = "w"
    else:
        completed = load_completed_links(args.output)
        file_mode = "a"

    pending = [e for e in examples if e["link"] not in completed]

    if not pending:
        print("All examples already generated. Use --force to regenerate.")
        return

    if completed:
        print(f"Resuming: {len(completed)} already completed, {len(pending)} remaining")
    else:
        print(f"Processing {len(pending)} examples")

    print(f"Provider: {args.provider}, Model: {args.model}")
    print(f"Test cases: {tc_min}-{tc_max}, Events per test case: {events_min}-{events_max}")
    if args.seed is not None:
        print(f"Seed: {args.seed}")
    print()

    # Create LLM client
    llm = create_llm(
        provider=args.provider,
        model=args.model,
        temperature=args.temperature,
        seed=args.seed,
    )

    # Process examples
    args.output.parent.mkdir(parents=True, exist_ok=True)
    success_count = 0
    fail_count = 0

    with open(args.output, file_mode) as f:
        for example in tqdm(pending, desc="Generating"):
            # Sample counts for this example
            test_case_count = random.randint(tc_min, tc_max)
            events_per_tc = random.randint(events_min, events_max)

            # Format prompt
            prompt = format_prompt(template, example, test_case_count, events_per_tc)

            # Generate test cases
            result = generate_test_cases(llm, prompt, example)

            if result:
                # Write each test case as a separate line
                for test_case in result.test_cases:
                    f.write(test_case.model_dump_json() + "\n")
                f.flush()
                success_count += len(result.test_cases)
            else:
                fail_count += 1
                if not args.continue_on_error:
                    print(
                        f"\nStopping due to failure on '{example['id']}'. "
                        f"Use --continue-on-error to skip failed examples.",
                        file=sys.stderr,
                    )
                    sys.exit(1)

    print()
    print(f"Complete. Output: {args.output}")
    print(f"Test cases generated: {success_count}, Examples failed: {fail_count}")


if __name__ == "__main__":
    main()
