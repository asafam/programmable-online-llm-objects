"""
State-probe test case generator (two-stage).

Each TC is built in K+1 LLM calls:

  * Stage A — one call generates the N state-mutating events
    (additions, entity-attribute modifications, retractions).
  * Stage B — K calls (cycling through 5 probe types) generate one probe
    question each. Each call sees the full state-events list and must pick
    2-4 supporting event IDs as `depends_on`. The script-side validator
    enforces that depends_on is in [2,4] and references real event IDs.

Run with both evaluate.py (LNL) and evaluate_baseline.py (OpenClaw) to
compare per-probe conditioned accuracy across depths.

Usage:
    python -m src.data.generate_state_probe_tcs \\
        -i outputs/data/zapier/20260421_zapier_fixed/samples.jsonl \\
        --depths 5 10 20 \\
        --k-probes 5 \\
        --model gpt-5.4-mini
"""
from __future__ import annotations

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

from src.data.generate_test_cases import format_sample
from src.data.schema import (
    Event,
    GeneratedEventWithExpect,
    MockToolDef,
    ObjectDef,
    Workflow,
    StateEventsList,
    StateProbeQuestion,
    Step,
    Sample,
)
from src.data.llm import create_llm
from src.data.utils import (
    add_common_args,
    generate_with_retries,
    infer_provider,
    load_completed_keys,
    load_jsonl,
    load_prompt_template,
    print_run_info,
    setup_output,
    validate_paths,
)

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "config/prompts/data-gen"
_STATE_EVENTS_PROMPT_PATH = _PROMPTS_DIR / "generate_state_events.yaml"
_PROBE_QUESTION_PROMPT_PATH = _PROMPTS_DIR / "generate_state_probe_question.yaml"

# Probe types — one stage-B call per type, cycling if K > len(PROBE_TYPES).
# All five types target conversational memory's O(N) scan failure modes.
# A structured-state system reads the answer from its dict; a conversational
# system must scan all N messages, apply retractions, and track attribute history.
PROBE_TYPES: list[tuple[str, str, str]] = [
    (
        "retraction_pruned_set",
        "Retraction-pruned set",
        "Question asks which entities are currently active/open/enrolled. "
        "The correct answer set MUST differ from the full addition set — "
        "at least one entity must have been retracted and therefore excluded. "
        "Easy from structured state (enumerate dict keys); hard from conversation "
        "(must scan all N messages to find which entities were retracted).\n"
        "IMPORTANT: Do NOT pick this type if every added entity is still active — "
        "you MUST exclude at least one retracted entity from the answer.\n"
        "Examples:\n"
        '  - "Which tickets are currently open?"\n'
        '  - "Which deals are still active?"\n'
        '  - "List the currently-enrolled participants."',
    ),
    (
        "attribute_churn_current_value",
        "Attribute-churn current value",
        "Question asks for the CURRENT value of a specific attribute on a "
        "named entity that has had that attribute changed AT LEAST TWICE after "
        "creation (so the event log contains 3+ mentions of that entity+attribute). "
        "Easy from structured state (single dict lookup); hard from conversation "
        "(must find the most recent of 3+ mentions without confusing earlier values).\n"
        "IMPORTANT: Only pick an entity whose attribute was changed at least TWICE "
        "after initial creation. Do NOT use an entity whose attribute was set once.\n"
        "Examples:\n"
        '  - "What is the current priority of T-017?" (if T-017 was escalated twice)\n'
        '  - "What stage is Deal #189 currently in?" (if it moved stages twice)\n'
        '  - "Who is currently assigned to incident INC-003?" (if reassigned twice)',
    ),
    (
        "retraction_aware_count",
        "Retraction-aware count",
        "Question asks HOW MANY entities are currently in a given state. "
        "The correct count must be less than the total additions — retractions "
        "reduce the count. Easy from structured state (len(dict)); hard from "
        "conversation (additions minus retractions requires full-history scan).\n"
        "IMPORTANT: The correct answer count must be strictly less than the "
        "number of additions (some entities were retracted). Do not ask about "
        "a category where nothing was retracted.\n"
        "Examples:\n"
        '  - "How many tickets are currently open?"\n'
        '  - "How many active incidents are there right now?"\n'
        '  - "How many deals are still in the pipeline?"',
    ),
    (
        "multi_attribute_filter_pruned",
        "Multi-attribute filter over pruned set",
        "Question filters the currently-active entities by TWO OR MORE attribute "
        "conditions simultaneously, AND requires excluding retracted entities. "
        "Combines retraction pruning + attribute join — double failure mode for "
        "conversational memory. Easy from structured state (filter dict by "
        "multiple keys); hard from conversation (O(N) scan + retraction tracking).\n"
        "IMPORTANT: The answer set must (a) exclude at least one retracted entity "
        "AND (b) require two attribute conditions to be simultaneously satisfied.\n"
        "Examples:\n"
        '  - "Which currently-open tickets are both P1 AND assigned to Alice?"\n'
        '  - "Which active deals are both in Negotiation stage AND owned by Carlos?"\n'
        '  - "Which enrolled participants have both confirmed status AND high tier?"',
    ),
    (
        "churn_prune_combined",
        "Churn + prune combined",
        "Question asks for the current attribute value of ALL still-active entities "
        "that were ORIGINALLY created with a particular attribute value (which some "
        "may have since changed). Requires both: (1) excluding retracted entities "
        "and (2) tracking attribute churn to show current values. Hardest for "
        "conversational memory: must track retractions AND multi-value attribute "
        "history simultaneously.\n"
        "IMPORTANT: Choose an attribute that some entities started with value X "
        "and later had changed. The answer must show current (not original) values "
        "for the surviving entities.\n"
        "Examples:\n"
        '  - "Among tickets originally created as P1, which are still open and what is each one\'s current priority?"\n'
        '  - "Of the deals that started in Prospecting stage, which are still active and what stage are they in now?"\n'
        '  - "For incidents originally assigned to Alice, which are still open and who owns them now?"',
    ),
]


