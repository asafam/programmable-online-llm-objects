"""
Shared utilities for data generation scripts.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Set, Type, TypeVar

import yaml
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def infer_provider(model: str) -> str:
    """Infer the LLM provider from the model name."""
    if model.startswith("claude"):
        return "anthropic"
    elif model.startswith("gpt") or model.startswith("o1") or model.startswith("o3"):
        return "openai"
    elif model.startswith("gemini"):
        return "google"
    else:
        raise ValueError(
            f"Cannot infer provider from model '{model}'. "
            f"Use --provider to specify 'openai', 'azure', 'anthropic', or 'google'."
        )


def load_prompt_template(path: Path) -> dict:
    """Load prompt template from YAML file, returning the full config dict."""
    with open(path, "r") as f:
        return yaml.safe_load(f)


def load_yaml(path: Path) -> list:
    """Load data from YAML file."""
    with open(path, "r") as f:
        return yaml.safe_load(f)


def load_jsonl(path: Path, model_class: Optional[Type[T]] = None) -> list:
    """Load data from JSONL file, optionally parsing into Pydantic models."""
    items = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            if model_class:
                items.append(model_class(**data))
            else:
                items.append(data)
    return items


def load_completed_keys(
    output_path: Path,
    key_extractor: Callable[[dict], Optional[str]],
) -> Set[str]:
    """Load keys of already-generated items for resume support.

    Args:
        output_path: Path to the output JSONL file.
        key_extractor: Function that extracts a unique key from each JSON object.
                       Return None to skip the item.
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
                    key = key_extractor(data)
                    if key:
                        completed.add(key)
                except json.JSONDecodeError:
                    continue
    return completed


def generate_with_retries(
    llm,
    prompt: str,
    response_model: Type[T],
    item_id: str,
    validator: Callable[[T], bool],
    max_retries: int = 3,
) -> Optional[T]:
    """Generate structured output using LLM with retries and exponential backoff.

    Args:
        llm: LLM client instance.
        prompt: Formatted prompt string.
        response_model: Pydantic model class for structured output.
        item_id: Identifier for error reporting.
        validator: Function that validates the result (returns True if valid).
        max_retries: Maximum number of retry attempts.

    Returns:
        Parsed response or None if generation fails.
    """
    from src.data.llm import user_message

    last_error: str = ""
    for attempt in range(max_retries):
        # On retries, append the previous failure reason so the LLM can self-correct
        if attempt > 0 and last_error:
            retry_prompt = (
                prompt
                + f"\n\n---\n\nIMPORTANT: Your previous attempt was rejected.\n"
                f"Reason: {last_error}\n"
                f"Please fix this specific issue and try again."
            )
        else:
            retry_prompt = prompt

        try:
            result = llm.generate_structured(
                messages=[user_message(retry_prompt)],
                response_model=response_model,
            )

            if not validator(result):
                raise ValueError("Validation failed")

            return result

        except Exception as e:
            last_error = str(e)
            if attempt < max_retries - 1:
                wait = 2**attempt  # Exponential backoff: 1s, 2s, 4s
                print(
                    f"  Retry {attempt + 1}/{max_retries} for '{item_id}' "
                    f"in {wait}s: {e}",
                    file=sys.stderr,
                )
                time.sleep(wait)
            else:
                print(
                    f"  Failed '{item_id}' after {max_retries} attempts: {e}",
                    file=sys.stderr,
                )
                return None


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add common CLI arguments shared across generation scripts."""
    parser.add_argument(
        "--provider",
        "-p",
        choices=["openai", "azure", "anthropic", "google"],
        default=None,
        help="LLM provider (inferred from model if not specified). Use 'azure' for Azure OpenAI deployments.",
    )
    parser.add_argument(
        "--model",
        "-m",
        default="claude-sonnet-4-6",
        help="Model name (default: claude-sonnet-4-6). Provider is inferred: claude-* → anthropic, gpt-*/o1-*/o3-* → openai, gemini-* → google. For Azure, use --provider azure with the deployment name.",
    )
    parser.add_argument(
        "--seed",
        "-s",
        type=int,
        default=None,
        help="Random seed for reproducibility",
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
        help="Regenerate all items, ignoring existing output",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=int,
        default=None,
        help="Process only the first N items from the input file",
    )


def validate_paths(input_path: Path, prompt_template_path: Path) -> None:
    """Validate that required input files exist."""
    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    if not prompt_template_path.exists():
        print(
            f"Error: Prompt template not found: {prompt_template_path}",
            file=sys.stderr,
        )
        sys.exit(1)


def setup_output(
    output_path: Path,
    force: bool,
    load_completed: Callable[[], Set[str]],
) -> tuple[Set[str], str]:
    """Setup output file and determine completed items.

    Returns:
        Tuple of (completed_set, file_mode).
    """
    if force:
        if output_path.exists():
            response = input(f"Output file already exists: {output_path}\nOverwrite all? [y/N] ")
            if response.strip().lower() != "y":
                print("Aborted.")
                sys.exit(0)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return set(), "w"
    else:
        completed = load_completed()
        return completed, "a"


def format_tc_event_detail(event_results: list) -> str:
    """Format per-TC pass-rate detail string for eval reporting.

    Returns a parenthesised detail string like:
      (base=3/3 (100%),  pre-mod=2/2 (100%),  post-mod=1/1 (100%))
    or, when base steps are not all passing:
      (base=2/3 (67%),  mod=inconclusive  (pre-mod=1/2 (50%),  post-mod=0/1 (0%)))
    Returns an empty string when there is nothing to report.
    """
    def _fmt_role(role_val) -> Optional[str]:
        evts = [e for e in event_results if getattr(e, "role", None) == role_val]
        if not evts:
            return None
        n_pass = sum(1 for e in evts if e.passed)
        return f"{n_pass}/{len(evts)} ({n_pass/len(evts):.0%})"

    steps_evts = [e for e in event_results if e.event_id.startswith("S")]
    steps_n = sum(1 for e in steps_evts if e.passed)
    steps_total = len(steps_evts)
    steps_100pct = (steps_total == 0) or (steps_n == steps_total)

    detail_parts: list[str] = []
    if steps_total:
        detail_parts.append(f"base={steps_n}/{steps_total} ({steps_n/steps_total:.0%})")

    pre   = _fmt_role("pre_mod")
    post  = _fmt_role("post_mod")
    irrel = _fmt_role("irrelevant")

    if steps_100pct:
        if pre:
            detail_parts.append(f"pre-mod={pre}")
        if post:
            detail_parts.append(f"post-mod={post}")
        if irrel:
            detail_parts.append(f"irrelevant={irrel}")
    elif steps_evts and (pre or post or irrel):
        # Base steps failed → mod rates are indicative only; wrap each in parens
        if pre:
            detail_parts.append(f"(pre-mod={pre})")
        if post:
            detail_parts.append(f"(post-mod={post})")
        if irrel:
            detail_parts.append(f"(irrelevant={irrel})")

    return f"({',  '.join(detail_parts)})" if detail_parts else ""


def print_run_info(
    provider: str,
    model: str,
    seed: Optional[int],
    extra_info: dict[str, str],
) -> None:
    """Print run configuration info. Uses `provider/model` form so the agent,
    planner, and judge lines all share a consistent format."""
    print(f"Agent: {provider}/{model}")
    for key, value in extra_info.items():
        print(f"{key}: {value}")
    if seed is not None:
        print(f"Seed: {seed}")
    print()
