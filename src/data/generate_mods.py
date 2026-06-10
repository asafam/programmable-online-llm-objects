"""
Phase 1: object-free MODIFICATION + EVENT generator.

Enriches a WorkflowSpec with modifications (NL intents, no target) and test events
(pre/post/irrelevant, no recipient). mod_type / ambiguity are SCRIPT-assigned (mirrors
generate_samples), the LLM produces only id/when/intent + events.

Usage:
    python -m src.data.generate_mods -i outputs/.../spec.jsonl --mod-type expansion
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

from src.data.schema import Ambiguity, ModType, SpecScenario, WorkflowSpec
from src.data.generate_samples import AMBIGUITY_DESCRIPTIONS, MODIFICATION_TYPES
from src.data.llm import create_llm
from src.data.utils import (
    add_common_args, generate_with_retries, infer_provider, load_completed_keys,
    load_jsonl, load_prompt_template, print_run_info, setup_output,
)

_PROMPT = Path("config/prompts/data-gen/generate_mods_spec.yaml")


def _format_spec(spec: WorkflowSpec) -> str:
    grounded = "\n".join(f"- {s}" for s in spec.grounded_steps) or "(none)"
    steps = "\n".join(f"- {s.text}" for s in spec.steps) or "(none)"
    base = "\n".join(f"- {e.input}" for e in spec.base_events) or "(none)"
    extra = ""
    if spec.state_constraint:
        extra = f"\n\nState invariant under test: {spec.state_constraint.description} ({spec.state_constraint.threshold})"
    return (f"Name: {spec.name}\nDomain: {spec.domain}\n\nGrounded steps:\n{grounded}\n\n"
            f"External triggers:\n{steps}\n\nState-infused base events:\n{base}{extra}")


def _process_spec(llm, spec: WorkflowSpec, prompt_tmpl: str, args) -> tuple[WorkflowSpec, bool]:
    # Script-assigned type/ambiguity (mirrors generate_samples sampling).
    concrete = list(MODIFICATION_TYPES.keys())
    n = args.mods_per_scenario
    if args.mod_type in (None, "mixed"):
        mod_types = [random.choice(concrete) for _ in range(n)]
    else:
        mod_types = [args.mod_type] * n
    ambiguity = random.choice(list(AMBIGUITY_DESCRIPTIONS.keys())) if args.ambiguity == "random" else args.ambiguity

    # State scenarios: CODE-generate the modification + post-mod events (no LLM, no pre_mod). The
    # mod changes the invariant's threshold; the post-mod events EXERCISE the new rule (with a flip),
    # placed clear of the base events to avoid date/window conflicts.
    if spec.state_constraint and spec.base_events:
        from src.data.generate_state_constraints import VALID_MOD_TYPES, build_mod_scenario
        from src.data.schema import SpecModification
        # mod_type comes from the six taxonomy categories (CLI --mod-type overrides). Sampling is
        # STRATIFIED across the run — pick the least-used valid type so a batch covers the
        # taxonomy instead of randomly repeating one (an independent draw once gave 3 removals).
        ct = spec.state_constraint.type.value
        valid = VALID_MOD_TYPES.get(ct, ["correction"])
        if mod_types[0] in valid and args.mod_type not in (None, "mixed"):
            mt = mod_types[0]
        else:
            used = getattr(args, "_mt_used", None)
            if used is None:
                used = {}
                setattr(args, "_mt_used", used)
            lo = min(used.get(t, 0) for t in valid)
            pool = [t for t in valid if used.get(t, 0) == lo]
            import hashlib
            mt = pool[int(hashlib.md5(spec.id.encode()).hexdigest(), 16) % len(pool)]
            used[mt] = used.get(mt, 0) + 1
        intent, mod_when, post_events = build_mod_scenario(spec, mt)
        spec.modifications = [SpecModification(id="M001", when=mod_when, intent=intent,
                                               mod_type=ModType(mt), ambiguity=Ambiguity(ambiguity))]
        spec.events = post_events
        return spec, True

    mt_label = ", ".join(mod_types)
    mt_desc = "\n".join(f"- {mt}: {MODIFICATION_TYPES[mt]}" for mt in mod_types)

    prompt = (prompt_tmpl
        .replace("{SPEC}", _format_spec(spec))
        .replace("{SCENARIO_COUNT}", "1")
        .replace("{MODS_PER_SCENARIO}", str(n))
        .replace("{MODIFICATION_TYPE}", mt_label)
        .replace("{MODIFICATION_TYPE_DESCRIPTION}", mt_desc)
        .replace("{EVENTS_BEFORE_COUNT}", str(args.events_before))
        .replace("{EVENTS_AFTER_COUNT}", str(args.events_after))
        .replace("{EVENTS_UNRELATED_COUNT}", str(args.events_unrelated)))

    from src.data.schema import WorkflowSpecScenarios
    res = generate_with_retries(
        llm=llm, prompt=prompt, response_model=WorkflowSpecScenarios,
        item_id=f"{spec.id}-mods",
        # Require a real, non-empty modification set (and at least one event to exercise it)
        # — an empty modifications list previously slipped through and produced no mod.
        validator=lambda r: bool(r.scenarios)
        and len(r.scenarios[0].modifications) >= n
        and bool(r.scenarios[0].events),
    )
    if not res:
        return spec, False
    sc = res.scenarios[0]
    # assign script-controlled fields
    for gm, mt in zip(sc.modifications, mod_types):
        gm.mod_type = ModType(mt)
        gm.ambiguity = Ambiguity(ambiguity)
    spec.modifications = sc.modifications
    spec.events = sc.events
    return spec, True


def default_output_path(input_path: Path) -> Path:
    return input_path.with_name("spec-mods.jsonl")


def run(args: argparse.Namespace) -> Path:
    if args.output is None:
        args.output = default_output_path(args.input)
    if getattr(args, "provider", None) is None:
        args.provider = infer_provider(args.model)
    if args.seed is not None:
        random.seed(args.seed)
    if args.events_unrelated is None:
        args.events_unrelated = args.mods_per_scenario

    specs = load_jsonl(args.input, WorkflowSpec)
    if getattr(args, "ids", None):
        specs = [s for s in specs if s.id in set(args.ids)]
    if getattr(args, "limit", None):
        specs = specs[: args.limit]
    print(f"Loaded {len(specs)} specs from {args.input}")

    completed, file_mode = setup_output(
        args.output, args.force,
        lambda: load_completed_keys(args.output, lambda d: d.get("id")),
    )
    pending = [s for s in specs if s.id not in completed]
    if not pending:
        print("All specs already have modifications. Use --force to regenerate.")
        return args.output

    prompt_tmpl = load_prompt_template(_PROMPT)["user_prompt"]
    workers = getattr(args, "workers", 1)
    print_run_info(args.provider, args.model, args.seed,
                   {"Phase": "1 — modifications (object-agnostic)", "mod-type": args.mod_type or "mixed", "Workers": str(workers)})
    llm = create_llm(provider=args.provider, model=args.model,
                     temperature=args.temperature, seed=args.seed)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    ok = 0
    fail = 0
    # State scenarios are code-generated and DETERMINISTIC — process them SEQUENTIALLY (the parallel
    # pool intermittently dropped one sample's mod). Only the LLM-path specs need the thread pool.
    state_pending = [s for s in pending if s.state_constraint and s.base_events]
    llm_pending = [s for s in pending if not (s.state_constraint and s.base_events)]
    with open(args.output, file_mode) as f:
        with tqdm(total=len(pending), desc="Modifications") as pbar:
            for s in state_pending:
                try:
                    spec, okk = _process_spec(llm, s, prompt_tmpl, args)
                except Exception as e:
                    tqdm.write(f"  FAILED {s.id}: {e}", file=sys.stderr)
                    spec, okk = s, False
                # GUARANTEE the (deterministic) state mod — defends against an observed intermittent
                # miss where a state spec slipped through with no modification.
                if not spec.modifications and spec.state_constraint and spec.base_events:
                    try:
                        from src.data.generate_state_constraints import VALID_MOD_TYPES, build_mod_scenario
                        from src.data.schema import SpecModification
                        valid = VALID_MOD_TYPES.get(spec.state_constraint.type.value, ["correction"])
                        mt = args.mod_type if args.mod_type in valid else random.choice(valid)
                        amb = random.choice(list(AMBIGUITY_DESCRIPTIONS.keys())) if args.ambiguity == "random" else args.ambiguity
                        intent, mod_when, post = build_mod_scenario(spec, mt)
                        spec.modifications = [SpecModification(id="M001", when=mod_when, intent=intent,
                                                              mod_type=ModType(mt), ambiguity=Ambiguity(amb))]
                        spec.events = post
                        okk = True
                        tqdm.write(f"  [force-mod] {spec.id}: state mod was missing — regenerated", file=sys.stderr)
                    except Exception as e:
                        tqdm.write(f"  FORCE-MOD FAILED {s.id}: {e}", file=sys.stderr)
                f.write(spec.model_dump_json() + "\n"); f.flush()
                ok += int(okk); fail += int(not okk); pbar.update(1)
            if llm_pending:
                write_lock = threading.Lock()
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = {ex.submit(_process_spec, llm, s, prompt_tmpl, args): s for s in llm_pending}
                    for fut in as_completed(futs):
                        s = futs[fut]
                        try:
                            spec, okk = fut.result()
                        except Exception as e:
                            tqdm.write(f"  FAILED {s.id}: {e}", file=sys.stderr)
                            spec, okk = s, False
                        with write_lock:
                            f.write(spec.model_dump_json() + "\n")
                            f.flush()
                        ok += int(okk); fail += int(not okk)
                        pbar.update(1)
    print(f"\nComplete. Output: {args.output}\nModifications added: {ok}, failed: {fail}")
    return args.output


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase 1: add object-free modifications + events to a WorkflowSpec.")
    p.add_argument("--input", "-i", type=Path, required=True)
    p.add_argument("--output", "-o", type=Path, default=None)
    p.add_argument("--mod-type", type=str, choices=list(MODIFICATION_TYPES.keys()) + ["mixed"], default=None)
    p.add_argument("--mods-per-scenario", type=int, default=1)
    p.add_argument("--ambiguity", type=str, choices=list(AMBIGUITY_DESCRIPTIONS.keys()) + ["random"], default="random")
    p.add_argument("--events-before", type=int, default=0)
    p.add_argument("--events-after", type=int, default=2)
    p.add_argument("--events-unrelated", type=int, default=None)
    p.add_argument("--id", dest="ids", metavar="ID", action="append", default=None)
    p.add_argument("--workers", "-w", type=int, default=1)
    add_common_args(p)
    return p


def main():
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
