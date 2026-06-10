"""
Automated pre-upload verifier for state-scenario samples.

Runs the recurring annotation feedback as automated checks BEFORE release, so the issues
reviewers kept catching are caught locally instead:

  - DETERMINISTIC checks: coherence (invariant exercised + reset), concurrent-pair positioning,
    no pre_mod, every event timed, seed->mock determinism, modification present + exercised,
    rate_limit per-key (>=2 SKUs) + no base/post key-window collision, post-mod after base,
    minimal entity count, no "State constraint" leakage.
  - LLM-JUDGE (optional, --judge): the SEMANTIC/fidelity issues a regex can't see — fidelity to
    the workflow's real mechanism, actor-chain clarity, seed completeness, expect correctness.

Usage:
    python -m src.data.verify_samples -i outputs/.../workflows-mods.jsonl            # deterministic
    python -m src.data.verify_samples -i outputs/.../workflows-mods.jsonl --judge    # + LLM-judge
Exit code is non-zero if any sample is flagged — so it can gate a release.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.data.schema import Sample, SampleVerdict
from src.data.generate_state_constraints import coherence_issues, concurrent_pair_issues

_VERIFY_PROMPT = Path("config/prompts/data-gen/verify_sample.yaml")


def _absday(when: str) -> int:
    m = re.match(r"W(\d+)-(\d+)", when or "")
    return (int(m.group(1)) - 1) * 7 + int(m.group(2)) if m else 0


def _skus(events) -> set:
    return set(re.findall(r"\b[A-Z]\d{2,}-[A-Z0-9]+\b", " ".join(e.input for e in events)))


def deterministic_issues(s: Sample) -> list[str]:
    """The structural checks — fast, free, run on every sample."""
    issues: list[str] = []
    sc = s.state_constraint
    base = [e for e in s.events if e.role == "base"]
    post = [e for e in s.events if e.role == "post_mod"]
    pre = [e for e in s.events if e.role == "pre_mod"]
    ct = getattr(sc.type, "value", sc.type) if sc else None

    if sc:
        texts = [f"{e.expect.action} {e.expect.reason or ''}" for e in base if e.expect]
        issues += coherence_issues(texts, ct)
        evd = [{"input": e.input, "when": e.when or "",
                "action": e.expect.action if e.expect else "",
                "reason": (e.expect.reason if e.expect else "") or "",
                "concurrent_group": e.concurrent_group} for e in base]
        issues += concurrent_pair_issues(evd, sc.threshold or "", ct)
        if not s.modifications:
            issues.append("no modification")
        if not post:
            issues.append("modification not exercised (no post_mod events)")

    if any("state constraint" in (st or "").lower() for st in s.steps):
        issues.append("a step literally says 'State constraint'")
    if pre:
        issues.append(f"{len(pre)} pre_mod event(s) present (should be 0)")
    untimed = [e.id for e in s.events if not e.when]
    if untimed:
        issues.append(f"events without a timestamp: {untimed[:3]}")
    if s.seed:
        reads = [t for t in s.tools if t.tool_name.endswith("_data")]
        if reads and not all(t.response_template == s.seed for t in reads):
            issues.append("read-service mock data != seed (seed->mock not deterministic)")
        try:
            d = json.loads(s.seed)
            for lk in ("reps", "sales_reps", "approvers"):
                if isinstance(d.get(lk), list) and len(d[lk]) > 3:
                    issues.append(f"seed.{lk} has {len(d[lk])} entities (>3, too much traffic)")
        except Exception:
            issues.append("seed is not valid JSON")

    if ct in ("rate_limit", "trigger", "dedup"):
        from src.data.scenario_builder import _parse_window_days
        D = _parse_window_days(sc.threshold)
        # DOMAIN-GENERIC: prefer the declared limit-tracked keys (SKUs/categories/contacts);
        # the SKU-code regex is only a legacy fallback for samples that predate `keys`.
        def keys_in(events):
            if s.keys:
                return {k for k in s.keys if any(k in e.input for e in events)}
            return _skus(events)
        bkeys = keys_in(base)
        if len(bkeys) < 2:
            issues.append(f"{ct} base exercises <2 distinct keys (per-key generalization not shown)")
        # a shared key is only a conflict if the base + post events for it fall within the window
        # (dedup windows are MINUTES — post-mod lands on a later day, never in-window)
        if ct != "dedup":
            for k in bkeys & keys_in(post):
                bd = [_absday(e.when) for e in base if k in e.input and e.when]
                pd = [_absday(e.when) for e in post if k in e.input and e.when]
                if bd and pd and min(pd) - max(bd) < D:
                    issues.append(f"base & post-mod reuse key {k} within the {D}-day window (conflict)")

    if base and post:
        bd = max((_absday(e.when) for e in base if e.when), default=0)
        pd = min((_absday(e.when) for e in post if e.when), default=10 ** 6)
        if pd <= bd:
            issues.append("post_mod events overlap or precede the base events")

    # the irrelevant event must be a REAL in-domain request (no placeholder id), naming a seed entity
    for e in [ev for ev in s.events if ev.role == "irrelevant"]:
        if re.search(r"\bIRR[-_]", e.input):
            issues.append(f"irrelevant event {e.id} uses a placeholder id (IRR-…), not a real domain id")
        if ct == "rate_limit":
            named = _skus([e])
            seed_skus = set(re.findall(r'"sku"\s*:\s*"([^"]+)"', s.seed or ""))
            known = seed_skus | set(s.keys)
            if named and known and not (named & known):
                issues.append(f"irrelevant event names key {sorted(named)} absent from the seed/keys")
    # the workflow must cover ALL the invariant's entities — steps that name SOME members of the
    # rotation but not others mean the system was grounded too narrowly (e.g. monitoring one
    # channel of three). Steps naming NO member (fully generic phrasing) are fine.
    ents = s.entities or []
    if len(ents) > 1 and s.steps:
        steps_text = " ".join(s.steps)
        mentioned = [x for x in ents if x in steps_text]
        if mentioned and len(mentioned) < len(ents):
            issues.append(f"steps specialize to {mentioned} but the invariant covers all of {ents}")
        # the same narrowing in the object graph: behaviors scoping to a strict subset
        obj_text = " ".join(f"{o.role or ''} {o.behavior or ''}" for o in s.objects)
        obj_mentioned = [x for x in ents if x in obj_text]
        if obj_mentioned and len(obj_mentioned) < len(ents):
            issues.append(f"object behaviors specialize to {obj_mentioned} but the invariant covers all of {ents}")

    # events are RAW STIMULI — "is detected/captured/processed" is the system's job, not the event's
    processed = [e.id for e in s.events if e.role in ("base", "post_mod")
                 and re.search(r"\bis (detected|captured|processed|triaged)\b", e.input, re.I)]
    if processed:
        issues.append(f"events phrased as system processing, not raw stimulus: {processed[:4]}")

    # cap: the approval request is emailed to the manager — approvers must carry email addresses
    if ct == "cap" and s.seed:
        try:
            ap = json.loads(s.seed).get("approvers") or []
            if ap and not all(isinstance(a, dict) and a.get("email") for a in ap):
                issues.append("cap seed approvers are missing email addresses (the request is emailed to them)")
        except Exception:
            pass
    return issues


def _summary(s: Sample) -> str:
    """Compact view of the sample for the LLM-judge."""
    base = [e for e in s.events if e.role == "base"]
    post = [e for e in s.events if e.role == "post_mod"]
    def ev_line(e):
        x = f"  [{e.id}] when={e.when} cg={e.concurrent_group} input: {e.input}"
        if e.expect:
            x += f"\n        expect: {e.expect.action}"
            if e.expect.reason:   # include reason so the FLIP marker is visible to the judge
                x += f"\n        reason: {e.expect.reason}"
        return x
    parts = [f"Name: {s.name}", f"Invariant under test: {s.state_constraint.description if s.state_constraint else '-'}",
             f"Threshold: {s.state_constraint.threshold if s.state_constraint else '-'}",
             "Steps:\n" + "\n".join(f"  - {st}" for st in s.steps),
             "Objects: " + ", ".join(f"{o.object_id}" for o in s.objects),
             "Seed: " + (s.seed or "(none)"),
             "Base events:\n" + "\n".join(ev_line(e) for e in base),
             "Modification: " + (s.modifications[0].intent if s.modifications else "(none)"),
             "Post-mod events:\n" + "\n".join(ev_line(e) for e in post)]
    return "\n\n".join(parts)


def judge_issues(s: Sample, llm, prompt_tmpl: str) -> list[str]:
    from src.data.utils import generate_with_retries
    prompt = prompt_tmpl.replace("{SAMPLE}", _summary(s))
    res = generate_with_retries(llm=llm, prompt=prompt, response_model=SampleVerdict,
                                item_id=f"verify-{s.id}", validator=lambda r: True)
    return [] if res is None or res.passed else list(res.issues)


def verify(path: Path, judge: bool = False, provider=None, model="gpt-5.4") -> int:
    samples = [Sample.model_validate_json(l) for l in open(path) if l.strip()]
    llm = prompt_tmpl = None
    if judge:
        from src.data.llm import create_llm
        from src.data.utils import infer_provider, load_prompt_template
        provider = provider or infer_provider(model)
        llm = create_llm(provider=provider, model=model, temperature=0.0)
        prompt_tmpl = load_prompt_template(_VERIFY_PROMPT)["user_prompt"]

    # Deterministic issues BLOCK a release (reliable arithmetic); judge issues are ADVISORY
    # (the LLM-judge is noisy — real findings mixed with miscounts), printed for human review.
    blocking = 0
    for s in samples:
        det = deterministic_issues(s)
        jud = [f"(judge, advisory) {i}" for i in judge_issues(s, llm, prompt_tmpl)] if judge else []
        status = "OK" if not (det or jud) else f"{len(det)} blocking, {len(jud)} advisory"
        print(f"\n[{s.id}] {status}")
        for i in det:
            print(f"   ✗ {i}")
        for i in jud:
            print(f"   · {i}")
        blocking += bool(det)
    from collections import Counter
    fam = Counter(getattr(s.state_constraint.type, "value", s.state_constraint.type)
                  for s in samples if s.state_constraint)
    print(f"\nInvariant families: " + ", ".join(f"{k}={v}" for k, v in fam.most_common())
          + ("   ⚠ low variety — consider re-running some templates" if len(fam) <= 1 < len(samples) else ""))
    print(f"=== {len(samples) - blocking}/{len(samples)} pass the BLOCKING (deterministic) gate; "
          f"{blocking} blocked. Judge notes above are advisory. ===")
    return blocking


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pre-upload verifier for state-scenario samples.")
    p.add_argument("--input", "-i", type=Path, required=True, help="workflows-mods.jsonl")
    p.add_argument("--judge", action="store_true", help="also run the LLM-judge (semantic/fidelity)")
    p.add_argument("--provider", "-p", default=None)
    p.add_argument("--model", "-m", default="gpt-5.4")
    return p


def main():
    a = build_parser().parse_args()
    sys.exit(1 if verify(a.input, judge=a.judge, provider=a.provider, model=a.model) else 0)


if __name__ == "__main__":
    main()
