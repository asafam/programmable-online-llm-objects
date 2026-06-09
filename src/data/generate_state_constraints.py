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
    EventExpect,
    GeneratedScenarioSpec,
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
    "not completed", "is not approved", "no approval", "reject", "declin", "refus", "withheld",
    "not allowed", "over the limit", "rate-limit", "held for", "cannot take effect", "is not made",
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


def _trim_seed(seed_str: str, mx: int = 3) -> str:
    """Keep scenarios SMALL (less agent traffic): cap each entity list (reps / approvers / SKUs)
    to `mx` — the minimum needed to demonstrate the invariant. SKUs stay >=2 so the per-SKU
    generalization still shows."""
    import json as _json
    try:
        d = _json.loads(seed_str)
    except Exception:
        return seed_str
    cat = d.get("catalog") if isinstance(d.get("catalog"), dict) else d
    changed = False
    for container, lk in [(d, "reps"), (d, "sales_reps"), (d, "approvers"), (cat, "skus")]:
        if isinstance(container, dict) and isinstance(container.get(lk), list) and len(container[lk]) > mx:
            container[lk] = container[lk][:mx]
            changed = True
    return _json.dumps(d) if changed else seed_str


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
    # A non-pair accept AT the pair's timestamp also consumes a slot (<=, not <), so count it —
    # otherwise an accepted same-key request sharing the pair's `when` is missed (inventory SC002).
    prior = sum(1 for e in events
                if not e.get("concurrent_group")
                and (not e.get("when") or e["when"] <= pair_when)
                and any(k in e["input"] for k in key)
                and not _is_blocked(f"{e.get('action', '')} {e.get('reason', '')}"))
    if prior != limit - 1:
        return [f"concurrent pair (key {sorted(key)[:2]}) has {prior} prior accepted same-key "
                f"request(s); needs limit-1={limit - 1} so the pair races for the last slot "
                f"(else one is wrongly blocked/allowed)"]
    return []


def _parse_limit(threshold: str, default: int = 2) -> int:
    m = _re.search(r"\d[\d,]*", threshold or "")   # handle thousands separators ("$50,000" → 50000)
    return int(m.group().replace(",", "")) if m else default


def _seed_reps(seed_str: str):
    """Parse the roster (name, position, daily_cap) out of the structured seed. None if absent."""
    import json as _json
    try:
        d = _json.loads(seed_str)
    except Exception:
        return None
    reps = d.get("reps") or d.get("roster") or d.get("representatives")
    if not isinstance(reps, list) or not reps:
        return None
    out = []
    for r in reps:
        if isinstance(r, dict) and r.get("name"):
            cap = r.get("daily_lead_cap") or r.get("daily_cap") or r.get("cap")
            out.append((r["name"], r.get("position", len(out) + 1), cap))
    return out or None


def _lead_ref(text: str) -> str:
    m = _re.search(r"\b([A-Z]{2,}-?\d[\w/-]*|Lead\s+#\d+|#\d+)\b", text or "")
    return m.group(1) if m else "the lead"


def _fill_outcome(outcomes, role, fallback, **vals):
    """Fill the LLM-provided OUTCOME template for `role` (so the action wording is domain-correct
    for ANY workflow); fall back to the built-in text when the LLM didn't provide one."""
    t = (outcomes or {}).get(role)
    if not t:
        return fallback
    for k, v in vals.items():
        t = t.replace("{" + k + "}", str(v))
    return _re.sub(r"\{[A-Z_]+\}", "", t).strip()


