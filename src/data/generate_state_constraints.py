"""
Stage 1.5: State-infused base-scenario generator (implementation-agnostic).

A workflow may enforce a cross-request invariant — a cap, quota, running total,
rate-limit, shared pool, or round-robin queue — where the correct decision depends on
accumulated state, not just the current request. This stage authors the BASE SCENARIO
for that invariant: a role="base" event sequence (with expects) that crosses the
threshold, so the state-infused behavior is testable. The base events precede any
modifications generated in Stage 2.

DECOUPLED FROM IMPLEMENTATION. This stage knows nothing about HOW the invariant is
realized (a single-writer Custodian or otherwise — that is decided in object
identification, Stage 1). It reads the workflow's objects/steps to learn the rule and
threshold, then writes a base scenario whose expectations are stated in OBSERVABLE
terms (a request is admitted vs blocked/held/escalated at the threshold), never in
internal mechanics. The same base scenario is the spec any implementation must satisfy.

Usage:
    python -m src.data.generate_state_constraints \\
        --input outputs/data/zapier/<run>/workflows.jsonl \\
        --model gpt-5.4 --provider azure
"""
from __future__ import annotations

import argparse
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

from src.data.schema import (
    GeneratedStateConstraint,
    SpecEventWithExpect,
    StateConstraint,
    WorkflowSpec,
)
from src.data.llm import create_llm
from src.data.utils import (
    add_common_args,
    generate_with_retries,
    infer_provider,
    load_completed_keys,
    load_prompt_template,
    load_yaml,
    print_run_info,
    setup_output,
)

_PROMPT = Path("config/prompts/data-gen/generate_state_constraints.yaml")

# Shared deterministic scenario-coherence terms (used by the infuse validator here AND by
# pipeline_v2's pre-upload check) — keep them in one place so they can't drift.
_BLOCK_TERMS = (
    "not approved", "no new reorder", "no reorder", "not sent", "not assigned", "blocked",
    "is held", "held unassigned", "suppress", "denied", "remains pending", "escalat",
    "not completed", "is not approved", "no approval",
)
_RESET_TERMS = (
    "new day", "resets", "reset", "no longer", "more than 7", "more than seven", "next day",
    "8 day", "eight day", "aged out", "outside the window", "per-day count", "window no longer",
)


import re as _re
_KEYTOK = _re.compile(r"#\d+|[A-Za-z0-9][A-Za-z0-9-]{3,}")


def _is_blocked(text: str) -> bool:
    return any(b in text.lower() for b in _BLOCK_TERMS)


def _valid_seed(s: str) -> bool:
    """The seed must be a non-empty JSON object (it becomes the read-service mock data verbatim)."""
    import json as _json
    try:
        return isinstance(_json.loads(s), dict) and bool(_json.loads(s))
    except Exception:
        return False


def concurrent_pair_issues(events: list[dict], threshold: str) -> list[str]:
    """The simultaneous pair only RACES if its key already has exactly (limit-1) accepted
    same-key requests in-window — then the two arrivals compete for the last slot (one
    accepted, one blocked). A pair on a FRESH key (0 prior) is wrong: both are within the
    limit, neither should block. events: dicts {input, when, action, reason, concurrent_group}.
    Conservative — returns [] when it can't confidently identify the key/limit."""
    pair = [e for e in events if e.get("concurrent_group")]
    if len(pair) < 2:
        return []
    m = _re.search(r"\d+", threshold or "")
    if not m:
        return []
    limit = int(m.group())
    if limit < 1:
        return []
    # key = distinctive tokens shared by BOTH pair inputs but NOT by every event (drops generic
    # words like "SKU"/"Inventory"/"lead" common everywhere, leaving the actual key code).
    # key = CODE-like tokens (id/SKU/placeholder) shared by BOTH pair inputs. Keep code-like
    # only (drops generic lowercase words); do NOT subtract tokens common to all events — for a
    # single-key scenario the key IS in every event, and subtracting it loses the key entirely.
    pair_keys = set.intersection(*[set(_KEYTOK.findall(e["input"])) for e in pair])
    key = {k for k in pair_keys
           if any(c.isdigit() for c in k) or k.isupper() or k.startswith("#")}
    if not key:
        return []
    pair_when = min((e["when"] for e in pair if e.get("when")), default="")
    prior = sum(1 for e in events
                if not e.get("concurrent_group")
                and (not e.get("when") or e["when"] < pair_when)
                and any(k in e["input"] for k in key)
                and not _is_blocked(f"{e.get('action', '')} {e.get('reason', '')}"))
    if prior != limit - 1:
        return [f"concurrent pair (key {sorted(key)[:2]}) has {prior} prior accepted same-key "
                f"request(s); needs limit-1={limit - 1} so the pair races for the last slot "
                f"(else one is wrongly blocked/allowed)"]
    return []


