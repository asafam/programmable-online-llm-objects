"""
Phase 2 (binding): turn an object-agnostic WorkflowSpec + a derived agent graph into
the unified, eval-compatible Sample.

Steps per spec:
  1. derive agent graph (reuse generate_workflows._identify_objects)
  2. bind event recipients (sole entry-point / input-text match)
  3. bind modification targets (single business-object fallback / LLM map)
  4. generate mock tools (fed the spec's event/step texts — entity consistency)
  5. rewrite object-specific expects (reuse generate_samples._rewrite_event_expectations)
  6. assemble the unified Sample

The bound output uses the UNCHANGED Sample/Event/Modification models, so evaluate.py is
unaffected. The pure binding/assembly helpers below are unit-testable without an LLM.
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
    Event, EventExpect, EventExpectations, GroundedTemplate, Modification,
    ModTargetBindings, ObjectDef, ObjectGraph, Sample, WorkflowSpec,
)
from src.data.llm import create_llm
from src.data.generate_workflows import (
    _identify_objects, _add_mock_tools, _OBJECTS_PROMPT,
)
from src.data.generate_samples import _rewrite_event_expectations
from src.data.utils import (
    add_common_args, generate_with_retries, infer_provider, load_completed_keys,
    load_jsonl, load_prompt_template, print_run_info, setup_output,
)
from src.lnl.parser import slugify

_BIND_MODS_PROMPT = Path("config/prompts/data-gen/bind_modifications.yaml")
_GROUND_STATE_PROMPT = Path("config/prompts/data-gen/ground_state_expects.yaml")


def _ground_state_base_expects(llm, sample: Sample) -> None:
    """Ground the state-infused base-event expects against the read-service mock roster,
    substituting placeholder entities ('Rep A/B/C') with real names while preserving the
    invariant (ordering / cap / count) reasoning. No-op when there's no state constraint,
    no base expects, or no read-service mock data."""
    if not sample.state_constraint:
        return
    base = [e for e in sample.events if e.role == "base" and e.expect]
    roster = "\n\n".join(f"Tool: {t.tool_name}\n{t.response_template[:2500]}"
                         for t in sample.tools if "_data" in t.tool_name.lower())
    if not base or not roster.strip():
        return
    base_lines = "\n".join(f"{e.id} | input: {e.input}\n   current expect: {e.expect.action}" for e in base)
    prompt = (load_prompt_template(_GROUND_STATE_PROMPT)["prompt"]
              .replace("{ROSTER}", roster).replace("{BASE_EVENTS}", base_lines))
    res = generate_with_retries(
        llm=llm, prompt=prompt, response_model=EventExpectations,
        item_id=f"{sample.id}-ground-state", validator=lambda r: bool(r.expectations),
    )
    if not res:
        return
    by_id = {i.event_id: i for i in res.expectations}
    for e in base:
        if e.id in by_id:
            e.expect = EventExpect(action=by_id[e.id].action, reason=by_id[e.id].reason)

# Read-service objects answer lookups via a `{object_id}_data` tool; they are not the
# target of a behavior modification.
def _is_read_service(obj: ObjectDef) -> bool:
    return "_data` tool" in (obj.behavior or "") or "_data tool" in (obj.behavior or "")


def _entry_points(graph: ObjectGraph) -> list[ObjectDef]:
    return [o for o in graph.objects if o.event_sources]


def _business_objects(graph: ObjectGraph) -> list[ObjectDef]:
    return [o for o in graph.objects if not o.event_sources and not _is_read_service(o)]


def _toks(s: str) -> set[str]:
    return {tok for tok in s.lower().replace("-", " ").replace("_", " ").split() if len(tok) > 3}


def _bind_recipient(text: str, entries: list[ObjectDef], llm=None) -> str | None:
    """Pick the entry-point object that receives an external stimulus.
    Sole entry → trivial. Else score the stimulus against each entry's DISTINCTIVE
    tokens (unique among entries), trying the most platform-specific source first:
    object_id, then event_sources. Falls back to first entry on a true tie."""
    if not entries:
        return None
    if len(entries) == 1:
        return entries[0].object_id
    t = (text or "").lower()
    fields = [
        [_toks(o.object_id) for o in entries],
        [_toks(" ".join(o.event_sources)) for o in entries],
    ]
    for toks in fields:
        shared = set.intersection(*toks) if toks else set()
        distinctive = [tk - shared for tk in toks]
        scores = [sum(1 for tok in d if tok in t) for d in distinctive]
        top = max(scores)
        if top > 0 and scores.count(top) == 1:   # unambiguous winner only
            return entries[scores.index(top)].object_id
    return entries[0].object_id


def _bind_mod_target(intent: str, business: list[ObjectDef], mapping: dict | None, mod_id: str) -> str | None:
    """Target for a modification. LLM `mapping` (mod_id→object_id) wins; else the sole
    business object; else None (flag)."""
    if mapping and mod_id in mapping:
        return mapping[mod_id]
    if len(business) == 1:
        return business[0].object_id
    return None


def _format_graph_for_binding(graph: ObjectGraph) -> str:
    lines = []
    for o in graph.objects:
        lines.append(f"- {o.object_id} ({o.role})")
        lines.append(f"    behavior: {(o.behavior or '')[:300]}")
    return "\n".join(lines)


def llm_bind_mod_targets(llm, graph: ObjectGraph, modifications, prompt_tmpl: str) -> dict[str, str]:
    """LLM map modification intents → object_ids. Returns {mod_id: object_id}; only
    entries whose object_id is a real object are kept (assembly validates the rest)."""
    if not modifications:
        return {}
    mods_text = "\n".join(f"- {m.id}: {m.intent}" for m in modifications)
    prompt = (prompt_tmpl
              .replace("{OBJECTS}", _format_graph_for_binding(graph))
              .replace("{MODIFICATIONS}", mods_text))
    obj_ids = {o.object_id for o in graph.objects}
    res = generate_with_retries(
        llm=llm, prompt=prompt, response_model=ModTargetBindings,
        item_id="bind-mods",
        validator=lambda r: bool(r.bindings) and all(b.object_id in obj_ids for b in r.bindings),
    )
    if not res:
        return {}
    return {b.mod_id: b.object_id for b in res.bindings if b.object_id in obj_ids}


def _spec_event_to_event(se, recipient: str, *, role_override: str | None = None) -> Event:
    d = se.model_dump()
    d.pop("recipient", None)
    if role_override:
        d["role"] = role_override
    return Event(recipient=recipient, **d)


def assemble_sample(spec: WorkflowSpec, graph: ObjectGraph, *, mod_mapping: dict | None = None) -> Sample:
    """Pure binding + assembly: bind recipients/targets and build the unified Sample.
    Raises ValueError if a recipient/target cannot be resolved to an object_id."""
    for obj in graph.objects:
        obj.object_id = slugify(obj.object_id)
        for p in obj.peers:
            p.object_id = slugify(p.object_id)
    obj_ids = {o.object_id for o in graph.objects}
    entries = _entry_points(graph)
    business = _business_objects(graph)

    events: list[Event] = []
    # external-trigger steps → base events (S###)
    for i, s in enumerate(spec.steps, 1):
        rcpt = _bind_recipient(s.text, entries)
        events.append(Event(id=f"S{i:03d}", call_type="send", source=s.source,
                            recipient=rcpt, input=s.text, when="W00-1T00:00",
                            expect=s.expect, role="base"))
    # state-infused base scenario (SC###) — preserve cumulative expects
    sc_remap = {}
    for i, se in enumerate(spec.base_events, 1):
        new_id = f"SC{i:03d}"
        sc_remap[se.id] = new_id
        rcpt = _bind_recipient(se.input, entries)
        e = _spec_event_to_event(se, rcpt, role_override="base")
        e.id = new_id
        e.triggered_by = sc_remap.get(e.triggered_by, e.triggered_by) if isinstance(e.triggered_by, str) else e.triggered_by
        e.depends_on = [sc_remap.get(d, d) for d in e.depends_on]
        events.append(e)
    # mod/pre/post/irrelevant events (E###)
    for se in spec.events:
        rcpt = _bind_recipient(se.input, entries)
        events.append(_spec_event_to_event(se, rcpt))

    modifications: list[Modification] = []
    for m in spec.modifications:
        target = _bind_mod_target(m.intent, business, mod_mapping, m.id)
        if target is None:
            raise ValueError(f"{spec.id}: cannot bind modification {m.id} target "
                             f"(business objects: {[o.object_id for o in business]})")
        modifications.append(Modification(
            id=m.id, call_type=m.call_type, source=m.source, target=target,
            when=m.when, intent=m.intent, mod_type=m.mod_type, ambiguity=m.ambiguity,
        ))

    # Membership validation (relocated entry-point/target check)
    for e in events:
        if e.recipient not in obj_ids:
            raise ValueError(f"{spec.id}: event {e.id} recipient '{e.recipient}' ∉ objects")
    for m in modifications:
        if m.target not in obj_ids:
            raise ValueError(f"{spec.id}: modification {m.id} target '{m.target}' ∉ objects")

    return Sample(
        id=spec.id, sample_id=spec.id, name=spec.name, domain=spec.domain,
        source_type=spec.source_type, link=spec.link,
        template=list(spec.template),          # raw base steps
        objects=graph.objects, steps=list(spec.grounded_steps),  # grounded steps (incl. constraint)
        modifications=modifications, events=events,
        state_constraint=spec.state_constraint,
    )


def _grounded_from_spec(spec: WorkflowSpec) -> GroundedTemplate:
    return GroundedTemplate(name=spec.name, domain=spec.domain, grounded_steps=list(spec.grounded_steps))


def _template_from_spec(spec: WorkflowSpec) -> dict:
    return {"id": spec.id, "name": spec.name, "domain": spec.domain,
            "source_type": spec.source_type, "link": spec.link,
            "raw_steps": list(spec.template), "template": list(spec.template)}


def bind_one(llm, spec: WorkflowSpec, objects_cfg: dict, bind_mods_tmpl: str | None = None) -> Sample | None:
    """Full Phase-2 binding for one spec (derive graph → assemble → mock tools → expects)."""
    # The invariant is now merged into spec.grounded_steps (as a state-constraint step) by
    # the ground stage, so _grounded_from_spec carries it to identify_objects → custodian.
    grounded = _grounded_from_spec(spec)
    template = _template_from_spec(spec)
    graph = _identify_objects(llm, grounded, template, objects_cfg)
    if not graph:
        return None
    # Mod-target binding: sole business object → deterministic; otherwise LLM-map.
    mapping = None
    if spec.modifications and len(_business_objects(graph)) != 1 and bind_mods_tmpl:
        mapping = llm_bind_mod_targets(llm, graph, spec.modifications, bind_mods_tmpl)
    sample = assemble_sample(spec, graph, mod_mapping=mapping)
    # Reuse the bound Workflow shape only as the carrier _add_mock_tools expects.
    # Feed the GROUNDED base-event texts (which now name the concrete cast — reps, SKUs)
    # to mock-data generation so the read-service roster contains exactly those entities.
    from src.data.schema import Workflow
    base_entity_texts = [e.input for e in sample.events if e.role == "base"]
    base_entity_texts += [e.expect.action for e in sample.events if e.role == "base" and e.expect]
    wf = Workflow(id=spec.id, name=spec.name, domain=spec.domain, source_type=spec.source_type,
                  objects=sample.objects, steps=list(sample.steps) + base_entity_texts,
                  events=sample.events, tools=[])
    _add_mock_tools(llm, wf)            # mock data fed the bound step + base-event texts
    sample.tools = wf.tools
    sample.objects = wf.objects
    _rewrite_event_expectations(llm, sample, wf)  # object-specific expects (skips pre-authored base)
    return sample


def default_output_path(input_path: Path) -> Path:
    return input_path.with_name("workflows-mods.jsonl")


def run(args: argparse.Namespace) -> Path:
    if args.output is None:
        args.output = default_output_path(args.input)
    if getattr(args, "provider", None) is None:
        args.provider = infer_provider(args.model)
    if args.seed is not None:
        random.seed(args.seed)

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
        print("All specs already bound. Use --force to regenerate.")
        return args.output

    objects_cfg = load_prompt_template(_OBJECTS_PROMPT)
    bind_mods_tmpl = load_prompt_template(_BIND_MODS_PROMPT)["user_prompt"]
    workers = getattr(args, "workers", 1)
    print_run_info(args.provider, args.model, args.seed,
                   {"Phase": "2 — bind spec → agent graph → unified", "Workers": str(workers)})
    llm = create_llm(provider=args.provider, model=args.model,
                     temperature=args.temperature, seed=args.seed)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    ok = 0
    fail = 0
    write_lock = threading.Lock()

    with open(args.output, file_mode) as f:
        with tqdm(total=len(pending), desc="Binding") as pbar:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(bind_one, llm, s, objects_cfg, bind_mods_tmpl): s for s in pending}
                for future in as_completed(futures):
                    s = futures[future]
                    try:
                        sample = future.result()
                    except Exception as e:
                        tqdm.write(f"  FAILED {s.id}: {e}", file=sys.stderr)
                        sample = None
                    if sample is not None:
                        with write_lock:
                            f.write(sample.model_dump_json() + "\n")
                            f.flush()
                        ok += 1
                    else:
                        fail += 1
                    pbar.update(1)

    print()
    print(f"Complete. Output: {args.output}")
    print(f"Bound: {ok}, failed: {fail}")
    return args.output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 2: bind a WorkflowSpec to a derived agent graph → unified Sample.")
    parser.add_argument("--input", "-i", type=Path, required=True, help="spec.jsonl (WorkflowSpec)")
    parser.add_argument("--output", "-o", type=Path, default=None, help="Output workflows-mods.jsonl")
    parser.add_argument("--id", dest="ids", metavar="ID", action="append", default=None)
    parser.add_argument("--workers", "-w", type=int, default=1)
    add_common_args(parser)
    return parser


def main():
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