def simulate_rotation(seed_str: str, events: list[dict], threshold: str,
                      outcomes: dict | None = None, unit: str = "assignment",
                      flip_old_limit: int | None = None) -> list[dict]:
    """DETERMINISTIC round-robin/counter simulation. events: dicts {id, input, when,
    concurrent_group}. Processes in (when, id) order, assigns each lead to the next eligible rep
    in rotation order (under the per-day cap, daily reset), holds when all are capped. The action
    wording comes from the LLM `outcomes` templates (domain-generic) with a built-in fallback.
    `flip_old_limit` (mod scenario): when an allowed event exceeds the OLD limit, the reason marks
    it as the FLIP (would have been blocked before the modification). Empty list if no roster."""
    reps = _seed_reps(seed_str)
    if not reps:
        return []
    reps.sort(key=lambda r: r[1])
    names = [r[0] for r in reps]
    dcap = _parse_limit(threshold)
    # The THRESHOLD is the invariant — use it as the cap (the seed's per-rep cap is just reference
    # data, and for a modification the threshold changes while the seed still shows the old cap).
    caps = {nm: dcap for nm in names}
    n = len(names)
    order = sorted(range(len(events)), key=lambda i: (events[i].get("when", ""), events[i].get("id", "")))
    res: dict[int, dict] = {}
    counts: dict[str, int] = {}
    cur_day = None
    ptr = 0
    for i in order:
        e = events[i]
        day = (e.get("when", "") or "").split("T")[0]
        if day != cur_day:
            cur_day, counts = day, {nm: 0 for nm in names}
        assigned = None
        for step in range(n):
            cand = names[(ptr + step) % n]
            if counts[cand] < caps[cand]:
                assigned = cand
                ptr = (ptr + step + 1) % n
                counts[cand] += 1
                break
        ref = _lead_ref(e.get("input", ""))
        if assigned:
            flip = (f" THIS IS THE FLIP: the original cap of {flip_old_limit} would have HELD this "
                    f"{counts[assigned]}{'th' if 4 <= counts[assigned] % 100 <= 20 else {1:'st',2:'nd',3:'rd'}.get(counts[assigned] % 10, 'th')} "
                    f"same-day {unit} for {assigned}, but the modification (cap {caps[assigned]}) ALLOWS it."
                    if flip_old_limit and counts[assigned] > flip_old_limit else "")
            res[i] = {"action": _fill_outcome(outcomes, "allowed",
                        f"{ref} IS assigned to {assigned} and the {unit} is posted.", ID=ref, ENTITY=assigned),
                      "reason": f"{assigned} is the next eligible in rotation and is under the per-period "
                                f"cap of {caps[assigned]} (now {counts[assigned]} of {caps[assigned]}).{flip}"}
        else:
            res[i] = {"action": _fill_outcome(outcomes, "blocked",
                        f"{ref} is NOT assigned to any rep and is held; no {unit} is posted.", ID=ref),
                      "reason": f"every entity has reached the per-period cap of {dcap}, so it is held "
                                f"until the cap resets."}
    return [res[i] for i in range(len(events))]


def rotation_scenario_valid(seed_str: str, events: list[dict], threshold: str) -> bool:
    """For a counter/rotation scenario: the simulation must show the concurrent pair as a real
    RACE (exactly one of the two held), at least one held overall (cap exercised), and >=2 days
    (the daily reset is exercised)."""
    sim = simulate_rotation(seed_str, events, threshold)
    if not sim:
        return False
    pair = [i for i, e in enumerate(events) if e.get("concurrent_group")]
    if len(pair) != 2:
        return False
    held = lambda s: "not assigned" in s["action"].lower() or "is held" in s["action"].lower()
    if sum(1 for i in pair if held(sim[i])) != 1:    # exactly one of the pair held → race
        return False
    if not any(held(s) for s in sim):                # the cap is exercised
        return False
    days = {(e.get("when", "") or "").split("T")[0] for e in events}
    return len(days) >= 2                            # the daily reset is exercised


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


def _seed_approvers(seed_str: str):
    import json as _json
    try:
        ap = _json.loads(seed_str).get("approvers")
    except Exception:
        return None
    if isinstance(ap, list):
        return [a if isinstance(a, str) else a.get("name") for a in ap if a]
    return None


def _first_key(seed_str: str):
    import json as _json
    try:
        d = _json.loads(seed_str)
    except Exception:
        return None
    skus = (d.get("catalog") or {}).get("skus") if isinstance(d.get("catalog"), dict) else d.get("skus")
    if isinstance(skus, list) and skus:
        f = skus[0]
        return f.get("sku") if isinstance(f, dict) else f
    return None


