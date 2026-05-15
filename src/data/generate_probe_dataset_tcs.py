"""
Probe-dataset test case generator (probe-first, 4-stage).

Each TC is built in (1 + E + K + 1) LLM calls:

  Stage A — choose tracked entities and probe types (1 call)
  Stage B — generate relevant events for each tracked entity (E calls, one per entity)
  Stage C — generate one probe question per probe target (K calls)
  Stage D — generate background interference events (1 call)

The script then interleaves relevant and background events deterministically
(seeded), reassigns IDs and timestamps, and assembles a standard TestCase.

Probe events have `role="post_mod"` and `depends_on=[…tracked event IDs…]`.
Tracked events have `role="irrelevant"` and `expect` set (per-event memory-fidelity check).
Background events have `role="irrelevant"` and `expect=None` (judge skips them).

Run through both evaluate.py (LNL) and evaluate_baseline.py (OpenClaw):
    python -m src.data.evaluate -i …_probe_dataset.jsonl \\
        --tracked-judge config/prompts/lnl/judge_memory_fidelity.yaml
    python -m src.data.evaluate_baseline -i …_probe_dataset.jsonl \\
        --tracked-judge config/prompts/lnl/judge_memory_fidelity.yaml

Usage:
    python -m src.data.generate_probe_dataset_tcs \\
        -i outputs/data/zapier/20260411_zapier_clean/samples.jsonl \\
        --depths 10 20 30 50 \\
        --seeds 3 \\
        --model gpt-5.4-mini
"""
from __future__ import annotations

import argparse
import random
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
    BackgroundEventsList,
    Event,
    GeneratedEventWithExpect,
    MockToolDef,
    ObjectDef,
    ProbeQuestion,
    ProbeTargetsList,
    ProbeType,
    RelevantEventsList,
    Sample,
    Step,
    TestCase,
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
_PROBE_TARGETS_PROMPT_PATH   = _PROMPTS_DIR / "generate_probe_targets.yaml"
_RELEVANT_EVENTS_PROMPT_PATH = _PROMPTS_DIR / "generate_relevant_events.yaml"
_PROBE_QUESTIONS_PROMPT_PATH = _PROMPTS_DIR / "generate_probe_questions.yaml"
_BACKGROUND_EVENTS_PROMPT_PATH = _PROMPTS_DIR / "generate_background_events.yaml"

