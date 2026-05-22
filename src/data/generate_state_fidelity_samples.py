"""
State-fidelity test case generator (two-stage, parametric).

Per (sample, depth, n_corrections) cell, two LLM calls:

  * Stage A — generates `depth` input events:
      - 1+ events that flow the probe-target entity through the workflow
        naturally (creation, approval responses, fulfillment confirmations, etc.)
      - n_c correction events, each addressed to the SAME object that received
        the original creation event, retroactively revising a field on the entity
      - the remainder are background events for other entities flowing through
        the same workflow

      Transitions happen implicitly inside the system as it processes these
      input events — the generator does not synthesize transition events.

  * Stage B — generates 1 probe question asking for the current value of
      probe_target_field on probe_target_entity, with depends_on listing all
      events that touch the probe-target (initial + corrections).

Run with both evaluate.py (LNL) and evaluate_baseline.py (OpenClaw), then
analyze with scripts/analyze_state_probes.py --mode fidelity.

Usage:
    python -m src.data.generate_state_fidelity_samples \\
        -i data/zapier/workflows.jsonl \\
        -o outputs/state_fidelity/pilot_tcs.jsonl \\
        --depths 5 10 20 30 \\
        --n-corrections 0 1 3 5 \\
        --model gpt-5.4-mini
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

from src.data.generate_samples import format_sample
from src.data.schema import (
    Event,
    FidelityEventsList,
    GeneratedEventWithExpect,
    MockToolDef,
    ObjectDef,
    Workflow,
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
_FIDELITY_EVENTS_PROMPT_PATH = _PROMPTS_DIR / "generate_fidelity_events.yaml"
_FIDELITY_PROBE_PROMPT_PATH  = _PROMPTS_DIR / "generate_fidelity_probe.yaml"


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


def _format_fidelity_events_prompt(
    template: str,
    sample: Workflow,
    depth: int,
    n_corrections: int,
) -> str:
    n_background = max(0, depth - n_corrections - 1)
    return (
        template
        .replace("{SAMPLE}", format_sample(sample))
        .replace("{DEPTH}", str(depth))
        .replace("{DEPTH_PADDED}", f"{depth:03d}")
        .replace("{N_CORRECTIONS}", str(n_corrections))
        .replace("{N_BACKGROUND}", str(n_background))
    )


def _format_fidelity_probe_prompt(
    template: str,
    sample: Workflow,
    depth: int,
    state_events: list[GeneratedEventWithExpect],
    probe_target_entity: str,
    probe_target_field: str,
    n_depends_on: int,
    probe_id: str,
    probe_when: str,
) -> str:
    return (
        template
        .replace("{SAMPLE}", format_sample(sample))
        .replace("{DEPTH}", str(depth))
        .replace("{DEPTH_PADDED}", f"{depth:03d}")
        .replace("{STATE_EVENTS_FORMATTED}", _format_state_events_for_context(state_events))
        .replace("{PROBE_TARGET_ENTITY}", probe_target_entity)
        .replace("{PROBE_TARGET_FIELD}", probe_target_field)
        .replace("{N_DEPENDS_ON}", str(n_depends_on))
        .replace("{PROBE_ID}", probe_id)
        .replace("{PROBE_TIMESTAMP}", probe_when)
    )


def _validate_fidelity_events(
    result: FidelityEventsList,
    depth: int,
    n_corrections: int,
) -> bool:
    events = result.state_events
    if len(events) != depth:
        raise ValueError(f"Expected {depth} events, got {len(events)}")

    for e in events:
        if not e.expect or not e.expect.action:
            raise ValueError(f"Event {e.id} missing expect.action")
        if e.expect.reason not in ("event", "correction", "background"):
            raise ValueError(
                f"Event {e.id} has invalid expect.reason {e.expect.reason!r}; "
                "must be 'event', 'correction', or 'background'"
            )

    n_c_actual = sum(1 for e in events if e.expect.reason == "correction")
    n_probe_events = sum(1 for e in events if e.expect.reason == "event")

    if n_c_actual != n_corrections:
        raise ValueError(
            f"Expected {n_corrections} correction events, got {n_c_actual}"
        )
    if n_probe_events < 1:
        raise ValueError("Must have at least 1 probe-target event (reason='event')")
    if not result.probe_target_entity.strip():
        raise ValueError("probe_target_entity is empty")
    if not result.probe_target_field.strip():
        raise ValueError("probe_target_field is empty")
    return True


def _validate_fidelity_probe(
    result: StateProbeQuestion,
    state_event_ids: set[str],
    expected_id: str,
    n_depends_on: int,
) -> bool:
    pe = result.probe_event
    if pe.id != expected_id:
        raise ValueError(f"Probe id mismatch: expected {expected_id}, got {pe.id}")
    if not pe.expect or not pe.expect.action or not pe.expect.reason:
        raise ValueError(f"Probe {pe.id} missing expect.action or expect.reason")
    if len(pe.depends_on) != n_depends_on:
        raise ValueError(
            f"Probe {pe.id} must have exactly {n_depends_on} depends_on entries "
            f"(got {len(pe.depends_on)}: {pe.depends_on})"
        )
    bad = [d for d in pe.depends_on if d not in state_event_ids]
    if bad:
        raise ValueError(
            f"Probe {pe.id} depends_on references unknown event IDs: {bad}"
        )
    return True


def _interleave_events(
    events: list[GeneratedEventWithExpect],
    rng: random.Random,
) -> list[GeneratedEventWithExpect]:
    """Shuffle corrections into background events, then reassign IDs and timestamps.

    Probe-target events (reason='event') stay at the front so the creation event
    always precedes corrections. Corrections and background are then randomly mixed.
    """
    probe_target = [e for e in events if e.expect and e.expect.reason == "event"]
    corrections  = [e for e in events if e.expect and e.expect.reason == "correction"]
    background   = [e for e in events if e.expect and e.expect.reason == "background"]

    mixed = corrections + background
    rng.shuffle(mixed)
    reordered = probe_target + mixed

    # Reassign IDs E001..EN and spread timestamps evenly across Mon-Fri 09:00-17:00
    n = len(reordered)
    total_minutes = 5 * 8 * 60  # 2400 min across the work week
    result = []
    for i, ev in enumerate(reordered):
        new_id = f"E{i + 1:03d}"
        minute = int(i * total_minutes / max(n, 1))
        day = 1 + minute // (8 * 60)
        tod = minute % (8 * 60)
        new_when = f"W02-{day}T{9 + tod // 60:02d}:{tod % 60:02d}"
        result.append(ev.model_copy(update={"id": new_id, "when": new_when}))
    return result


def _scenario_to_test_case(
    sample: Workflow,
    state_events: list[GeneratedEventWithExpect],
    probe_event: GeneratedEventWithExpect,
    depth: int,
    n_corrections: int,
    tc_index: int,
) -> Sample:
    all_events: list[Event] = []
    for ge in state_events:
        d = ge.model_dump()
        d.update(role="irrelevant", after_mod_ids=[], depends_on=[])
        all_events.append(Event(**d))

    d = probe_event.model_dump()
    d.update(role="post_mod", after_mod_ids=[])
    all_events.append(Event(**d))

    return Sample(
        id=f"{sample.id}-sfid-D{depth:02d}-C{n_corrections:02d}-TC{tc_index:03d}",
        sample_id=sample.id,
        name=f"{sample.name} [State Fidelity D{depth} C{n_corrections}]",
        domain=sample.domain,
        source_type=sample.source_type,
        link=sample.link,
        objects=sample.objects,
        llm_classes=[],
        steps=sample.steps,
        modifications=[],
        events=all_events,
        mock_tools=list(sample.tools),
    )


def _load_samples(input_path: Path) -> list[Workflow]:
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


def _default_output_path(input_path: Path) -> Path:
    return input_path.parent / f"{input_path.stem}_state_fidelity.jsonl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate state-fidelity test cases from samples or test_cases JSONL."
    )
    parser.add_argument("--input", "-i", type=Path, required=True,
                        help="Input JSONL: workflows.jsonl or workflows-mods.jsonl")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Output JSONL path (default: {input_stem}_state_fidelity.jsonl)")
    parser.add_argument("--depths", type=int, nargs="+", default=[5, 10, 20, 30], metavar="N",
                        help="Total input-event batch sizes to generate (default: 5 10 20 30)")
    parser.add_argument("--n-corrections", type=int, nargs="+", default=[0, 1, 3, 5],
                        metavar="NC",
                        help="Correction counts per probe-target entity (default: 0 1 3 5)")
    parser.add_argument("--paired", action="store_true", default=False,
                        help="Zip --depths with --n-corrections by position instead of "
                             "cross-producting them (lists must have the same length)")
    parser.add_argument("--scenarios-per-sample", type=int, default=1,
                        help="TC variants per (sample, depth, n_c) cell (default: 1)")
    parser.add_argument("--id", dest="ids", metavar="ID", action="append", default=None,
                        help="Filter to specific sample IDs (repeatable)")
    parser.add_argument("--workers", "-w", type=int, default=1,
                        help="Parallel workers (default: 1)")
    parser.add_argument("--fidelity-events-prompt", type=Path,
                        default=_FIDELITY_EVENTS_PROMPT_PATH,
                        help=f"Stage-A prompt template (default: {_FIDELITY_EVENTS_PROMPT_PATH})")
    parser.add_argument("--fidelity-probe-prompt", type=Path,
                        default=_FIDELITY_PROBE_PROMPT_PATH,
                        help=f"Stage-B prompt template (default: {_FIDELITY_PROBE_PROMPT_PATH})")
    add_common_args(parser)
    return parser


def run(args: argparse.Namespace) -> Path:
    if args.output is None:
        args.output = _default_output_path(args.input)
    if args.provider is None:
        args.provider = infer_provider(args.model)

    validate_paths(args.input, args.fidelity_events_prompt)
    validate_paths(args.input, args.fidelity_probe_prompt)

    samples = _load_samples(args.input)
    events_template = load_prompt_template(args.fidelity_events_prompt)["user_prompt"]
    probe_template  = load_prompt_template(args.fidelity_probe_prompt)["user_prompt"]

    if getattr(args, "ids", None):
        id_set = set(args.ids)
        samples = [s for s in samples if s.id in id_set]

    if args.limit:
        samples = samples[: args.limit]

    print(f"Loaded {len(samples)} samples from {args.input}")

    completed, file_mode = setup_output(
        args.output,
        args.force,
        lambda: load_completed_keys(
            args.output,
            lambda d: d["id"].rsplit("-TC", 1)[0]
            if d.get("id") and "-TC" in d["id"]
            else d.get("id"),
        ),
    )

    if args.paired:
        if len(args.depths) != len(args.n_corrections):
            raise ValueError(
                f"--paired requires --depths and --n-corrections to have the same length "
                f"({len(args.depths)} vs {len(args.n_corrections)})"
            )
        cell_pairs = list(zip(sorted(args.depths), args.n_corrections))
    else:
        cell_pairs = [
            (depth, n_c)
            for depth in sorted(args.depths)
            for n_c in args.n_corrections
        ]
    cell_pairs = [(d, nc) for d, nc in cell_pairs if nc + 1 <= d]

    work_units: list[tuple[Workflow, int, int]] = [
        (sample, depth, n_c)
        for sample in samples
        for (depth, n_c) in cell_pairs
        if f"{sample.id}-sfid-D{depth:02d}-C{n_c:02d}" not in completed
    ]

    if not work_units:
        print("All work units already generated. Use --force to regenerate.")
        return args.output

    if completed:
        print(f"Resuming: {len(completed)} already completed, {len(work_units)} remaining")
    else:
        print(f"Processing {len(work_units)} work units")

    if args.paired:
        cells_summary = ", ".join(f"(D={d},C={nc})" for d, nc in cell_pairs)
    else:
        cells_summary = (
            f"D∈{{{','.join(str(d) for d in sorted(args.depths))}}} × "
            f"C∈{{{','.join(str(c) for c in args.n_corrections)}}}"
        )
    print_run_info(
        args.provider,
        args.model,
        args.seed,
        {
            "Cells": cells_summary,
            "Scenarios per cell": str(args.scenarios_per_sample),
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

    def _process_unit(sample: Workflow, depth: int, n_c: int) -> list[Sample]:
        test_cases: list[Sample] = []
        for scenario_idx in range(1, args.scenarios_per_sample + 1):
            scenario_tag = f"{sample.id}-sfid-D{depth:02d}-C{n_c:02d}-v{scenario_idx}"

            # Stage A: generate depth input events.
            stage_a_prompt = _format_fidelity_events_prompt(
                events_template, sample, depth, n_c
            )
            stage_a_result = generate_with_retries(
                llm=llm,
                prompt=stage_a_prompt,
                response_model=FidelityEventsList,
                item_id=f"{scenario_tag}-eventsA",
                validator=lambda r, d=depth, nc=n_c: _validate_fidelity_events(r, d, nc),
            )
            if not stage_a_result:
                continue

            # Interleave corrections into background events (prompt outputs them grouped).
            rng = random.Random(args.seed ^ hash(scenario_tag) if args.seed else None)
            state_events = _interleave_events(stage_a_result.state_events, rng)
            probe_target_entity = stage_a_result.probe_target_entity
            probe_target_field  = stage_a_result.probe_target_field
            state_ids = {e.id for e in state_events}

            # depends_on = all events that touch the probe-target (reason in "event"/"correction")
            n_depends_on = sum(
                1 for e in state_events
                if e.expect and e.expect.reason in ("event", "correction")
            )

            # Stage B: generate 1 probe about the probe-target entity.
            probe_id   = f"E{depth + 1:03d}"
            probe_when = "W02-6T09:00"
            stage_b_prompt = _format_fidelity_probe_prompt(
                template=probe_template,
                sample=sample,
                depth=depth,
                state_events=state_events,
                probe_target_entity=probe_target_entity,
                probe_target_field=probe_target_field,
                n_depends_on=n_depends_on,
                probe_id=probe_id,
                probe_when=probe_when,
            )
            stage_b_result = generate_with_retries(
                llm=llm,
                prompt=stage_b_prompt,
                response_model=StateProbeQuestion,
                item_id=f"{scenario_tag}-probe",
                validator=lambda r, sids=state_ids, pid=probe_id, nd=n_depends_on: (
                    _validate_fidelity_probe(r, sids, pid, nd)
                ),
            )
            if not stage_b_result:
                tqdm.write(f"  SKIP {scenario_tag}: Stage B failed", file=sys.stderr)
                continue

            tc = _scenario_to_test_case(
                sample, state_events, stage_b_result.probe_event,
                depth, n_c, scenario_idx,
            )
            test_cases.append(tc)
        return test_cases

    with open(args.output, file_mode) as f:
        with tqdm(total=len(work_units), desc="Generating state fidelity TCs") as pbar:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(_process_unit, sample, depth, n_c): (sample.id, depth, n_c)
                    for sample, depth, n_c in work_units
                }
                for future in as_completed(futures):
                    sample_id, depth, n_c = futures[future]
                    try:
                        test_cases = future.result()
                    except Exception as e:
                        tqdm.write(
                            f"  FAILED {sample_id}-sfid-D{depth:02d}-C{n_c:02d}: {e}",
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