def _run_builder(ct, seed, threshold, phrasings, decorations, key="", unit="", **kw) -> list:
    """Construct the phrase closures from the LLM's phrasing templates + decorations, then call
    the family builder. Shared by the base scenario AND the modification scenario."""
    from src.data.scenario_builder import (
        build_cap_scenario, build_counter_scenario, build_rate_limit_scenario,
    )
    tmpl = {p.role: p.template for p in phrasings}
    decos = decorations or []

    def fill(t, **vals):
        s = t or ""
        for k, v in vals.items():
            s = s.replace("{" + k + "}", str(v))
        return _re.sub(r"\{[A-Z_]+\}", "", s).strip()

    def deco_for(idstr):
        if not decos:
            return ""
        m = _re.search(r"\d+", idstr or "")
        return decos[(int(m.group()) if m else 0) % len(decos)]

    # outcome wording: pass the full template map (the builders look up allowed/blocked/approved/
    # held/submitted) so the action text is domain-correct for any workflow; fallback inside builder.
    if ct == "counter":
        req = tmpl.get("request") or tmpl.get("submit") or "A new lead {ID} arrives {DECO}."
        phrase = lambda lid, d: fill(req, ID=lid, DECO=(d if isinstance(d, str) else deco_for(lid)))
        return build_counter_scenario(seed, threshold, phrase, decos or [""],
                                      outcomes=tmpl, unit=unit or "lead assignment", **kw)
    if ct == "rate_limit":
        req = tmpl.get("request") or "A request {ID} arrives for {KEY} {DECO}."
        k = key or _first_key(seed) or "SKU-1"
        phrase = lambda rid, blk, kk: fill(req, ID=rid, KEY=kk, DECO=deco_for(rid))
        return build_rate_limit_scenario(seed, threshold, k, phrase,
                                         outcomes=tmpl, unit=unit or "reorder", **kw)
    if ct == "cap":
        sub = tmpl.get("submit") or "Quote {ID} is submitted requesting a ${AMOUNT} discount {DECO}."
        app = tmpl.get("approve") or "{APPROVER} approves {ID}."
        approvers = _seed_approvers(seed) or ["the approver"]
        submit_phrase = lambda qid, amt: fill(sub, ID=qid, AMOUNT=f"{amt:,}", DECO=deco_for(qid))
        approve_phrase = lambda qid, ap: fill(app, ID=qid, APPROVER=ap)
        return build_cap_scenario(seed, threshold, submit_phrase, approve_phrase, approvers,
                                  outcomes=tmpl, unit=unit or "approval", **kw)
    return []


def _build_scenario(gen: GeneratedScenarioSpec) -> list:
    """CODE builds the base request sequence + derives expects from the LLM's seed + phrasing."""
    return _run_builder(getattr(gen.constraint_type, "value", gen.constraint_type),
                        gen.seed, gen.threshold, gen.phrasings, gen.decorations, gen.key, gen.unit)


def _base_cap_total(base_events) -> int:
    """Sum the approved discount amounts in the base cap scenario (the running total it ended at)."""
    total = 0
    for e in base_events:
        if not e.expect:
            continue
        a = e.expect.action or ""
        if "approves" in a and "BUT" not in a and "held" not in a.lower():
            m = _re.search(r"\$([\d,]+)", a)
            if m:
                total += int(m.group(1).replace(",", ""))
    return total


def _ev_abs_day(when: str) -> int:
    m = _re.match(r"W(\d+)-(\d+)", when or "")
    return (int(m.group(1)) - 1) * 7 + int(m.group(2)) if m else 0


def _abs_to_day(absday: int) -> str:
    absday = max(1, absday)
    return f"W{(absday - 1) // 7 + 1:02d}-{(absday - 1) % 7 + 1}"


def _other_key(seed_str: str, exclude: str):
    import json as _json
    try:
        d = _json.loads(seed_str)
    except Exception:
        return None
    skus = (d.get("catalog") or {}).get("skus") if isinstance(d.get("catalog"), dict) else d.get("skus")
    for s in (skus or []):
        code = s.get("sku") if isinstance(s, dict) else s
        if code and code != exclude:
            return code
    return None


