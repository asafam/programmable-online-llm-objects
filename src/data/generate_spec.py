"""
Phase 1b (object-agnostic): GROUND the infused spec.

Runs AFTER state-infusion. Given an infused WorkflowSpec (abstract base scenario with
placeholder entities + state_constraint), this:
  - grounds the template → grounded_steps,
  - writes object-free external-trigger steps,
  - GROUNDS the base scenario too — replacing placeholder entities ("Rep #1") with one
    concrete, consistent cast (Maya Patel, Daniel Kim, …), preserving the invariant logic.

Grounding the steps and the base scenario TOGETHER fixes the entity-count/identity drift:
the cast is decided once here and flows to both the expects and (Phase 2) the mock roster.

Usage:
    python -m src.data.generate_spec -i outputs/.../spec-infused.jsonl -o outputs/.../spec.jsonl
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

load_dotenv()

from src.data.schema import (
    EntityGroundingMap, GroundedTemplate, SpecEventWithExpect, SpecWorkflowSteps, WorkflowSpec,
)
from src.data.llm import create_llm
from src.data.generate_workflows import _ground_template, _GROUND_PROMPT
from src.data.utils import (
    add_common_args, generate_with_retries, infer_provider, load_completed_keys,
    load_jsonl, load_prompt_template, print_run_info, setup_output,
)

_STEPS_SPEC_PROMPT = Path("config/prompts/data-gen/write_steps_spec.yaml")
_GROUND_BE_PROMPT = Path("config/prompts/data-gen/ground_base_events.yaml")


def _template_from_spec(spec: WorkflowSpec) -> dict:
    return {"id": spec.id, "name": spec.name, "domain": spec.domain,
            "source_type": spec.source_type, "link": spec.link,
            "raw_steps": list(spec.template), "template": list(spec.template)}


def _write_steps_spec(llm, grounded: GroundedTemplate, spec_id: str, prompt_cfg: dict) -> SpecWorkflowSteps | None:
    steps_text = "\n".join(f"- {s}" for s in grounded.grounded_steps)
    prompt = prompt_cfg["prompt"].replace("{NAME}", grounded.name).replace("{GROUNDED_STEPS}", steps_text)
    return generate_with_retries(
        llm=llm, prompt=prompt, response_model=SpecWorkflowSteps,
        item_id=f"{spec_id}:spec-steps", validator=lambda r: bool(r.steps),
    )


def _normalize_concurrent_whens(events: list) -> list:
    """Guarantee each concurrent_group shares ONE non-empty `when`, so simultaneous
    arrivals are unambiguous (LLMs sometimes leave the shared timestamp blank)."""
    all_whens = [e.when for e in events if getattr(e, "when", None)]
    fallback = all_whens[-1] if all_whens else "W01-1T12:00"
    groups: dict[str, list] = {}
    for e in events:
        if getattr(e, "concurrent_group", None):
            groups.setdefault(e.concurrent_group, []).append(e)
    for grp in groups.values():
        shared = next((e.when for e in grp if e.when), fallback)
        for e in grp:
            e.when = shared
    return events


_PLACEHOLDER_RE = re.compile(r"#\s?\d")  # leftover "#1", "# 2" → ungrounded placeholder


def _apply_mapping(text: str, pairs: list[tuple[str, str]]) -> str:
    for ph, val in pairs:
        text = text.replace(ph, val)
    return text


def _ground_base_events(llm, grounded: GroundedTemplate, base_events: list, seed: str,
                        prompt_cfg: dict) -> tuple[list, str]:
    """Ground the infused base scenario AND the seed by DETERMINISTIC substitution: the LLM
    returns only a placeholder→concrete map, which we apply in code to inputs, expects, and the
    seed — one consistent cast. This preserves expect SEMANTICS (reset/block/concurrent) and
    keeps the seed roster/catalog consistent with the events. Falls back to ungrounded on failure."""
    if not base_events and not seed:
        return base_events, seed
    # Code-built scenarios already use CONCRETE entities (from the seed) — no placeholders to map,
    # so grounding is a no-op. Only the legacy placeholder flow needs the LLM mapping pass.
    blob = " ".join(f"{e.input} {e.expect.action if e.expect else ''} "
                    f"{e.expect.reason if e.expect and e.expect.reason else ''}" for e in base_events) + " " + (seed or "")
    if not _PLACEHOLDER_RE.search(blob):
        return base_events, seed
    grounded_steps = "\n".join(f"- {s}" for s in grounded.grounded_steps)
    be_text = "\n".join(
        f"{e.id} | input: {e.input}\n   expect: {(e.expect.action if e.expect else '')}"
        for e in base_events
    )
    if seed:
        be_text += f"\n\nSEED (initial reference state — also contains placeholders to map):\n{seed}"
    prompt = (prompt_cfg["prompt"]
              .replace("{NAME}", grounded.name).replace("{DOMAIN}", grounded.domain)
              .replace("{GROUNDED_STEPS}", grounded_steps).replace("{BASE_EVENTS}", be_text))

    def _blobs(pairs):
        out = [f"{e.input} {e.expect.action if e.expect else ''} {e.expect.reason if e.expect and e.expect.reason else ''}"
               for e in base_events]
        if seed:
            out.append(seed)
        return [_apply_mapping(b, pairs) for b in out]

    def _covers_all(m: EntityGroundingMap) -> bool:
        if not m.mappings:
            return False
        pairs = sorted(((e.placeholder, e.value) for e in m.mappings), key=lambda p: -len(p[0]))
        return not any(_PLACEHOLDER_RE.search(b) for b in _blobs(pairs))

    res = generate_with_retries(
        llm=llm, prompt=prompt, response_model=EntityGroundingMap,
        item_id="ground-base-events", validator=_covers_all,
    )
    if not res:
        return base_events, seed
    # longest placeholder first so "Lead #10" is replaced before "Lead #1"
    pairs = sorted(((e.placeholder, e.value) for e in res.mappings), key=lambda p: -len(p[0]))
    for e in base_events:
        e.input = _apply_mapping(e.input, pairs)
        e.role = "base"
        if e.expect:
            e.expect.action = _apply_mapping(e.expect.action, pairs)
            if e.expect.reason:
                e.expect.reason = _apply_mapping(e.expect.reason, pairs)
    return base_events, _apply_mapping(seed, pairs) if seed else seed


def _process_spec(llm, spec: WorkflowSpec, ground_cfg, steps_cfg, ground_be_cfg) -> WorkflowSpec | None:
    # Pass the seed so the grounded steps use the SAME entities/roles/titles as the scenario.
    grounded = _ground_template(llm, _template_from_spec(spec), ground_cfg, seed=spec.seed)
    if not grounded:
        return None
    spec.name = grounded.name
    spec.domain = grounded.domain
    spec.grounded_steps = list(grounded.grounded_steps)
    # Merge the infused invariant INTO the workflow steps as a first-class state-constraint
    # step (object-free), so every downstream consumer — graph derivation (→ custodian),
    # validators, the eval judge — sees it natively. state_constraint stays as structured
    # metadata. (Replaces the transient append previously done in bind_spec.)
    if spec.state_constraint:
        sc = spec.state_constraint
        # Phrase as a NATURAL business rule — annotators rejected the explicit "State
        # constraint" label. The invariant signals in the description still let object
        # identification detect the invariant and create a custodian.
        rule = (sc.description or "").strip()
        if sc.threshold and sc.threshold.lower() not in rule.lower():
            rule = (f"{rule} ({sc.threshold})" if rule else sc.threshold).strip()
        if rule:
            spec.grounded_steps.append(rule)
    # State scenarios drop the external-trigger S-events in binding (the code-built base IS the
    # test), so don't spend a call generating them — and they were a source of seed drift.
    if spec.state_constraint:
        spec.steps = []
    else:
        spec_steps = _write_steps_spec(llm, grounded, spec.id, steps_cfg)
        if not spec_steps:
            return None
        spec.steps = spec_steps.steps
    spec.base_events, spec.seed = _ground_base_events(
        llm, grounded, spec.base_events, spec.seed, ground_be_cfg)
    spec.base_events = _normalize_concurrent_whens(spec.base_events)
    return spec


def default_output_path(input_path: Path) -> Path:
    return input_path.with_name("spec.jsonl")


def run(args: argparse.Namespace) -> Path:
    if args.output is None:
        args.output = default_output_path(args.input)
    if getattr(args, "provider", None) is None:
        args.provider = infer_provider(args.model)
    if args.seed is not None:
        random.seed(args.seed)
    for p in (_STEPS_SPEC_PROMPT, _GROUND_BE_PROMPT, _GROUND_PROMPT):
        if not p.exists():
            print(f"Error: prompt file not found: {p}", file=sys.stderr)
            sys.exit(1)

    specs = load_jsonl(args.input, WorkflowSpec)
    if getattr(args, "ids", None):
        specs = [s for s in specs if s.id in set(args.ids)]
    if getattr(args, "limit", None):
        specs = specs[: args.limit]
    print(f"Loaded {len(specs)} infused specs from {args.input}")

    completed, file_mode = setup_output(
        args.output, args.force,
        lambda: load_completed_keys(args.output, lambda d: d.get("id")),
    )
    pending = [s for s in specs if s.id not in completed]
    if not pending:
        print("All specs already grounded. Use --force to regenerate.")
        return args.output

    ground_cfg = load_prompt_template(_GROUND_PROMPT)
    steps_cfg = load_prompt_template(_STEPS_SPEC_PROMPT)
    ground_be_cfg = load_prompt_template(_GROUND_BE_PROMPT)
    workers = getattr(args, "workers", 1)
    print_run_info(args.provider, args.model, args.seed,
                   {"Phase": "1b — ground (steps + base scenario)", "Workers": str(workers)})
    llm = create_llm(provider=args.provider, model=args.model,
                     temperature=args.temperature, seed=args.seed)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    ok = 0
    fail = 0
    write_lock = threading.Lock()
    with open(args.output, file_mode) as f:
        with tqdm(total=len(pending), desc="Ground") as pbar:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_process_spec, llm, s, ground_cfg, steps_cfg, ground_be_cfg): s for s in pending}
                for fut in as_completed(futs):
                    try:
                        spec = fut.result()
                    except Exception as e:
                        tqdm.write(f"  FAILED {futs[fut].id}: {e}", file=sys.stderr)
                        spec = None
                    if spec is not None:
                        with write_lock:
                            f.write(spec.model_dump_json() + "\n")
                            f.flush()
                        ok += 1
                    else:
                        fail += 1
                    pbar.update(1)
    print(f"\nComplete. Output: {args.output}\nGrounded: {ok}, failed: {fail}")
    return args.output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 1b: ground the infused spec (steps + base scenario).")
    parser.add_argument("--input", "-i", type=Path, required=True, help="Infused spec JSONL (from generate_state_constraints)")
    parser.add_argument("--output", "-o", type=Path, default=None, help="Output grounded spec.jsonl")
    parser.add_argument("--id", dest="ids", metavar="ID", action="append", default=None)
    parser.add_argument("--workers", "-w", type=int, default=1)
    add_common_args(parser)
    return parser


def main():
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