# Descriptive text per probe type injected into the Stage C prompt.
_PROBE_TYPE_DESCRIPTIONS: dict[ProbeType, tuple[str, str]] = {
    ProbeType.direct_lookup: (
        "Direct Lookup",
        "Ask for the CURRENT value of ONE specific field on ONE named tracked entity. "
        "The entity must have received at least one correction event — so there are "
        "multiple conflicting values in the event stream and the correct answer is "
        "the value from the LAST correction. "
        "Easy from structured state (single dict lookup); hard from conversation "
        "(must find the most recent of several conflicting values).\n"
        "Examples:\n"
        '  - "What is the current discount on QUOTE-1042?"\n'
        '  - "What is the final approved amount for ORDER-5501?"\n'
        '  - "What unit price was last set for INVOICE-0088?"',
    ),
    ProbeType.aggregate: (
        "Aggregate",
        "Ask for a SUM, COUNT, or TOTAL across ALL tracked entities for a shared "
        "scalar field. The answer requires reading the FINAL value of that field "
        "for every tracked entity (after all corrections) and combining them. "
        "Easy from structured state (iterate dict, sum); hard from conversation "
        "(must find the latest value per entity across the entire event stream).\n"
        "Examples:\n"
        '  - "What is the total approved amount across all tracked quotes?"\n'
        '  - "How many tracked orders are currently active?"\n'
        '  - "What is the combined quantity across all tracked invoices?"',
    ),
    ProbeType.conditional_aggregate: (
        "Conditional Aggregate",
        "Ask for a FILTERED COUNT, SUM, or SET — which tracked entities meet a "
        "condition, or a sum/count over the subset meeting the condition. "
        "The condition must reference at least one attribute field (e.g. discount > 15%, "
        "status = approved, quantity > 100). The correct answer requires knowing each "
        "tracked entity's FINAL attribute values (after all corrections) and then "
        "filtering. Combines per-entity recall with predicate evaluation.\n"
        "Examples:\n"
        '  - "Which tracked quotes were approved with a discount above 15%?"\n'
        '  - "How many tracked orders have a quantity above 50 units?"\n'
        '  - "What is the total amount for tracked deals in the Closed-Won stage?"',
    ),
    ProbeType.retraction_status: (
        "Retraction Status",
        "Ask whether one or more tracked entities are CURRENTLY ACTIVE or have been "
        "retracted (cancelled, deleted, withdrawn, closed, removed). The correct "
        "answer requires recognising the LATEST lifecycle event for each entity — "
        "specifically whether a retraction event was issued after creation/corrections. "
        "Easy from structured state (entity present vs deleted in dict); hard from "
        "conversation (must remember a single retraction line buried in interference, "
        "and not over-recall a creation that was later cancelled).\n"
        "Examples:\n"
        '  - "Is QUOTE-1042 still being tracked?"\n'
        '  - "Which of the tracked tickets are still active?"\n'
        '  - "Has DEAL-5501 been cancelled?"',
    ),
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_relevant_events(events: list[GeneratedEventWithExpect]) -> str:
    lines = []
    for e in events:
        reason = e.expect.reason if e.expect else "?"
        action = e.expect.action if e.expect else "(no expect)"
        lines.append(
            f"[{e.id}] {e.when} → {e.recipient}: {e.input!r}\n"
            f"    expect ({reason}): {action}"
        )
    return "\n\n".join(lines)


def _format_target_entities(targets: list, tracked: list) -> str:
    """Format target entities for the probe question prompt."""
    # tracked is a list of TrackedEntitySpec
    tracked_map = {te.entity_id: te for te in tracked}
    lines = []
    for eid in targets:
        spec = tracked_map.get(eid)
        if spec:
            lines.append(f"- {eid} ({spec.entity_type}, {spec.n_corrections} corrections)")
        else:
            lines.append(f"- {eid}")
    return "\n".join(lines) if lines else "(all tracked entities)"


def _format_tracked_ids(tracked_entities) -> str:
    return "\n".join(f"- {te.entity_id} ({te.entity_type})" for te in tracked_entities)


def _format_field_types(relevant_events: list[GeneratedEventWithExpect]) -> str:
    """Summarise the field types seen across relevant events for the background prompt."""
    # Pull field hints from expect.reason labels
    reasons = {e.expect.reason for e in relevant_events if e.expect}
    return "Same field types as the tracked entities (amounts, discounts, statuses, quantities). " \
           f"Event reasons seen: {', '.join(sorted(reasons))}."


def _assign_timestamps(
    events: list[GeneratedEventWithExpect],
    start_day: int = 1,
    end_day: int = 5,
) -> list[GeneratedEventWithExpect]:
    """Reassign timestamps evenly across Mon–Fri of week 02."""
    n = len(events)
    if n == 0:
        return events
    minutes_start = start_day * 480 + 540   # day * 8h offset + 9:00
    minutes_end   = end_day * 480 + 1020    # 17:00
    if n == 1:
        steps = [minutes_start]
    else:
        step = (minutes_end - minutes_start) / (n - 1)
        steps = [int(minutes_start + i * step) for i in range(n)]

    result = []
    for e, m in zip(events, steps):
        # m = absolute minutes from week start
        # week = 2, day within week
        day = (m // 480) % 5 + 1   # 1=Mon … 5=Fri
        hm  = m % 480              # minutes since 0:00 of that day
        hour, minute = divmod(hm + (9 * 60 if day == 1 else 0), 60)
        # Simple: just use total minutes mapped to W02-D
        total_from_mon = m - 480
        day_of_week = max(1, min(5, total_from_mon // 480 + 1))
        mins_in_day = total_from_mon % 480 + 9 * 60
        h, mn = divmod(mins_in_day, 60)
        h = max(9, min(17, h))

        d = e.model_copy(update={"when": f"W02-{day_of_week}T{h:02d}:{mn:02d}"})
        result.append(d)
    return result


def _interleave(
    relevant_events: list[GeneratedEventWithExpect],
    background_events: list[GeneratedEventWithExpect],
    seed: int,
) -> list[GeneratedEventWithExpect]:
    """Place relevant events at random positions in the background stream.

    Maintains the relative ordering of relevant events (per-entity creation before
    transitions before corrections). Background events fill the remaining slots.
    """
    rng = random.Random(seed)
    n_rel = len(relevant_events)
    n_bg  = len(background_events)
    n_total = n_rel + n_bg

    if n_rel == 0:
        return list(background_events)
    if n_bg == 0:
        return list(relevant_events)

    positions = sorted(rng.sample(range(n_total), n_rel))
    pos_set   = set(positions)

    result: list[GeneratedEventWithExpect] = []
    rel_iter = iter(relevant_events)
    bg_iter  = iter(background_events)
    for i in range(n_total):
        if i in pos_set:
            result.append(next(rel_iter))
        else:
            result.append(next(bg_iter))
    return result


def _reassign_ids(
    events: list[GeneratedEventWithExpect],
    probes: list[GeneratedEventWithExpect],
) -> tuple[list[GeneratedEventWithExpect], list[GeneratedEventWithExpect], dict[str, str]]:
    """Reassign event IDs to E001..E{N} and update probe depends_on references."""
    old_to_new: dict[str, str] = {}
    updated_events: list[GeneratedEventWithExpect] = []
    for i, e in enumerate(events, 1):
        new_id = f"E{i:03d}"
        old_to_new[e.id] = new_id
        updated_events.append(e.model_copy(update={"id": new_id}))

    updated_probes: list[GeneratedEventWithExpect] = []
    for i, p in enumerate(probes, len(events) + 1):
        new_id = f"E{i:03d}"
        new_depends = [old_to_new.get(d, d) for d in p.depends_on]
        updated_probes.append(p.model_copy(update={"id": new_id, "depends_on": new_depends}))

    return updated_events, updated_probes, old_to_new


def _scenario_to_test_case(
    sample: Sample,
    events: list[GeneratedEventWithExpect],
    probes: list[GeneratedEventWithExpect],
    depth: int,
    seed: int,
    tc_index: int,
) -> TestCase:
    all_events: list[Event] = []
    for ge in events:
        d = ge.model_dump()
        d.update(role="irrelevant", after_mod_ids=[])
        all_events.append(Event(**d))
    for ge in probes:
        d = ge.model_dump()
        d.update(role="post_mod", after_mod_ids=[])
        all_events.append(Event(**d))

    return TestCase(
        id=f"{sample.id}-probe2-D{depth:02d}-S{seed:02d}-TC{tc_index:03d}",
        sample_id=sample.id,
        name=f"{sample.name} [Probe2 D{depth} S{seed}]",
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


# ── Validators ────────────────────────────────────────────────────────────────

def _validate_probe_targets(
    result: ProbeTargetsList,
    depth: int,
    expected_probe_types: set[ProbeType],
) -> bool:
    max_rel = max(4, depth * 6 // 10)
    max_entities = min(3, max_rel // 3)
    max_corr = max(1, min(4, max_rel // max(max_entities, 1) - 2))
    if max_entities == 1:
        max_corr = 1
    # aggregate/conditional_aggregate/retraction_status probes need ≥2 entities;
    # direct_lookup alone is fine with just 1.
    _multi_entity_types = {ProbeType.aggregate, ProbeType.conditional_aggregate, ProbeType.retraction_status}
    min_entities = 2 if (_multi_entity_types & expected_probe_types) else 1
    if not (min_entities <= len(result.tracked_entities) <= max_entities):
        raise ValueError(
            f"Expected {min_entities}–{max_entities} tracked entities at D={depth}, "
            f"got {len(result.tracked_entities)}"
        )
    if len(result.probe_targets) != len(expected_probe_types):
        raise ValueError(
            f"Expected exactly {len(expected_probe_types)} probe targets "
            f"(one per type), got {len(result.probe_targets)}"
        )
    types_seen = {pt.probe_type for pt in result.probe_targets}
    if types_seen != expected_probe_types:
        raise ValueError(
            f"Probe types must be exactly {sorted(t.value for t in expected_probe_types)}; "
            f"got: {sorted(t.value for t in types_seen)}"
        )
    # Retraction events are lifecycle one-shots, not memory-load comparable to corrections —
    # exempt them from the budget so D=10 + retraction can still fit 2 entities × full lifecycle.
    est_rel_core = sum(1 + 1 + te.n_corrections for te in result.tracked_entities)
    if est_rel_core > max_rel:
        raise ValueError(
            f"Estimated relevant events ({est_rel_core}, excl. retraction) exceeds max ({max_rel} = 60% of D={depth}). "
            f"Use fewer corrections per entity."
        )
    for te in result.tracked_entities:
        if not (1 <= te.n_corrections <= max_corr):
            raise ValueError(
                f"Entity {te.entity_id}: n_corrections={te.n_corrections} out of range [1,{max_corr}]"
            )
    if ProbeType.retraction_status in expected_probe_types:
        # Need at least one retracted and one non-retracted entity for the probe to be meaningful.
        retracted_ids = {te.entity_id for te in result.tracked_entities if te.retracted}
        non_retracted_ids = {te.entity_id for te in result.tracked_entities if not te.retracted}
        if not retracted_ids:
            raise ValueError(
                "retraction_status probe requires at least one tracked entity with retracted=true"
            )
        if not non_retracted_ids:
            raise ValueError(
                "retraction_status probe requires at least one tracked entity with retracted=false"
            )
    else:
        # If retraction_status isn't asked for, no entity should be retracted.
        if any(te.retracted for te in result.tracked_entities):
            raise ValueError(
                "retracted=true is only allowed when probe types include retraction_status"
            )
    return True


def _validate_relevant_events(
    result: RelevantEventsList,
    n_corrections: int,
    retracted: bool = False,
) -> bool:
    events = result.state_events
    if not events:
        raise ValueError("No events generated")
    reasons = [e.expect.reason for e in events if e.expect]
    n_corr = reasons.count("correction")
    if n_corr != n_corrections:
        raise ValueError(
            f"Expected {n_corrections} correction events, got {n_corr}"
        )
    if "creation" not in reasons:
        raise ValueError("Missing creation event")
    n_retraction = reasons.count("retraction")
    if retracted:
        if n_retraction != 1:
            raise ValueError(
                f"Expected exactly 1 retraction event (entity is marked retracted=true), "
                f"got {n_retraction}"
            )
        # Retraction must be the LAST relevant event so the final state is "removed".
        if events[-1].expect and events[-1].expect.reason != "retraction":
            raise ValueError(
                "Retraction event must be the LAST event in the entity's stream"
            )
    else:
        if n_retraction != 0:
            raise ValueError(
                f"Entity not marked retracted but got {n_retraction} retraction events"
            )
    for e in events:
        if not e.expect or not e.expect.action:
            raise ValueError(f"Event {e.id} missing expect.action")
    return True


def _validate_probe_question(
    result: ProbeQuestion,
    relevant_event_ids: set[str],
    probe_id: str,
    required_coverage: Optional[dict[str, list[str]]] = None,
) -> bool:
    pe = result.probe_event
    if pe.id != probe_id:
        raise ValueError(f"Probe id mismatch: expected {probe_id}, got {pe.id}")
    if not pe.expect or not pe.expect.action or not pe.expect.reason:
        raise ValueError(f"Probe {pe.id} missing expect.action or expect.reason")
    n_deps = len(pe.depends_on)
    if not (2 <= n_deps <= 7):
        raise ValueError(
            f"Probe {pe.id} depends_on must have 2–7 entries (got {n_deps})"
        )
    bad = [d for d in pe.depends_on if d not in relevant_event_ids]
    if bad:
        raise ValueError(
            f"Probe {pe.id} depends_on references unknown IDs: {bad}"
        )
    if required_coverage:
        # Each target entity must have at least one event in depends_on
        uncovered = [
            eid for eid, eids in required_coverage.items()
            if not any(d in eids for d in pe.depends_on)
        ]
        if uncovered:
            raise ValueError(
                f"Probe {pe.id} depends_on does not cover entities: {uncovered}. "
                f"Add at least one event from each entity."
            )
    return True


def _validate_background_events(result: BackgroundEventsList, n_background: int) -> bool:
    if len(result.state_events) != n_background:
        raise ValueError(
            f"Expected {n_background} background events, got {len(result.state_events)}"
        )
    for e in result.state_events:
        if e.expect is not None:
            raise ValueError(
                f"Background event {e.id} must not have an expect field (got: {e.expect})"
            )
    return True


# ── Prompt formatters ────────────────────────────────────────────────────────

def _fmt_probe_targets(
    template: str,
    sample: Sample,
    depth: int,
    seed: int,
    probe_types: list[ProbeType],
) -> str:
    max_rel = max(4, depth * 6 // 10)
    max_entities = min(3, max_rel // 3)
    _multi_entity_types = {ProbeType.aggregate, ProbeType.conditional_aggregate, ProbeType.retraction_status}
    min_entities = 2 if (_multi_entity_types & set(probe_types)) else 1
    # Per-entity budget assuming max_entities; capped at [1, 4].
    # When only 1 entity is tracked (small depths), cap at 1 correction so probe
    # difficulty stays comparable to D≥10 (where each of 2 entities gets 1 correction).
    max_corr = max(1, min(4, max_rel // max(max_entities, 1) - 2))
    if max_entities == 1:
        max_corr = 1
    type_names = [pt.value for pt in probe_types]
    type_list_str = ", ".join(type_names)
    n_types = len(probe_types)
    retraction_block = (
        "## Retraction Directive\n\n"
        "This run includes the `retraction_status` probe type. At least ONE tracked "
        "entity must be marked `retracted=true`, and at least one must remain active "
        "(`retracted=false`). The retracted entity will receive a retraction event "
        "(cancellation/closure/withdrawal) AFTER its corrections, making its final "
        "state \"removed\". Probes can then ask which entities are still active.\n"
        if ProbeType.retraction_status in probe_types
        else "## Retraction Directive\n\n"
        "This run does NOT include retraction probes. All tracked entities must have "
        "`retracted=false`.\n"
    )
    return (
        template
        .replace("{SAMPLE}", format_sample(sample))
        .replace("{DEPTH}", str(depth))
        .replace("{SEED}", str(seed))
        .replace("{MAX_RELEVANT_EVENTS}", str(max_rel))
        .replace("{MIN_TRACKED_ENTITIES}", str(min_entities))
        .replace("{MAX_TRACKED_ENTITIES}", str(max_entities))
        .replace("{MAX_CORRECTIONS_PER_ENTITY}", str(max_corr))
        .replace("{PROBE_TYPES_LIST}", type_list_str)
        .replace("{N_PROBE_TYPES}", str(n_types))
        .replace("{RETRACTION_DIRECTIVE}", retraction_block)
    )


def _fmt_relevant_events(
    template: str,
    sample: Sample,
    entity_id: str,
    entity_type: str,
    n_corrections: int,
    shared_correction_field: str,
    other_tracked: str,
    event_id_start: int,
    event_id_end: int,
    retracted: bool = False,
) -> str:
    n_events = 1 + 1 + n_corrections + (1 if retracted else 0)
    if retracted:
        retraction_block = (
            "## Retraction Required\n\n"
            f"This entity ({entity_id}) is marked for retraction. After all corrections, "
            "append exactly ONE retraction event as the FINAL event in this stream. "
            "The retraction must be a natural domain event for the workflow "
            "(e.g. cancellation, withdrawal, closure, deletion, marking as invalid). "
            "Use the appropriate verb for the entity type — \"cancelled\", \"withdrawn\", "
            "\"closed\", \"deleted\", \"removed\", \"superseded\", \"voided\".\n\n"
            "  `expect.reason`: \"retraction\"\n"
            "  `expect.action`: \"Entity {ENTITY_ID} retracted ({verb}); removed from "
            "tracked state.\" — current state must be empty for this entity (the system "
            "should `delete` the key from its state).\n"
        ).replace("{ENTITY_ID}", entity_id)
    else:
        retraction_block = (
            "## Retraction Directive\n\n"
            f"This entity ({entity_id}) is NOT being retracted. Do NOT include any "
            "retraction/cancellation/deletion/closure event in its stream. The final "
            "event must be a correction (or transition); the entity remains active.\n"
        )
    return (
        template
        .replace("{SAMPLE}", format_sample(sample))
        .replace("{ENTITY_ID}", entity_id)
        .replace("{ENTITY_TYPE}", entity_type)
        .replace("{N_CORRECTIONS}", str(n_corrections))
        .replace("{SHARED_CORRECTION_FIELD}", shared_correction_field)
        .replace("{OTHER_TRACKED_ENTITIES}", other_tracked)
        .replace("{N_EVENTS}", str(n_events))
        .replace("{EVENT_ID_START}", f"E{event_id_start:03d}")
        .replace("{EVENT_ID_END}", f"E{event_id_end:03d}")
        .replace("{RETRACTION_DIRECTIVE}", retraction_block)
    )


def _fmt_probe_question(
    template: str,
    sample: Sample,
    probe_type: ProbeType,
    relevant_events: list[GeneratedEventWithExpect],
    target_entities: list[str],
    tracked_entities,
    probe_id: str,
    probe_timestamp: str,
) -> str:
    type_name, type_desc = _PROBE_TYPE_DESCRIPTIONS[probe_type]
    return (
        template
        .replace("{SAMPLE}", format_sample(sample))
        .replace("{PROBE_TYPE_NAME}", type_name)
        .replace("{PROBE_TYPE_DESCRIPTION}", type_desc)
        .replace("{RELEVANT_EVENTS_FORMATTED}", _format_relevant_events(relevant_events))
        .replace("{TARGET_ENTITIES_FORMATTED}", _format_target_entities(target_entities, tracked_entities))
        .replace("{PROBE_ID}", probe_id)
        .replace("{PROBE_TIMESTAMP}", probe_timestamp)
    )


def _fmt_background_events(
    template: str,
    sample: Sample,
    n_background: int,
    tracked_entities,
    relevant_events: list[GeneratedEventWithExpect],
    event_id_start: int,
    event_id_end: int,
) -> str:
    min_confusable = max(1, n_background // 2)
    return (
        template
        .replace("{SAMPLE}", format_sample(sample))
        .replace("{N_BACKGROUND}", str(n_background))
        .replace("{TRACKED_ENTITY_IDS_FORMATTED}", _format_tracked_ids(tracked_entities))
        .replace("{TRACKED_FIELD_TYPES}", _format_field_types(relevant_events))
        .replace("{EVENT_ID_START}", f"E{event_id_start:03d}")
        .replace("{EVENT_ID_END}", f"E{event_id_end:03d}")
        .replace("{MIN_CONFUSABLE}", str(min_confusable))
    )


# ── Sample loader (shared with generate_state_probe_tcs.py) ──────────────────

def _load_samples(input_path: Path) -> list[Sample]:
    """Load samples from JSONL — accepts both samples.jsonl and test_cases.jsonl."""
    raw = load_jsonl(input_path)
    if not raw:
        return []
    first = raw[0]
    if "modifications" in first and "events" in first:
        seen: dict[str, Sample] = {}
        for d in raw:
            sid = d["sample_id"]
            if sid not in seen:
                seen[sid] = Sample(
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
    return [Sample(**d) for d in raw]


# ── Core generation logic ─────────────────────────────────────────────────────

def _generate_tc(
    sample: Sample,
    depth: int,
    seed: int,
    tc_index: int,
    llm,
    templates: dict[str, str],
    tag: str,
    probe_types: list[ProbeType],
) -> Optional[TestCase]:
    """Generate one TC for (sample, depth, seed). Returns None on failure."""

    expected_types: set[ProbeType] = set(probe_types)

    # Stage A: choose tracked entities and probe types.
    stage_a_prompt = _fmt_probe_targets(
        templates["targets"], sample, depth, seed, probe_types
    )
    targets_result = generate_with_retries(
        llm=llm,
        prompt=stage_a_prompt,
        response_model=ProbeTargetsList,
        item_id=f"{tag}-A",
        validator=lambda r, d=depth, et=expected_types: _validate_probe_targets(r, d, et),
    )
    if not targets_result:
        tqdm.write(f"  FAILED {tag} stage A", file=sys.stderr)
        return None

    tracked_entities        = targets_result.tracked_entities
    probe_targets           = targets_result.probe_targets
    shared_correction_field = targets_result.shared_correction_field

    # Stage B: generate relevant events per tracked entity.
    all_relevant: list[GeneratedEventWithExpect] = []
    entity_event_ids: dict[str, list[str]] = {}  # entity_id → [E001, E002, ...]
    id_offset = 1
    for te in tracked_entities:
        n_events = 1 + 1 + te.n_corrections + (1 if te.retracted else 0)
        id_end   = id_offset + n_events - 1

        other_tracked = "\n".join(
            f"- {x.entity_id} ({x.entity_type})"
            for x in tracked_entities if x.entity_id != te.entity_id
        ) or "(none)"

        stage_b_prompt = _fmt_relevant_events(
            templates["relevant"],
            sample,
            te.entity_id,
            te.entity_type,
            te.n_corrections,
            shared_correction_field,
            other_tracked,
            id_offset,
            id_end,
            retracted=te.retracted,
        )
        rel_result = generate_with_retries(
            llm=llm,
            prompt=stage_b_prompt,
            response_model=RelevantEventsList,
            item_id=f"{tag}-B-{te.entity_id}",
            validator=lambda r, nc=te.n_corrections, rtr=te.retracted: _validate_relevant_events(r, nc, rtr),
        )
        if not rel_result:
            tqdm.write(f"  FAILED {tag} stage B for {te.entity_id}", file=sys.stderr)
            return None

        # Re-ID from the assigned range
        for i, e in enumerate(rel_result.state_events):
            rel_result.state_events[i] = e.model_copy(update={"id": f"E{id_offset + i:03d}"})

        entity_event_ids[te.entity_id] = [e.id for e in rel_result.state_events]
        all_relevant.extend(rel_result.state_events)
        id_offset = id_end + 1

    relevant_ids = {e.id for e in all_relevant}

    # Stage C: generate probe questions.
    probe_events: list[GeneratedEventWithExpect] = []
    for p_idx, pt in enumerate(probe_targets):
        probe_id  = f"P{p_idx + 1:03d}"
        probe_when = f"W02-6T{9 + p_idx:02d}:00"

        relevant_for_probe = [
            e for e in all_relevant
            if any(eid == e.id.replace("E", "")
                   or e.id in {x for x in all_relevant for t in [pt.target_entities] if e.id in t}
                   for eid in pt.target_entities)
        ]
        # Include ALL relevant events so the LLM can derive ground truth
        relevant_for_probe = all_relevant

        stage_c_prompt = _fmt_probe_question(
            templates["questions"],
            sample,
            pt.probe_type,
            relevant_for_probe,
            pt.target_entities,
            tracked_entities,
            probe_id,
            probe_when,
        )
        # For multi-entity probes, depends_on must cover at least one event from
        # each target entity so the inclusion gate is meaningful.
        is_multi_entity = (
            pt.probe_type in (ProbeType.aggregate, ProbeType.conditional_aggregate)
            or (pt.probe_type == ProbeType.retraction_status and len(pt.target_entities) > 1)
        )
        required_coverage = (
            {eid: entity_event_ids[eid] for eid in pt.target_entities}
            if is_multi_entity
            else None
        )
        probe_result = generate_with_retries(
            llm=llm,
            prompt=stage_c_prompt,
            response_model=ProbeQuestion,
            item_id=f"{tag}-C-{pt.probe_type.value}",
            validator=lambda r, rids=relevant_ids, pid=probe_id, cov=required_coverage: _validate_probe_question(
                r, rids, pid, cov
            ),
        )
        if probe_result:
            # Tag the probe event with its probe_type in expect.reason so the
            # analysis script can group by probe_type without re-running Stage A.
            pe = probe_result.probe_event
            if pe.expect:
                pe = pe.model_copy(
                    update={"expect": pe.expect.model_copy(update={"reason": pt.probe_type.value})}
                )
            probe_events.append(pe)
        else:
            tqdm.write(f"  FAILED {tag} stage C probe {pt.probe_type.value}", file=sys.stderr)

    if len(probe_events) != len(probe_targets):
        tqdm.write(
            f"  PARTIAL {tag}: {len(probe_events)}/{len(probe_targets)} probes — skipping",
            file=sys.stderr,
        )
        return None

    # Stage D: generate background events.
    n_background = depth - len(all_relevant)
    if n_background < 0:
        tqdm.write(
            f"  WARN {tag}: relevant events ({len(all_relevant)}) > depth ({depth}); "
            f"clamping background to 0",
            file=sys.stderr,
        )
        n_background = 0

    bg_events: list[GeneratedEventWithExpect] = []
    if n_background > 0:
        bg_id_start = id_offset
        bg_id_end   = id_offset + n_background - 1
        stage_d_prompt = _fmt_background_events(
            templates["background"],
            sample,
            n_background,
            tracked_entities,
            all_relevant,
            bg_id_start,
            bg_id_end,
        )
        bg_result = generate_with_retries(
            llm=llm,
            prompt=stage_d_prompt,
            response_model=BackgroundEventsList,
            item_id=f"{tag}-D",
            validator=lambda r, nb=n_background: _validate_background_events(r, nb),
        )
        if not bg_result:
            tqdm.write(f"  FAILED {tag} stage D", file=sys.stderr)
            return None
        bg_events = bg_result.state_events

    # Interleave relevant + background (probes come after).
    merged = _interleave(all_relevant, bg_events, seed)

    # Reassign IDs and timestamps; update probe depends_on.
    merged = _assign_timestamps(merged)
    merged, probe_events, _ = _reassign_ids(merged, probe_events)

    # Assign probe timestamps after all events.
    for i, p in enumerate(probe_events):
        probe_events[i] = p.model_copy(update={"when": f"W02-6T{9 + i:02d}:00"})

    return _scenario_to_test_case(sample, merged, probe_events, depth, seed, tc_index)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _default_output_path(input_path: Path) -> Path:
    return input_path.parent / f"{input_path.stem}_probe_dataset.jsonl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate probe-dataset test cases from samples JSONL (probe-first)."
    )
    parser.add_argument(
        "--input", "-i",
        type=Path,
        required=True,
        help="Input JSONL: samples.jsonl (preferred) or test_cases.jsonl",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Output JSONL path (default: {input_stem}_probe_dataset.jsonl)",
    )
    parser.add_argument(
        "--depths",
        type=int,
        nargs="+",
        default=[10, 20, 30, 50],
        metavar="N",
        help="Event depths to generate (default: 10 20 30 50)",
    )
    parser.add_argument(
        "--seeds",
        type=int,
        default=3,
        metavar="K",
        help="Number of seeds per (sample, depth) cell (default: 3)",
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
        "--probe-targets-prompt",
        type=Path,
        default=_PROBE_TARGETS_PROMPT_PATH,
    )
    parser.add_argument(
        "--relevant-events-prompt",
        type=Path,
        default=_RELEVANT_EVENTS_PROMPT_PATH,
    )
    parser.add_argument(
        "--probe-questions-prompt",
        type=Path,
        default=_PROBE_QUESTIONS_PROMPT_PATH,
    )
    parser.add_argument(
        "--background-events-prompt",
        type=Path,
        default=_BACKGROUND_EVENTS_PROMPT_PATH,
    )
    parser.add_argument(
        "--probe-types",
        nargs="+",
        choices=[pt.value for pt in ProbeType],
        default=[
            ProbeType.direct_lookup.value,
            ProbeType.aggregate.value,
            ProbeType.conditional_aggregate.value,
        ],
        help=(
            "Which probe types to generate per TC. Default: direct_lookup, aggregate, "
            "conditional_aggregate (the original 3-probe layout). "
            "For a memory-only experiment use --probe-types direct_lookup retraction_status."
        ),
    )
    add_common_args(parser)
    return parser


def run(args: argparse.Namespace) -> Path:
    if args.output is None:
        args.output = _default_output_path(args.input)
    if args.provider is None:
        args.provider = infer_provider(args.model)

    validate_paths(args.input, args.probe_targets_prompt)
    validate_paths(args.input, args.relevant_events_prompt)
    validate_paths(args.input, args.probe_questions_prompt)
    validate_paths(args.input, args.background_events_prompt)

    samples = _load_samples(args.input)
    templates = {
        "targets":    load_prompt_template(args.probe_targets_prompt)["user_prompt"],
        "relevant":   load_prompt_template(args.relevant_events_prompt)["user_prompt"],
        "questions":  load_prompt_template(args.probe_questions_prompt)["user_prompt"],
        "background": load_prompt_template(args.background_events_prompt)["user_prompt"],
    }

    if getattr(args, "ids", None):
        id_set = set(args.ids)
        samples = [s for s in samples if s.id in id_set]

    if args.limit:
        samples = samples[: args.limit]

    print(f"Loaded {len(samples)} samples from {args.input}")

    # Completion key: "{sample_id}-probe2-D{depth:02d}-S{seed:02d}"
    completed, file_mode = setup_output(
        args.output,
        args.force,
        lambda: load_completed_keys(
            args.output,
            lambda d: (
                d["id"].rsplit("-TC", 1)[0]
                if d.get("id") and "-TC" in d["id"]
                else d.get("id")
            ),
        ),
    )

    seeds = list(range(1, args.seeds + 1))
    work_units = [
        (sample, depth, seed)
        for sample in samples
        for depth in args.depths
        for seed in seeds
        if f"{sample.id}-probe2-D{depth:02d}-S{seed:02d}" not in completed
    ]

    if not work_units:
        print("All work units already generated. Use --force to regenerate.")
        return args.output

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
            "Seeds": str(args.seeds),
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

    probe_types = [ProbeType(pt) for pt in args.probe_types]

    def _process_unit(sample: Sample, depth: int, seed: int) -> Optional[TestCase]:
        tag = f"{sample.id}-probe2-D{depth:02d}-S{seed:02d}"
        return _generate_tc(sample, depth, seed, 1, llm, templates, tag, probe_types)

    with open(args.output, file_mode) as f:
        with tqdm(total=len(work_units), desc="Generating probe dataset TCs") as pbar:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(_process_unit, s, d, seed): (s.id, d, seed)
                    for s, d, seed in work_units
                }
                for future in as_completed(futures):
                    sid, depth, seed = futures[future]
                    try:
                        tc = future.result()
                    except Exception as e:
                        tqdm.write(
                            f"  FAILED {sid}-probe2-D{depth:02d}-S{seed:02d}: {e}",
                            file=sys.stderr,
                        )
                        tc = None
                    with write_lock:
                        if tc is not None:
                            f.write(tc.model_dump_json() + "\n")
                            f.flush()
                    if tc is not None:
                        success_count += 1
                    else:
                        fail_count += 1
                    pbar.update(1)

    print()
    print(f"Complete. Generated {success_count} TCs ({fail_count} failed).")
    print(f"Output: {args.output}")
    return args.output


def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