def build_mod_scenario(spec, mod_type: str):
    """CODE-generate the modification + post-mod events for a state scenario: derive the MODIFIED
    threshold and re-run the family builder so the post-mod scenario EXERCISES the new rule (with a
    flip — allowed-under-new where it was blocked-under-old). Placed AFTER all base events to avoid
    date conflicts: rate_limit also uses a DIFFERENT key (no rolling-window overlap), counter lands
    on a fresh day (daily reset), cap continues the base's running total. Returns (intent, mod_when,
    post_mod_events)."""
    from src.data.scenario_builder import _parse_window_days
    ct = spec.state_constraint.type.value
    old_thr = spec.state_constraint.threshold
    old_limit = _parse_limit(old_thr)
    expand = mod_type != "restriction"

    latest = max((_ev_abs_day(e.when) for e in spec.base_events if e.when), default=7)
    mod_abs = latest + 1                                     # mod fires after the whole base scenario
    mod_when = _abs_to_day(mod_abs) + "T10:30"
    # post-mod starts after the mod, and for rate_limit fully past the base rolling window
    window = _parse_window_days(old_thr) if ct == "rate_limit" else 1
    base_abs = mod_abs + window + 1
    post_day = _abs_to_day(base_abs)
    key = spec.key

    if ct == "cap":
        new_limit = int(round(old_limit * 1.5)) if expand else int(round(old_limit * 0.67))
        kw = {"base_day": post_day, "starting_total": _base_cap_total(spec.base_events)}
        intent = (f"Starting now, raise the quarter's approved-discount cap to ${new_limit:,} "
                  f"(from ${old_limit:,})." if expand else
                  f"Starting now, lower the quarter's approved-discount cap to ${new_limit:,} (from ${old_limit:,}).")
    else:
        new_limit = old_limit + 1 if expand else max(1, old_limit - 1)
        if ct == "rate_limit":
            key = _other_key(spec.seed, spec.key) or spec.key   # different SKU → no window overlap
            kw = {"base_day": post_day}
            unit = "reorders for the same product within the rolling window"
        else:
            kw = {"base_day": post_day, "reset_day": _abs_to_day(base_abs + 1)}  # distinct later day
            unit = "new lead assignments per representative per day"
        intent = (f"Starting now, allow up to {new_limit} {unit} instead of {old_limit}." if expand else
                  f"Starting now, allow only {new_limit} {unit} instead of {old_limit}.")

    new_thr = _re.sub(r"\d[\d,]*", str(new_limit), old_thr, count=1)
    kw["id_offset"] = 100   # post-mod request ids start at 100+ so they never reuse a base id
    kw["flip_old_limit"] = old_limit   # mark the event allowed-under-new but blocked-under-old (the FLIP)
    events = _run_builder(ct, spec.seed, new_thr, spec.phrasings, spec.decorations, key, spec.unit, **kw)
    for i, e in enumerate(events, 1):
        e.id = f"PM{i:03d}"
        e.role = "post_mod"
        e.after_mod_ids = ["M001"]

    # IRRELEVANT post-mod event: a request OUTSIDE the invariant, handled normally — proves the
    # modification is scoped and does NOT change unrelated behavior.
    irr_tmpl = {p.role: p.template for p in spec.phrasings}.get("irrelevant")
    if irr_tmpl:
        last_abs = max((_ev_abs_day(e.when) for e in events if e.when), default=_ev_abs_day(mod_when))
        irr_input = _re.sub(r"\{[A-Z_]+\}", "", irr_tmpl.replace("{ID}", "IRR-0001")).strip()
        events.append(SpecEventWithExpect(
            id="IRR001", call_type="send_event", source="__external__", input=irr_input,
            when=_abs_to_day(last_abs + 1) + "T09:00", role="irrelevant", after_mod_ids=["M001"],
            expect=EventExpect(
                action="The request is handled normally; the modification does not change it.",
                reason=f"this request is outside the {spec.unit or 'gated'} invariant, so the rule "
                       f"change does not apply to it.")))
    return intent, mod_when, events


def _process_template(llm, template: dict, prompt_template: str) -> tuple[WorkflowSpec, bool]:
    """INFUSE (before grounding): the LLM supplies ONLY realism — the invariant type/threshold,
    the structured seed, phrasing templates, and a decoration pool. CODE then builds the request
    sequence and derives every expect by simulation (scenario_builder), so the base scenario is
    logically correct BY CONSTRUCTION. Returns (spec, ok)."""
    spec = _seed_spec(template)
    prompt = prompt_template.replace("{WORKFLOW}", format_template(template))
    gen = generate_with_retries(
        llm=llm, prompt=prompt, response_model=GeneratedScenarioSpec,
        item_id=template["id"],
        # The LLM only needs to give a valid type/threshold, a JSON seed, and phrasing.
        validator=lambda r: bool(r.threshold) and _valid_seed(r.seed) and bool(r.phrasings),
    )
    if gen is None:
        return spec, False

    gen.seed = _trim_seed(gen.seed)            # cap entities (reps/approvers/skus) → small scenarios
    spec.seed = gen.seed
    spec.phrasings = gen.phrasings
    spec.decorations = gen.decorations
    spec.key = gen.key
    spec.unit = gen.unit
    spec.state_constraint = StateConstraint(
        type=gen.constraint_type, threshold=gen.threshold, description=gen.description,
    )
    base_events = _build_scenario(gen)
    if not base_events:
        return spec, False
    spec.base_events = base_events

    # Sanity net: the code-built scenario should pass the coherence + pair checks by construction.
    texts = [f"{e.expect.action} {e.expect.reason or ''}" for e in base_events if e.expect]
    ct = getattr(gen.constraint_type, "value", gen.constraint_type)
    evd = [{"input": e.input, "when": e.when or "",
            "action": e.expect.action if e.expect else "",
            "reason": (e.expect.reason if e.expect else "") or "",
            "concurrent_group": e.concurrent_group} for e in base_events]
    issues = coherence_issues(texts, ct) + concurrent_pair_issues(evd, gen.threshold or "")
    if issues:
        tqdm.write(f"  [builder] {template['id']}: built scenario flagged: {issues}", file=sys.stderr)
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