def coherence_issues(expect_texts: list[str], constraint_type: str) -> list[str]:
    """A state scenario is valid only if its base expects actually exercise the invariant:
    at least one BLOCKS the gated action, and (for counter/rate_limit) one shows the period
    RESET. Returns a list of issue strings; empty means coherent."""
    texts = [t.lower() for t in expect_texts if t]
    if not texts:
        return []
    issues = []
    if not any(any(b in x for b in _BLOCK_TERMS) for x in texts):
        issues.append("no base event blocks/holds/suppresses the gated action (invariant never exercised)")
    if constraint_type in ("counter", "rate_limit"):
        if not any(any(rt in x for rt in _RESET_TERMS) for x in texts):
            issues.append(f"{constraint_type}: no reset/window-expiry event (same key, later period, allowed)")
    return issues

# Retained for the pipeline opt-in flag's choices; the type is now CLASSIFIED by
# the LLM from the custodian's invariant, not script-assigned.
CONSTRAINT_TYPES = ["cap", "counter", "rate_limit"]


def format_template(template: dict) -> str:
    """Raw-template view (PRE-grounding): the abstract steps reveal the invariant."""
    steps = "\n".join(f"- {s}" for s in (template.get("template") or template.get("raw_steps", []))) or "(none)"
    return (
        f"ID: {template['id']}\nName: {template['name']}\n"
        f"Domain: {template.get('domain', 'general')}\n\nTemplate steps:\n{steps}"
    )


def _seed_spec(template: dict) -> WorkflowSpec:
    return WorkflowSpec(
        id=template["id"], name=template["name"], domain=template.get("domain", "general"),
        source_type=template["source_type"], link=template.get("link") or template.get("seed_utterance", ""),
        template=list(template.get("template") or template.get("raw_steps", [])),
    )


def _process_template(llm, template: dict, prompt_template: str) -> tuple[WorkflowSpec, bool]:
    """INFUSE FIRST (before grounding): read the RAW template to learn the invariant and
    author the abstract base scenario. Entities are PLACEHOLDERS (grounded later, together
    with the steps, so the entity set is concretized once); only the numbers/logic are
    concrete here. Returns (spec, ok). Always returns a spec skeleton; ok=False when no
    cross-request invariant exists (spec carries no base events)."""
    spec = _seed_spec(template)
    prompt = prompt_template.replace("{WORKFLOW}", format_template(template))
    gen = generate_with_retries(
        llm=llm, prompt=prompt, response_model=GeneratedStateConstraint,
        item_id=template["id"],
        # Require: base events + threshold, a concurrent pair (>=2 sharing a concurrent_group),
        # AND scenario coherence — at least one BLOCKED action, plus a RESET event for
        # counter/rate_limit. Retries until the LLM actually exercises the invariant + reset.
        validator=lambda r: bool(r.base_events) and bool(r.threshold)
        and _valid_seed(r.seed)
        and sum(1 for e in r.base_events if e.concurrent_group) >= 2
        and not coherence_issues(
            [f"{e.expect.action} {e.expect.reason or ''}" for e in r.base_events if e.expect],
            getattr(r.constraint_type, "value", r.constraint_type))
        and not concurrent_pair_issues(
            [{"input": e.input, "when": e.when or "",
              "action": e.expect.action if e.expect else "",
              "reason": (e.expect.reason if e.expect else "") or "",
              "concurrent_group": e.concurrent_group} for e in r.base_events],
            r.threshold or ""),
    )
    if gen is None:
        return spec, False

    base_events: list[SpecEventWithExpect] = []
    for ge in gen.base_events:
        d = ge.model_dump()
        d["role"] = "base"
        base_events.append(SpecEventWithExpect(**d))
    spec.base_events = base_events
    spec.seed = gen.seed
    spec.state_constraint = StateConstraint(
        type=gen.constraint_type, threshold=gen.threshold, description=gen.description,
    )
    return spec, True