def _format_state_events_for_context(events: list[GeneratedEventWithExpect]) -> str:
    lines = []
    for ev in events:
        action = (ev.expect.action if ev.expect else "").strip()
        reason = (ev.expect.reason if ev.expect else "").strip()
        kind = f" [{reason}]" if reason else ""
        lines.append(f"- {ev.id} @ {ev.when}{kind}: {ev.input.strip()}")
        if action:
            lines.append(f"    expected post-state: {action}")
    return "\n".join(lines)


def _format_state_events_prompt(
    template: str,
    sample: Workflow,
    depth: int,
) -> str:
    return (
        template
        .replace("{SAMPLE}", format_sample(sample))
        .replace("{DEPTH}", str(depth))
        .replace("{DEPTH_PADDED}", f"{depth:03d}")
    )


def _format_probe_question_prompt(
    template: str,
    sample: Workflow,
    depth: int,
    state_events: list[GeneratedEventWithExpect],
    probe_type_key: str,
    probe_type_name: str,
    probe_type_description: str,
    probe_id: str,
    probe_when: str,
) -> str:
    return (
        template
        .replace("{SAMPLE}", format_sample(sample))
        .replace("{DEPTH}", str(depth))
        .replace("{DEPTH_PADDED}", f"{depth:03d}")
        .replace("{STATE_EVENTS_FORMATTED}", _format_state_events_for_context(state_events))
        .replace("{PROBE_TYPE_KEY}", probe_type_key)
        .replace("{PROBE_TYPE_NAME}", probe_type_name)
        .replace("{PROBE_TYPE_DESCRIPTION}", probe_type_description)
        .replace("{PROBE_TYPE_EXAMPLES}", probe_type_description)
        .replace("{PROBE_ID}", probe_id)
        .replace("{PROBE_TIMESTAMP}", probe_when)
    )


def _load_samples(input_path: Path) -> list[Workflow]:
    """Load samples from JSONL — accepts both workflows.jsonl and samples.jsonl.

    When samples.jsonl is provided (detected by presence of 'modifications' and
    'events' fields), deduplicates by sample_id to reconstruct unique samples.
    """
    raw = load_jsonl(input_path)
    if not raw:
        return []

    first = raw[0]
    if "modifications" in first and "events" in first:
        seen: dict[str, Workflow] = {}
        for d in raw:
            sid = d["sample_id"]
            if sid not in seen:
                seen[sid] = Workflow(
                    id=sid,
                    name=d["name"],
                    domain=d["domain"],
                    source_type=d.get("source_type", ""),
                    link=d.get("link", ""),
                    raw_steps=[],
                    objects=[ObjectDef(**o) for o in d["objects"]],
                    steps=[Step(**s) for s in d["steps"]],
                    mock_tools=[MockToolDef(**t) for t in d.get("mock_tools", [])],
                    flagged=False,
                    flag_reasons=[],
                )
        return list(seen.values())

    return [Workflow(**d) for d in raw]


