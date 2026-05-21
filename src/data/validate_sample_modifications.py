"""Stage 2 validator: grade Modifications + Events linkage for each Sample.

For each Sample (a test case) in the input JSONL, compute deterministic
health issues for each modification (target resolves, mod_type valid,
when-timestamp parses) and call an LLM judge to grade quality and the
type_match / ambiguity_match / events_test_mod axes.

Usage:
    python -m src.data.validate_sample_modifications \\
        --samples outputs/.../workflows-mods.jsonl \\
        --provider azure --judge-model gpt-5.4 \\
        --workers 4 \\
        --output samples__modification_validation.jsonl
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

from src.data.llm import create_llm
from src.data.schema import (
    Sample,
    ModType,
    Ambiguity,
    ModificationVerdict,
    SampleModificationValidation,
    SampleModificationsJudgement,
)
from src.data.utils import generate_with_retries, load_jsonl

load_dotenv()


_PROMPT_PATH = (
    Path(__file__).parent.parent.parent
    / "config" / "prompts" / "data-gen" / "validate_sample_modifications.yaml"
)

_WHEN_RE = re.compile(r"^W(\d+)-(\d+)T(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def _load_prompt() -> str:
    with open(_PROMPT_PATH) as f:
        return yaml.safe_load(f)["prompt"]


def _format_objects(s: Sample) -> str:
    if not s.objects:
        return "(no objects)"
    lines = []
    for o in s.objects:
        lines.append(f"  - {o.object_id}: {o.role}")
    return "\n".join(lines)


def _format_steps(s: Sample) -> str:
    if not s.steps:
        return "(no steps)"
    lines = []
    for i, st in enumerate(s.steps, 1):
        lines.append(f"  [{i}] target={st.target}  text: {st.text}")
    return "\n".join(lines)


def _format_modifications(s: Sample) -> str:
    if not s.modifications:
        return "(no modifications)"
    lines = []
    for m in s.modifications:
        lines.append(
            f"  - mod_id={m.id}  when={m.when}  target={m.target}  "
            f"mod_type={m.mod_type.value}  ambiguity={m.ambiguity.value}"
        )
        lines.append(f"    intent: {m.intent}")
    return "\n".join(lines)


def _format_events(s: Sample) -> str:
    if not s.events:
        return "(no events)"
    lines = []
    for e in s.events:
        role = e.role or "?"
        after = ",".join(e.after_mod_ids) if e.after_mod_ids else "-"
        # NOTE: do NOT truncate input or expect.action — the judge needs the
        # full payload to verify mod-qualifying conditions in addresses,
        # ticket text, etc. Earlier truncation at 120/140 chars caused sonnet
        # to flag PARTIAL because it couldn't see whether addresses contained
        # the modification's filter term (e.g., "TX"/"Texas").
        expect = e.expect.action if e.expect else ""
        lines.append(
            f"  - {e.id}  role={role}  when={e.when}  recipient={e.recipient}  "
            f"after_mod={after}"
        )
        lines.append(f"    input: {e.input}")
        if expect:
            lines.append(f"    expect.action: {expect}")
    return "\n".join(lines)


def _health_check_modification(sample: Sample, mod_index: int) -> list[str]:
    """Deterministic per-modification structural checks."""
    issues: list[str] = []
    m = sample.modifications[mod_index]
    obj_ids = {o.object_id for o in sample.objects}

    if not (m.id or "").strip():
        issues.append("mod_id is empty")
    if not (m.intent or "").strip():
        issues.append("intent is empty")
    if not (m.target or "").strip():
        issues.append("target is empty")
    elif m.target not in obj_ids:
        issues.append(f"target '{m.target}' does not exist in sample.objects")
    if m.mod_type not in ModType:
        issues.append(f"mod_type '{m.mod_type}' is not a valid ModType")
    if m.ambiguity not in Ambiguity:
        issues.append(f"ambiguity '{m.ambiguity}' is not a valid Ambiguity")
    if not _WHEN_RE.match(m.when or ""):
        issues.append(f"when '{m.when}' does not match Wnn-NTHH:MM format")
    # Check that at least one event references this mod
    referenced = any(m.id in (e.after_mod_ids or []) for e in sample.events)
    if not referenced:
        issues.append(f"no event references mod_id '{m.id}' in after_mod_ids")
    return issues


def _aggregate_health(verdicts: list[ModificationVerdict]) -> str:
    return "OK" if all(not v.health_issues for v in verdicts) else "ISSUES"


def _aggregate_quality(verdicts: list[ModificationVerdict], overall_q: str) -> str:
    scores = [v.quality for v in verdicts]
    if overall_q == "POOR" or any(q == "POOR" for q in scores):
        return "POOR"
    if overall_q == "ADEQUATE" or any(q == "ADEQUATE" for q in scores):
        return "ADEQUATE"
    return "GOOD" if scores else "ADEQUATE"


def _validate_sample(llm, sample: Sample, prompt_template: str) -> SampleModificationValidation:
    if not sample.modifications:
        return SampleModificationValidation(
            sample_id=sample.id,
            workflow_id=sample.sample_id or "",
            n_modifications=0,
            n_events=len(sample.events),
            modification_verdicts=[],
            overall_quality="POOR",
            overall_issues=["sample has zero modifications"],
            overall_reasoning="No modifications to validate.",
            aggregate_health="ISSUES",
            aggregate_quality="POOR",
        )

    health_lists = [_health_check_modification(sample, i) for i in range(len(sample.modifications))]

    prompt = (
        prompt_template
        .replace("{SAMPLE_ID}", sample.id)
        .replace("{SAMPLE_NAME}", sample.name)
        .replace("{OBJECTS}", _format_objects(sample))
        .replace("{STEPS}", _format_steps(sample))
        .replace("{MODIFICATIONS}", _format_modifications(sample))
        .replace("{EVENTS}", _format_events(sample))
    )
    expected_ids = [m.id for m in sample.modifications]

    judgement = generate_with_retries(
        llm=llm,
        prompt=prompt,
        response_model=SampleModificationsJudgement,
        item_id=f"{sample.id}-mods",
        validator=lambda r: (
            r.overall_quality in ("GOOD", "ADEQUATE", "POOR")
            and {m.mod_id for m in r.modifications} == set(expected_ids)
        ),
    )

    if judgement is None:
        verdicts = [
            ModificationVerdict(
                sample_id=sample.id,
                mod_id=m.id,
                mod_type=m.mod_type.value,
                ambiguity=m.ambiguity.value,
                target=m.target,
                when=m.when,
                intent_preview=m.intent[:120],
                health_issues=health_lists[i],
                quality="POOR",
                quality_issues=["(judge failed; quality not assessed)"],
            )
            for i, m in enumerate(sample.modifications)
        ]
        return SampleModificationValidation(
            sample_id=sample.id,
            workflow_id=sample.sample_id or "",
            n_modifications=len(sample.modifications),
            n_events=len(sample.events),
            modification_verdicts=verdicts,
            overall_quality="POOR",
            overall_issues=["(judge failed)"],
            overall_reasoning="LLM judge failed to produce a verdict.",
            aggregate_health=_aggregate_health(verdicts),
            aggregate_quality="POOR",
        )

    judge_by_id = {j.mod_id: j for j in judgement.modifications}
    verdicts: list[ModificationVerdict] = []
    for i, m in enumerate(sample.modifications):
        j = judge_by_id.get(m.id)
        verdicts.append(ModificationVerdict(
            sample_id=sample.id,
            mod_id=m.id,
            mod_type=m.mod_type.value,
            ambiguity=m.ambiguity.value,
            target=m.target,
            when=m.when,
            intent_preview=m.intent[:120],
            health_issues=health_lists[i],
            quality=(j.quality if j else "POOR"),
            quality_issues=(list(j.quality_issues) if j else ["(judge did not return verdict for this mod)"]),
            type_match=(j.type_match if j else "NO"),
            ambiguity_match=(j.ambiguity_match if j else "NO"),
            events_test_mod=(j.events_test_mod if j else "NO"),
        ))

    return SampleModificationValidation(
        sample_id=sample.id,
        workflow_id=sample.sample_id or "",
        n_modifications=len(sample.modifications),
        n_events=len(sample.events),
        modification_verdicts=verdicts,
        overall_quality=judgement.overall_quality,
        overall_issues=list(judgement.overall_issues or []),
        overall_reasoning=judgement.reasoning,
        aggregate_health=_aggregate_health(verdicts),
        aggregate_quality=_aggregate_quality(verdicts, judgement.overall_quality),
    )


def _print_summary(results: list[SampleModificationValidation]) -> None:
    from collections import Counter
    health = Counter(r.aggregate_health for r in results)
    quality = Counter(r.aggregate_quality for r in results)
    overall_q = Counter(r.overall_quality for r in results)

    # Per-mod stats
    all_mods = [v for r in results for v in r.modification_verdicts]
    type_match = Counter(v.type_match for v in all_mods)
    amb_match = Counter(v.ambiguity_match for v in all_mods)
    events_test = Counter(v.events_test_mod for v in all_mods)

    print("\n" + "=" * 70)
    print(f"Modification validation — {len(results)} samples, {len(all_mods)} modifications")
    print("=" * 70)
    print("Health (deterministic):")
    print(f"  OK:        {health.get('OK', 0):3d}")
    print(f"  ISSUES:    {health.get('ISSUES', 0):3d}")
    print("Overall quality (LLM, per sample):")
    print(f"  GOOD:      {overall_q.get('GOOD', 0):3d}")
    print(f"  ADEQUATE:  {overall_q.get('ADEQUATE', 0):3d}")
    print(f"  POOR:      {overall_q.get('POOR', 0):3d}")
    print("Aggregate (per sample):")
    print(f"  GOOD:      {quality.get('GOOD', 0):3d}")
    print(f"  ADEQUATE:  {quality.get('ADEQUATE', 0):3d}")
    print(f"  POOR:      {quality.get('POOR', 0):3d}")
    print("Per-modification axis matches:")
    print(f"  type_match YES/NO:       {type_match.get('YES', 0):3d} / {type_match.get('NO', 0):3d}")
    print(f"  ambiguity_match YES/NO:  {amb_match.get('YES', 0):3d} / {amb_match.get('NO', 0):3d}")
    print(f"  events_test_mod Y/P/N:   "
          f"{events_test.get('YES', 0):3d} / {events_test.get('PARTIAL', 0):3d} / {events_test.get('NO', 0):3d}")
    print()

    flagged = [
        r for r in results
        if r.aggregate_health == "ISSUES" or r.aggregate_quality == "POOR"
    ]
    if flagged:
        print(f"Flagged for review ({len(flagged)}):")
        for r in flagged:
            n_health = sum(len(v.health_issues) for v in r.modification_verdicts)
            n_poor = sum(1 for v in r.modification_verdicts if v.quality == "POOR")
            print(
                f"  {r.sample_id:<55} "
                f"health={r.aggregate_health:<6} overall={r.overall_quality:<8} "
                f"quality={r.aggregate_quality:<8} "
                f"(health_issues={n_health} poor_mods={n_poor}/{r.n_modifications})"
            )
        print()


def main_with_args(args: argparse.Namespace) -> int:
    samples: list[Sample] = load_jsonl(args.samples, Sample)
    prompt_template = _load_prompt()

    if args.filter:
        samples = [s for s in samples if s.id in set(args.filter)]
    if args.limit:
        samples = samples[: args.limit]

    if not samples:
        print("No samples to validate.", file=sys.stderr)
        return 1

    output_path = args.output or args.samples.with_name(
        args.samples.stem + "__modification_validation.jsonl"
    )

    llm = create_llm(provider=args.provider, model=args.judge_model, temperature=0.0)

    results: list[SampleModificationValidation] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_validate_sample, llm, s, prompt_template): s for s in samples
        }
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # pragma: no cover
                print(f"  ✗ {s.id}: judge crashed — {exc}", file=sys.stderr)
                continue
            results.append(result)
            marker = {"GOOD": "✓", "ADEQUATE": "·", "POOR": "✗"}.get(result.aggregate_quality, "?")
            print(f"  {marker} {result.sample_id:<55} {result.aggregate_quality}")

    results.sort(key=lambda r: r.sample_id)

    with open(output_path, "w") as f:
        for r in results:
            f.write(r.model_dump_json() + "\n")

    _print_summary(results)
    print(f"Wrote {len(results)} validation records to {output_path}")

    if any(r.aggregate_quality == "POOR" for r in results) and not args.no_fail:
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--samples", required=True, type=Path,
                        help="Path to samples JSONL (Sample records).")
    parser.add_argument("--provider", default=os.environ.get("LLM_PROVIDER", "azure"))
    parser.add_argument("--judge-model", default="gpt-5.4")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--filter", nargs="+", default=None)
    parser.add_argument("--no-fail", action="store_true",
                        help="Exit 0 even when POOR samples are present.")
    args = parser.parse_args()
    sys.exit(main_with_args(args))


if __name__ == "__main__":
    main()