def default_output_path(input_path: Path) -> Path:
    return Path("outputs/data/zapier") / f"{input_path.stem}_spec-infused.jsonl"


def run(args: argparse.Namespace) -> Path:
    if args.output is None:
        args.output = default_output_path(args.input)
    if getattr(args, "provider", None) is None:
        args.provider = infer_provider(args.model)
    if args.seed is not None:
        random.seed(args.seed)

    if not _PROMPT.exists():
        print(f"Error: prompt file not found: {_PROMPT}", file=sys.stderr)
        sys.exit(1)

    templates = load_yaml(args.input)
    if getattr(args, "ids", None):
        id_set = set(args.ids)
        templates = [t for t in templates if t["id"] in id_set]
    if getattr(args, "limit", None):
        templates = templates[: args.limit]
    print(f"Loaded {len(templates)} templates from {args.input}")

    completed, file_mode = setup_output(
        args.output,
        args.force,
        lambda: load_completed_keys(args.output, lambda d: d.get("id")),
    )
    pending = [t for t in templates if t["id"] not in completed]
    if not pending:
        print("All templates already infused. Use --force to regenerate.")
        return args.output
    if completed:
        print(f"Resuming: {len(completed)} done, {len(pending)} remaining")

    workers = getattr(args, "workers", 1)
    print_run_info(args.provider, args.model, args.seed,
                   {"Phase": "1a — infuse state (PRE-grounding, object-agnostic)", "Workers": str(workers)})
    llm = create_llm(provider=args.provider, model=args.model,
                     temperature=args.temperature, seed=args.seed)
    prompt_template = load_prompt_template(_PROMPT)["user_prompt"]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    ok_count = 0
    skip_count = 0
    write_lock = threading.Lock()

    with open(args.output, file_mode) as f:
        with tqdm(total=len(pending), desc="Infuse state") as pbar:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(_process_template, llm, t, prompt_template): t
                    for t in pending
                }
                for future in as_completed(futures):
                    try:
                        spec, ok = future.result()
                    except Exception as e:
                        spec = _seed_spec(futures[future])
                        ok = False
                        tqdm.write(f"  FAILED {spec.id}: {e}", file=sys.stderr)
                    with write_lock:
                        f.write(spec.model_dump_json() + "\n")
                        f.flush()
                    ok_count += int(ok)
                    skip_count += int(not ok)
                    pbar.update(1)

    print()
    print(f"Complete. Output: {args.output}")
    print(f"Base scenarios authored: {ok_count}, no invariant / skeleton only: {skip_count}")
    return args.output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Phase 1a: infuse state from raw templates (PRE-grounding) into a spec skeleton.",
    )
    parser.add_argument("--input", "-i", type=Path, required=True,
                        help="Raw templates YAML")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Output infused spec JSONL")
    parser.add_argument("--id", dest="ids", metavar="ID", action="append", default=None,
                        help="Only process these template id(s); repeatable")
    parser.add_argument("--workers", "-w", type=int, default=1, help="Parallel workers")
    add_common_args(parser)
    return parser


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