def _validate_state_events(
    result: StateEventsList,
    depth: int,
) -> bool:
    if len(result.state_events) != depth:
        raise ValueError(
            f"Expected {depth} state events, got {len(result.state_events)}"
        )
    for se in result.state_events:
        if not se.expect or not se.expect.action:
            raise ValueError(f"State event {se.id} missing expect.action")
    return True


def _validate_probe_question(
    result: StateProbeQuestion,
    state_event_ids: set[str],
    expected_id: str,
) -> bool:
    pe = result.probe_event
    if pe.id != expected_id:
        raise ValueError(
            f"Probe id mismatch: expected {expected_id}, got {pe.id}"
        )
    if not pe.expect or not pe.expect.action or not pe.expect.reason:
        raise ValueError(
            f"Probe event {pe.id} missing expect.action or expect.reason"
        )
    if not (2 <= len(pe.depends_on) <= 4):
        raise ValueError(
            f"Probe event {pe.id} must declare depends_on with 2-4 state-event IDs "
            f"(got {len(pe.depends_on)}: {pe.depends_on})"
        )
    bad = [d for d in pe.depends_on if d not in state_event_ids]
    if bad:
        raise ValueError(
            f"Probe event {pe.id} depends_on references unknown event IDs: {bad}"
        )
    return True


def _scenario_to_test_case(
    sample: Workflow,
    state_events: list[GeneratedEventWithExpect],
    probe_events: list[GeneratedEventWithExpect],
    depth: int,
    tc_index: int,
) -> Sample:
    all_events: list[Event] = []

    for ge in state_events:
        d = ge.model_dump()
        d.update(role="irrelevant", after_mod_ids=[], depends_on=[])
        all_events.append(Event(**d))

    for ge in probe_events:
        d = ge.model_dump()
        d.update(role="post_mod", after_mod_ids=[])
        all_events.append(Event(**d))

    return Sample(
        id=f"{sample.id}-probe-D{depth:02d}-TC{tc_index:03d}",
        sample_id=sample.id,
        name=f"{sample.name} [State Probe D{depth}]",
        domain=sample.domain,
        source_type=sample.source_type,
        link=sample.link,
        objects=sample.objects,
        llm_classes=[],
        steps=sample.steps,
        modifications=[],
        events=all_events,
        mock_tools=list(sample.mock_tools),
    )


def _default_output_path(input_path: Path) -> Path:
    return input_path.parent / f"{input_path.stem}_state_probes.jsonl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate state-probe test cases from samples or test_cases JSONL."
    )
    parser.add_argument(
        "--input", "-i",
        type=Path,
        required=True,
        help="Input JSONL: workflows.jsonl or samples.jsonl",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Output JSONL path (default: {input_stem}_state_probes.jsonl)",
    )
    parser.add_argument(
        "--depths",
        type=int,
        nargs="+",
        default=[5, 10, 20],
        metavar="N",
        help="State event depths to generate (default: 5 10 20)",
    )
    parser.add_argument(
        "--k-probes",
        type=int,
        default=5,
        help="Probe questions per TC (default: 5)",
    )
    parser.add_argument(
        "--scenarios-per-sample",
        type=int,
        default=1,
        help="TC variants per (sample, depth) pair (default: 1)",
    )
    parser.add_argument(
        "--id",
        dest="ids",
        metavar="ID",
        action="append",
        default=None,
        help="Filter to specific sample IDs (repeatable)",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=1,
        help="Parallel workers (default: 1)",
    )
    parser.add_argument(
        "--state-events-prompt",
        type=Path,
        default=_STATE_EVENTS_PROMPT_PATH,
        help=f"Stage-A prompt template (default: {_STATE_EVENTS_PROMPT_PATH})",
    )
    parser.add_argument(
        "--probe-question-prompt",
        type=Path,
        default=_PROBE_QUESTION_PROMPT_PATH,
        help=f"Stage-B prompt template (default: {_PROBE_QUESTION_PROMPT_PATH})",
    )
    add_common_args(parser)
    return parser


def run(args: argparse.Namespace) -> Path:
    if args.output is None:
        args.output = _default_output_path(args.input)
    if args.provider is None:
        args.provider = infer_provider(args.model)

    validate_paths(args.input, args.state_events_prompt)
    validate_paths(args.input, args.probe_question_prompt)

    samples = _load_samples(args.input)
    state_events_template = load_prompt_template(args.state_events_prompt)["user_prompt"]
    probe_question_template = load_prompt_template(args.probe_question_prompt)["user_prompt"]

    if getattr(args, "ids", None):
        id_set = set(args.ids)
        samples = [s for s in samples if s.id in id_set]

    if args.limit:
        samples = samples[: args.limit]

    print(f"Loaded {len(samples)} samples from {args.input}")

    # Completion key: "{sample_id}-probe-D{depth:02d}"
    completed, file_mode = setup_output(
        args.output,
        args.force,
        lambda: load_completed_keys(
            args.output,
            lambda d: d["id"].rsplit("-TC", 1)[0] if d.get("id") and "-TC" in d["id"] else d.get("id"),
        ),
    )

    work_units = [
        (sample, depth)
        for sample in samples
        for depth in args.depths
        if f"{sample.id}-probe-D{depth:02d}" not in completed
    ]

    if not work_units:
        print("All work units already generated. Use --force to regenerate.")
        return args.output

    total_units = len(samples) * len(args.depths)
    if completed:
        print(f"Resuming: {len(completed)} already completed, {len(work_units)} remaining")
    else:
        print(f"Processing {len(work_units)} work units")

    print_run_info(
        args.provider,
        args.model,
        args.seed,
        {
            "Depths": ", ".join(str(d) for d in args.depths),
            "Probe questions": str(args.k_probes),
            "Scenarios per sample": str(args.scenarios_per_sample),
            "Workers": str(args.workers),
        },
    )

    llm = create_llm(
        provider=args.provider,
        model=args.model,
        temperature=args.temperature,
        seed=args.seed,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    success_count = 0
    fail_count = 0
    write_lock = threading.Lock()

    def _process_unit(sample: Workflow, depth: int) -> list[Sample]:
        test_cases: list[Sample] = []
        for scenario_idx in range(1, args.scenarios_per_sample + 1):
            scenario_tag = f"{sample.id}-probe-D{depth:02d}-v{scenario_idx}"

            # Stage A: generate the N state-mutating events.
            stage_a_prompt = _format_state_events_prompt(
                state_events_template, sample, depth
            )
            state_result = generate_with_retries(
                llm=llm,
                prompt=stage_a_prompt,
                response_model=StateEventsList,
                item_id=f"{scenario_tag}-stateA",
                validator=lambda r, d=depth: _validate_state_events(r, d),
            )
            if not state_result:
                continue
            state_events = state_result.state_events
            state_ids = {se.id for se in state_events}

            # Stage B: K calls, one per probe type (cycling).
            probe_events: list[GeneratedEventWithExpect] = []
            for probe_idx in range(args.k_probes):
                type_key, type_name, type_description = PROBE_TYPES[
                    probe_idx % len(PROBE_TYPES)
                ]
                probe_id = f"E{depth + probe_idx + 1:03d}"
                probe_when = f"W02-6T{9 + probe_idx:02d}:00"
                stage_b_prompt = _format_probe_question_prompt(
                    template=probe_question_template,
                    sample=sample,
                    depth=depth,
                    state_events=state_events,
                    probe_type_key=type_key,
                    probe_type_name=type_name,
                    probe_type_description=type_description,
                    probe_id=probe_id,
                    probe_when=probe_when,
                )
                probe_result = generate_with_retries(
                    llm=llm,
                    prompt=stage_b_prompt,
                    response_model=StateProbeQuestion,
                    item_id=f"{scenario_tag}-probe-{probe_idx + 1}-{type_key}",
                    validator=lambda r, sids=state_ids, pid=probe_id: _validate_probe_question(
                        r, sids, pid
                    ),
                )
                if probe_result:
                    probe_events.append(probe_result.probe_event)

            if len(probe_events) != args.k_probes:
                tqdm.write(
                    f"  PARTIAL {scenario_tag}: produced {len(probe_events)}/{args.k_probes} probes — skipping",
                    file=sys.stderr,
                )
                continue

            tc = _scenario_to_test_case(
                sample, state_events, probe_events, depth, scenario_idx
            )
            test_cases.append(tc)
        return test_cases

    with open(args.output, file_mode) as f:
        with tqdm(total=len(work_units), desc="Generating state probe TCs") as pbar:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(_process_unit, sample, depth): (sample.id, depth)
                    for sample, depth in work_units
                }
                for future in as_completed(futures):
                    sample_id, depth = futures[future]
                    try:
                        test_cases = future.result()
                    except Exception as e:
                        tqdm.write(
                            f"  FAILED {sample_id}-probe-D{depth:02d}: {e}",
                            file=sys.stderr,
                        )
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
    return args.output


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
