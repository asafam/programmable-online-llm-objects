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


def concurrent_pair_issues(events: list[dict], threshold: str, ct: str = "counter") -> list[str]:
    """The simultaneous pair only RACES if its key already has exactly (limit-1) accepted
    same-key requests in-window — then the two arrivals compete for the last slot (one
    accepted, one blocked). A pair on a FRESH key (0 prior) is wrong: both are within the
    limit, neither should block. events: dicts {input, when, action, reason, concurrent_group}.
    Conservative — returns [] when it can't confidently identify the key/limit.
    trigger/dedup have their own race semantics: exactly ONE of the pair fires the quorum /
    exactly ONE is deduplicated."""
    pair = [e for e in events if e.get("concurrent_group")]
    if len(pair) < 2:
        return []
    if ct in ("trigger", "dedup"):
        txt = lambda e: f"{e.get('action', '')} {e.get('reason', '')}".lower()
        marker = "is reached" if ct == "trigger" else "duplicate"
        hits = sum(1 for e in pair if marker in txt(e))
        if hits != 1:
            return [f"{ct}: the concurrent pair must race — exactly one of the two should "
                    f"{'fire the quorum' if ct == 'trigger' else 'be ignored as a duplicate'} "
                    f"(found {hits})"]
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


def _seed_reps(seed_str: str, entities: list | None = None):
    """The rotation roster as (name, position, cap) tuples — DOMAIN-GENERIC.
    Prefer the explicit `entities` list (the LLM names the rotation members in order); else scan
    the seed for ANY list of named members (reps, channels, agents, queues…), not a fixed key."""
    if entities:
        return [(name, i + 1, None) for i, name in enumerate(entities)]
    import json as _json
    try:
        d = _json.loads(seed_str)
    except Exception:
        return None
    candidates = []
    for v in (d.values() if isinstance(d, dict) else []):
        if isinstance(v, list) and v and all(isinstance(r, dict) and r.get("name") for r in v):
            candidates.append(v)
    if not candidates:
        return None
    # prefer a list whose members carry a rotation `position`; else the first named list
    members = next((c for c in candidates if all("position" in r for r in c)), candidates[0])
    out = []
    for r in members:
        cap = next((r[k] for k in r if isinstance(r.get(k), int) and k.endswith("_cap")), None) \
            or r.get("cap")
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
                      flip_old_limit: int | None = None, entities: list | None = None,
                      exempt: str | None = None, rule_off: bool = False) -> list[dict]:
    """DETERMINISTIC round-robin/counter simulation. events: dicts {id, input, when,
    concurrent_group}. Processes in (when, id) order, assigns each lead to the next eligible rep
    in rotation order (under the per-day cap, daily reset), holds when all are capped. The action
    wording comes from the LLM `outcomes` templates (domain-generic) with a built-in fallback.
    `flip_old_limit` (mod scenario): when an allowed event exceeds the OLD limit, the reason marks
    it as the FLIP (would have been blocked before the modification). `exempt` (mod scenario):
    that member ignores the cap — its beyond-cap assignments are the FLIP. Empty if no roster."""
    reps = _seed_reps(seed_str, entities)
    if not reps:
        return []
    reps.sort(key=lambda r: r[1])
    names = [r[0] for r in reps]
    dcap = _parse_limit(threshold)
    # The THRESHOLD is the invariant — use it as the cap (the seed's per-rep cap is just reference
    # data, and for a modification the threshold changes while the seed still shows the old cap).
    caps = {nm: (10 ** 9 if rule_off or nm == exempt else dcap) for nm in names}
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
            if rule_off and counts[assigned] > dcap:
                res[i] = {"action": _fill_outcome(outcomes, "allowed",
                            f"{ref} IS assigned to {assigned} and the {unit} is posted.", ID=ref, ENTITY=assigned),
                          "reason": f"the per-period cap was RETIRED by the modification, so assignment "
                                    f"continues beyond the old cap of {dcap} (now {counts[assigned]} for "
                                    f"{assigned}). THIS IS THE FLIP: under the retired cap of {dcap} this "
                                    f"{unit} would have been HELD."}
            elif assigned == exempt and counts[assigned] > dcap:
                res[i] = {"action": _fill_outcome(outcomes, "allowed",
                            f"{ref} IS assigned to {assigned} and the {unit} is posted.", ID=ref, ENTITY=assigned),
                          "reason": f"{assigned} is exempt from the per-period cap, so the {unit} proceeds "
                                    f"even beyond {dcap} (now {counts[assigned]})."
                                    f" THIS IS THE FLIP: without the exemption, {assigned} would be at the "
                                    f"cap of {dcap} and this {unit} would have been HELD; the modification "
                                    f"exempts {assigned}, so it is allowed."}
            else:
                res[i] = {"action": _fill_outcome(outcomes, "allowed",
                            f"{ref} IS assigned to {assigned} and the {unit} is posted.", ID=ref, ENTITY=assigned),
                          "reason": f"{assigned} is the next eligible in rotation and is under the per-period "
                                    f"cap of {dcap} (now {counts[assigned]} of {dcap}).{flip}"}
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
    if constraint_type == "trigger":
        # INVERSE semantics: the invariant is exercised when the quorum FIRES the action at least
        # once AND at least one event only accumulates (below quorum).
        if not any("quorum" in x and "is reached" in x for x in texts):
            issues.append("trigger: no base event reaches the quorum (the action never fires)")
        if not any("below the quorum" in x for x in texts):
            issues.append("trigger: no accumulating event below the quorum (no state build-up shown)")
        return issues
    if constraint_type == "dedup":
        # the invariant is exercised when ≥1 repeat is IGNORED as a duplicate AND ≥1 same-key
        # repeat past the window is processed again (the window expires).
        if not any("duplicate" in x for x in texts):
            issues.append("dedup: no base event is ignored as a duplicate (invariant never exercised)")
        if not any("window has expired" in x or "window expired" in x for x in texts):
            issues.append("dedup: no same-key repeat past the window (window expiry never shown)")
        return issues
    if not any(any(b in x for b in _BLOCK_TERMS) for x in texts):
        issues.append("no base event blocks/holds/suppresses the gated action (invariant never exercised)")
    if constraint_type in ("counter", "rate_limit"):
        if not any(any(rt in x for rt in _RESET_TERMS) for x in texts):
            issues.append(f"{constraint_type}: no reset/window-expiry event (same key, later period, allowed)")
    return issues

# Retained for the pipeline opt-in flag's choices; the type is now CLASSIFIED by
# the LLM from the custodian's invariant, not script-assigned.
CONSTRAINT_TYPES = ["cap", "counter", "rate_limit", "trigger", "dedup"]


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


def _seed_sales_reps(seed_str: str):
    """[(person, manager)] pairs from the cap seed — DOMAIN-GENERIC: prefer "sales_reps", else
    scan ANY list of named members carrying a manager (employees, requesters, agents). Used so
    the system's routing of each request to the submitter's MANAGER is verifiable in the expect."""
    import json as _json
    try:
        d = _json.loads(seed_str)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    candidates = [d.get("sales_reps")] + [v for k, v in d.items() if k != "sales_reps"]
    for reps in candidates:
        if not (isinstance(reps, list) and reps
                and all(isinstance(r, dict) and r.get("name") and r.get("manager") for r in reps)):
            continue
        return [(r["name"], r["manager"]) for r in reps]
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


def _run_builder(ct, seed, threshold, phrasings, decorations, key="", unit="",
                 entities=None, keys=None, contacts=None, cap_scope="shared",
                 person_caps=None, qty_noun="", **kw) -> list:
    """Construct the phrase closures from the LLM's phrasing templates + decorations, then call
    the family builder. Shared by the base scenario AND the modification scenario.
    `entities` (counter) / `keys` (rate_limit) are the LLM-named domain values — the builders use
    them directly instead of guessing the seed's shape (which is domain-specific)."""
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

    def amount_for(idstr):
        m = _re.search(r"\d+", idstr or "")
        return f"{60 + (int(m.group()) if m else 0) * 35 % 440}"   # deterministic plausible value

    _people = [r[0] for r in (_seed_reps(seed, entities) or [])] or \
              [r for r, _m in (_seed_sales_reps(seed) or [])] or ["an employee"]

    def submitter_for(idstr):
        m = _re.search(r"\d+", idstr or "")
        return _people[(int(m.group()) if m else 0) % len(_people)]

    # outcome wording: pass the full template map (the builders look up allowed/blocked/approved/
    # held/submitted) so the action text is domain-correct for any workflow; fallback inside builder.
    if ct == "counter":
        req = tmpl.get("request") or tmpl.get("submit") or "A new request {ID} arrives {DECO}."
        phrase = lambda lid, d: fill(req, ID=lid, AMOUNT=amount_for(lid), SUBMITTER=submitter_for(lid), DECO=(d if isinstance(d, str) else deco_for(lid)))
        return build_counter_scenario(seed, threshold, phrase, decos or [""],
                                      outcomes=tmpl, unit=unit or "assignment", entities=entities, **kw)
    if ct == "rate_limit":
        req = tmpl.get("request") or "A request {ID} arrives for {KEY} {DECO}."
        k = key or (keys[0] if keys else None) or _first_key(seed) or "KEY-1"
        phrase = lambda rid, blk, kk: fill(req, ID=rid, KEY=kk, AMOUNT=amount_for(rid), SUBMITTER=submitter_for(rid), DECO=deco_for(rid))
        return build_rate_limit_scenario(seed, threshold, k, phrase,
                                         outcomes=tmpl, unit=unit or "request", keys=keys, contacts=contacts, **kw)
    if ct == "trigger":
        from src.data.scenario_builder import build_trigger_scenario
        req = tmpl.get("request") or "An event {ID} for {KEY} arrives {DECO}."
        k = key or (keys[0] if keys else None) or _first_key(seed) or "KEY-1"
        phrase = lambda rid, kk: fill(req, ID=rid, KEY=kk, AMOUNT=amount_for(rid), SUBMITTER=submitter_for(rid), DECO=deco_for(rid))
        return build_trigger_scenario(seed, threshold, k, phrase,
                                      outcomes=tmpl, unit=unit or "escalation", keys=keys, contacts=contacts, **kw)
    if ct == "dedup":
        from src.data.scenario_builder import build_dedup_scenario
        req = tmpl.get("request") or "A report {ID} about {KEY} arrives {DECO}."
        k = key or (keys[0] if keys else None) or _first_key(seed) or "KEY-1"
        phrase = lambda rid, kk: fill(req, ID=rid, KEY=kk, AMOUNT=amount_for(rid), SUBMITTER=submitter_for(rid), DECO=deco_for(rid))
        return build_dedup_scenario(seed, threshold, k, phrase,
                                    outcomes=tmpl, unit=unit or "report", keys=keys, contacts=contacts, **kw)
    if ct == "cap":
        sub = tmpl.get("submit") or "{SUBMITTER} submits request {ID} for ${AMOUNT} {DECO}."
        app = tmpl.get("approve") or "{APPROVER} reviews request {ID}."
        reps = _seed_sales_reps(seed)
        if not reps:                              # no chain in the seed → fall back to bare approvers
            reps = [("an account executive", a) for a in (_seed_approvers(seed) or ["the approver"])]
        submit_phrase = lambda qid, amt, rep: fill(
            sub, ID=qid, AMOUNT=f"{amt:,}", SUBMITTER=rep, SUBMITTER_EMAIL=_seed_email(seed, rep),
            DECO=deco_for(qid))
        approve_phrase = lambda qid, mgr: fill(app, ID=qid, APPROVER=mgr,
                                               APPROVER_EMAIL=_seed_email(seed, mgr))
        return build_cap_scenario(seed, threshold, submit_phrase, approve_phrase, reps,
                                  outcomes=tmpl, unit=unit or "approval",
                                  per_person=(cap_scope == "per_person"),
                                  person_caps=kw.pop("person_caps", None) or person_caps,
                                  qty_noun=qty_noun, **kw)
    return []


def _build_scenario(gen: GeneratedScenarioSpec) -> list:
    """CODE builds the base request sequence + derives expects from the LLM's seed + phrasing.
    For ANALYSIS workflows, a NEUTRAL event is woven in mid-stream — its content matches no rule
    term, the analysis classifies it 'neutral', and it does NOT count (the filter is visible in
    the base scenario, so not everything carries the counting label)."""
    events = _run_builder(getattr(gen.constraint_type, "value", gen.constraint_type),
                          gen.seed, gen.threshold, gen.phrasings, gen.decorations, gen.key, gen.unit,
                          entities=gen.entities, keys=gen.keys, contacts=gen.key_contacts,
                          cap_scope=gen.cap_scope, person_caps=gen.person_caps, qty_noun=gen.qty_noun)
    if events and gen.analysis_field and gen.irrelevant_deco:
        tmpl = {p.role: p.template for p in gen.phrasings}
        req = tmpl.get("request")
        if req:
            k = gen.key or (gen.keys[0] if gen.keys else "")
            txt = (req.replace("{ID}", "REQ-0050").replace("{KEY}", k)
                      .replace("{DECO}", gen.irrelevant_deco).replace("{AMOUNT}", "75"))
            txt = _re.sub(r"\{[A-Z_]+\}", "", txt).strip()
            first_when = events[0].when or "W01-1T09:00"
            neutral = SpecEventWithExpect(
                id="E050", call_type="send_event", source="__external__", input=txt,
                when=first_when[:-2] + "31", role="base",
                expect=EventExpect(
                    action=f"REQ-0050 is analyzed against the seeded {gen.analysis_field} rules — "
                           f"its text matches no '{gen.analysis_label or 'counting'}' term, so it "
                           f"is classified 'neutral' and does NOT count; normal handling proceeds.",
                    reason=f"the analysis is rules-based on the text; without a "
                           f"'{gen.analysis_label or 'counting'}' term this occurrence never enters "
                           f"the count — the rule only accumulates matching items."))
            events.insert(1, neutral)
    # BRANCH DEMONSTRATIONS: one event per DISTINCT handling path the steps describe (a positive
    # mention posts to #brand-wins; a known topic gets the instant KB fix) — none enters the count
    if events and gen.branch_demos:
        tmplb = {p.role: p.template for p in gen.phrasings}
        reqb = tmplb.get("request") or tmplb.get("submit")
        if reqb:
            kb = gen.key or (gen.keys[0] if gen.keys else "")
            first_when = events[0].when or "W01-1T09:00"
            for bi, bd in enumerate(gen.branch_demos, 1):
                txtb = (reqb.replace("{ID}", f"REQ-007{bi}").replace("{KEY}", kb)
                            .replace("{DECO}", bd.content).replace("{AMOUNT}", "85"))
                txtb = _re.sub(r"\{[A-Z_]+\}", "", txtb).strip()
                events.insert(1 + bi, SpecEventWithExpect(
                    id=f"E07{bi}", call_type="send_event", source="__external__", input=txtb,
                    when=first_when[:-2] + f"{32 + bi * 3:02d}", role="base",
                    expect=EventExpect(
                        action=bd.action,
                        reason=bd.reason or "this content matches a DIFFERENT handling path stated "
                               "in the steps; it is handled there and never enters the gated count.")))
    # FOLLOW-UP interaction (role="followup" phrasing): when the steps say the notified party
    # acts back (the assigned owner updates status from Slack), that interaction is a real event —
    # placed right after the first FIRED outcome, recorded against the open item (no new item)
    tmpl2 = {p.role: p.template for p in gen.phrasings}
    fu = tmpl2.get("followup")
    if events and fu:
        fired_idx = next((i for i, e in enumerate(events)
                          if e.expect and ("quorum of" in (e.expect.reason or "")
                                           and "is reached" in (e.expect.reason or ""))), None)
        if fired_idx is not None:
            k = gen.key or (gen.keys[0] if gen.keys else "")
            contact = (gen.key_contacts or {}).get(k) or "the assigned owner"
            txt = (fu.replace("{ID}", "REQ-0060").replace("{KEY}", k).replace("{CONTACT}", contact))
            txt = _re.sub(r"\{[A-Z_]+\}", "", txt).strip()
            when = events[fired_idx].when or "W01-1T12:00"
            hh = int(when.split("T")[1][:2]) + 1
            fup = SpecEventWithExpect(
                id="E060", call_type="send_event", source="__external__", input=txt,
                when=f"{when.split('T')[0]}T{hh:02d}:00", role="base",
                expect=EventExpect(
                    action=f"the status update from {contact} is recorded against the open "
                           f"{gen.unit or 'item'} for {k}; NO new {gen.unit or 'item'} is created.",
                    reason=f"this is the notified owner acting on the already-created item — the "
                           f"workflow records the update against it; it is not a new request and "
                           f"does not enter any count."))
            events.insert(fired_idx + 1, fup)
    return events


def _base_cap_total(base_events, person: str | None = None) -> int:
    """The running total the base cap scenario ended at, parsed from the CODE-AUTHORED reasons
    ("keeps ... running approved total at X") — never from the LLM-worded actions, whose verbs
    vary ("approves" missing once made a carried total silently 0). `person` restricts to one
    submitter's events (per-person caps track separate budgets)."""
    total = 0
    for e in base_events:
        if not e.expect or not e.expect.reason:
            continue
        r = e.expect.reason
        if person and person not in r:
            continue
        m = _re.search(r"running approved total at \$?([\d,]+)", r)
        if m:
            total = max(total, int(m.group(1).replace(",", "")))
    return total


def _ev_abs_day(when: str) -> int:
    m = _re.match(r"W(\d+)-(\d+)", when or "")
    return (int(m.group(1)) - 1) * 7 + int(m.group(2)) if m else 0


def _abs_to_day(absday: int) -> str:
    absday = max(1, absday)
    return f"W{(absday - 1) // 7 + 1:02d}-{(absday - 1) % 7 + 1}"


def _well_stocked_sku(seed_str: str):
    """The seed SKU that is WELL ABOVE its reorder level (no reorder needed) — the irrelevant
    event names it as a real, outside-the-invariant request."""
    import json as _json
    try:
        d = _json.loads(seed_str)
    except Exception:
        return None
    skus = (d.get("catalog") or {}).get("skus") if isinstance(d.get("catalog"), dict) else d.get("skus")
    for s in (skus or []):
        if isinstance(s, dict) and s.get("sku") and s.get("on_hand") is not None and s.get("reorder_level") is not None:
            if s["on_hand"] > s["reorder_level"]:
                return s["sku"]
    return None


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


VALID_MOD_TYPES = {           # (family → taxonomy types with a code-generable transform)
    "counter": ["temporal", "contextual", "exception", "correction", "expansion", "removal"],
    "rate_limit": ["temporal", "contextual", "exception", "correction", "expansion", "removal"],
    "trigger": ["temporal", "contextual", "exception", "correction", "expansion", "removal"],
    "dedup": ["temporal", "contextual", "exception", "correction", "expansion", "removal"],
    "cap": ["temporal", "correction", "expansion"],   # per-entity caps/retirement don't map cleanly
}


def _looser_threshold(ct: str, old_thr: str, old_limit: int):
    """The 'loosened' rule per family: counter/rate_limit/cap raise N; trigger LOWERS the quorum
    (fires sooner); dedup HALVES the window. Returns (new_thr, builder_flip_kwargs)."""
    from src.data.scenario_builder import _parse_window_minutes
    if ct == "dedup":
        m = _re.search(r"(\d+)([-\s]*(?:[a-z]+[-\s]+)?(?:minutes?|mins?|hours?|hrs?|days?)\b)",
                       old_thr, _re.I)
        new_n = max(1, int(m.group(1)) // 2) if m else 0
        new_thr = old_thr[:m.start(1)] + str(new_n) + old_thr[m.end(1):] if m else old_thr
        return new_thr, {"flip_old_window_min": _parse_window_minutes(old_thr)}
    if ct == "trigger":
        new_limit = max(2, old_limit - 1)
        return _re.sub(r"\d[\d,]*", str(new_limit), old_thr, count=1), {"flip_old_limit": old_limit}
    if ct == "cap":
        new_limit = int(round(old_limit * 1.5))
        return _re.sub(r"\d[\d,]*", str(new_limit), old_thr, count=1), {"flip_old_limit": old_limit}
    return _re.sub(r"\d[\d,]*", str(old_limit + 1), old_thr, count=1), {"flip_old_limit": old_limit}


def _seed_email(seed_str: str, name: str) -> str:
    """The seed email address for a named person — scanned across ALL people lists."""
    import json as _json
    try:
        d = _json.loads(seed_str)
    except Exception:
        return ""
    for v in (d.values() if isinstance(d, dict) else []):
        if isinstance(v, list):
            for p in v:
                if isinstance(p, dict) and p.get("name") == name and p.get("email"):
                    return p["email"]
    return ""


def _seed_person(seed_str: str):
    """A REAL person/owner from the seed for expansion's extra notification (approver > manager)."""
    import json as _json
    try:
        d = _json.loads(seed_str)
    except Exception:
        return None
    for a in (d.get("approvers") or []):
        if isinstance(a, dict) and a.get("name"):
            return f"{a['name']} — {a.get('title', 'approver')} per the seed ({a.get('email', 'by email')})"
    for r in (d.get("sales_reps") or []):
        if isinstance(r, dict) and r.get("manager"):
            return r["manager"]
    return None


def build_mod_scenario(spec, mod_type: str, mod_dim: str = None):
    """CODE-generate the modification + post-mod events for a state scenario. `mod_type` is one of
    the SIX taxonomy categories, each mapped to a deterministic transform of the invariant:
      temporal    — the changed rule applies only until an expiry; the post scenario shows the new
                    rule in force (flip) AND the original rule back after expiry
      contextual  — an attribute-based override: one named key gets a DIFFERENT limit; another key
                    shows the original rule unchanged
      exception   — one named entity is exempt; the builders add a non-exempt scope proof
      correction  — the threshold value is FIXED ("should be X, not Y"); flip under the corrected rule
      expansion   — NEW behavior added on the gated outcome (an extra notification); the gated
                    outcome visibly shows the addition
      removal     — the rule is retired; the formerly-gated event now proceeds (the flip)
    Unsupported (family, type) combos fall back to correction. The IRRELEVANT event is a request
    the workflow handles normally whose outcome is IDENTICAL before and after the modification.
    Returns (intent, mod_when, post_mod_events)."""
    from src.data.scenario_builder import _parse_window_days, _second_key
    ct = spec.state_constraint.type.value
    old_thr = spec.state_constraint.threshold
    old_limit = _parse_limit(old_thr)
    if mod_type not in VALID_MOD_TYPES.get(ct, []):
        mod_type = "correction"
    # contextual/exception require a SECOND DISTINCT key (the override/exempt key vs. one still
    # under the original rule) — with a single tracked key the two runs would silently collapse
    # onto the same key and contradict each other (observed: both quorums applied to one account)
    from src.data.scenario_builder import _second_key as _sk
    if (mod_type in ("contextual", "exception") and ct in ("rate_limit", "trigger", "dedup")
            and (_sk(spec.seed, spec.key, spec.keys) or spec.key) == spec.key):
        mod_type = "correction"

    # normalize the main key (an empty spec.key would make every key_= fallback collapse onto
    # keys[0] — the root cause of two contextual runs landing on the same account)
    if not spec.key and spec.keys:
        spec.key = spec.keys[0]
    latest = max((_ev_abs_day(e.when) for e in spec.base_events if e.when), default=7)
    mod_abs = latest + 1                                     # mod fires after the whole base scenario
    mod_when = _abs_to_day(mod_abs) + "T10:30"
    # post-mod starts after the mod, and for windowed families fully past the base rolling window
    # (dedup windows are MINUTES — the next day is already clear of the base)
    window = _parse_window_days(old_thr) if ct in ("rate_limit", "trigger") else 1
    base_abs = mod_abs + window + 1
    post_day = _abs_to_day(base_abs)
    other = _second_key(spec.seed, spec.key, spec.keys) or spec.key

    def run(thr, key_=None, day=None, offset=100, **kw):
        kw.setdefault("base_day", day or post_day)
        if ct == "counter" and "reset_day" not in kw:
            kw["reset_day"] = _abs_to_day(_ev_abs_day(kw["base_day"] + "T09:00") + 1)
        if ct == "cap" and "starting_total" not in kw:
            p = ((_seed_sales_reps(spec.seed) or [("",)])[0][0]
                 if spec.cap_scope == "per_person" else None)
            kw["starting_total"] = _base_cap_total(spec.base_events, person=p)
        kw["id_offset"] = offset
        return _run_builder(ct, spec.seed, thr, spec.phrasings, spec.decorations,
                            key_ or spec.key, spec.unit, entities=spec.entities, keys=spec.keys,
                            contacts=spec.key_contacts, cap_scope=spec.cap_scope,
                            person_caps=spec.person_caps, qty_noun=spec.qty_noun, **kw)

    noun = {"counter": "member", "cap": "rep"}.get(ct, "key")
    rule_noun = {"trigger": "quorum", "dedup": "deduplication"}.get(ct, "limit")
    events = []
    affected_key = None   # the entity the modification targets (exception/contextual)

    if mod_type == "exception":                       # one REAL seed entity becomes exempt
        if ct in ("rate_limit", "trigger", "dedup"):
            ex = _second_key(spec.seed, "", spec.keys) or spec.key
            affected_key = ex
            events = run(old_thr, key_=ex, exempt_key=ex)
        else:
            roster = spec.entities or [r[0] for r in (_seed_reps(spec.seed) or [])]
            ex = roster[0] if roster else "the first member"
            events = run(old_thr, exempt=ex)
        if ct == "trigger":
            intent = (f"Starting now, {ex} is exempt from the quorum ESCALATION rule ({old_thr}) — "
                      f"its occurrences are still recorded but never fire the {spec.unit or 'action'}; "
                      f"every other {noun} escalates as before.")
        elif ct == "dedup":
            intent = (f"Starting now, {ex} is exempt from deduplication ({old_thr}) — its repeats "
                      f"are all processed; every other {noun} is deduplicated as before.")
        else:
            intent = (f"Starting now, {ex} is exempt from the limit ({old_thr}); "
                      f"every other {noun} remains subject to it as before.")

    elif mod_type == "removal":                       # the rule is retired entirely
        events = run(old_thr, rule_off=True)
        intent = (f"Starting now, the {rule_noun} is RETIRED entirely: stop enforcing "
                  f"\"{old_thr}\". Requests are handled without it.")

    elif mod_type == "correction":                    # the value was wrong — fix it
        new_thr, flip_kw = _looser_threshold(ct, old_thr, old_limit)
        events = run(new_thr, key_=other if ct in ("rate_limit", "trigger") else None, **flip_kw)
        intent = (f"Correction: the rule should be \"{new_thr}\", not \"{old_thr}\" — the original "
                  f"value was set wrong. Apply the corrected rule from now on.")

    elif mod_type == "temporal":                      # new value only until an expiry
        new_thr, flip_kw = _looser_threshold(ct, old_thr, old_limit)
        run1 = run(new_thr, key_=other if ct in ("rate_limit", "trigger") else None, **flip_kw)
        r1_last = max((_ev_abs_day(e.when) for e in run1 if e.when), default=base_abs)
        expiry_label = _abs_to_day(r1_last)
        day2 = _abs_to_day(r1_last + (window if ct in ("rate_limit", "trigger") else 0) + 1)
        kw2 = {}
        if ct == "cap":   # the running total carries across the expiry
            kw2["starting_total"] = _base_cap_total(spec.base_events) + _base_cap_total(run1)
        run2 = run(old_thr, key_=spec.key if ct in ("rate_limit", "trigger") else None,
                   day=day2, offset=150, **kw2)
        if ct == "counter":   # the old-rule proof doesn't need the reset tail again (base showed it)
            roster_n = min(len(spec.entities or _seed_reps(spec.seed) or []), 3) or 1
            run2 = run2[:-roster_n]
        for e in run2:
            e.expect.reason = (f"The temporary rule has EXPIRED (it applied only until {expiry_label}) "
                               f"— the original \"{old_thr}\" applies again. " + (e.expect.reason or ""))
        events = run1 + run2
        intent = (f"From now until the end of {expiry_label}, \"{new_thr}\" applies (instead of "
                  f"\"{old_thr}\"); after {expiry_label} the original rule automatically returns.")

    elif mod_type == "contextual":                    # attribute override for one named key
        new_thr, flip_kw = _looser_threshold(ct, old_thr, old_limit)
        if ct == "counter":
            roster = spec.entities or [r[0] for r in (_seed_reps(spec.seed) or [])]
            ctx = roster[0] if roster else "the first member"
            run1 = run(new_thr, entities=[ctx], **{k: v for k, v in flip_kw.items()}) \
                if False else _run_builder(ct, spec.seed, new_thr, spec.phrasings, spec.decorations,
                                           spec.key, spec.unit, entities=[ctx], keys=spec.keys,
                                           base_day=post_day, reset_day=_abs_to_day(base_abs + 1),
                                           id_offset=100, **flip_kw)
            day2 = _abs_to_day(max((_ev_abs_day(e.when) for e in run1 if e.when), default=base_abs) + 1)
            run2 = _run_builder(ct, spec.seed, old_thr, spec.phrasings, spec.decorations,
                                spec.key, spec.unit, entities=spec.entities, keys=spec.keys,
                                base_day=day2, reset_day=_abs_to_day(_ev_abs_day(day2 + "T09:00") + 1),
                                id_offset=150)
            roster_n = min(len(spec.entities or _seed_reps(spec.seed) or []), 3) or 1
            run2 = run2[:-roster_n]   # old-rule proof doesn't need the reset tail (base showed it)
        else:
            ctx = other
            affected_key = ctx
            # SINGLE-KEY runs: the override applies only to ctx — the builders' second-key proof
            # would otherwise simulate a NON-override key under the override threshold (wrong
            # quorum/limit in its reasons)
            run1 = run(new_thr, key_=ctx, single_key=True, **flip_kw)
            day2 = _abs_to_day(max((_ev_abs_day(e.when) for e in run1 if e.when), default=base_abs)
                               + (window if ct in ("rate_limit", "trigger") else 0) + 1)
            run2 = run(old_thr, key_=spec.key, day=day2, offset=150, single_key=True)
        for e in run2:
            e.expect.reason = (f"{ctx}'s override does NOT apply here — the original \"{old_thr}\" "
                               f"still governs every other {noun}. " + (e.expect.reason or ""))
        events = run1 + run2
        intent = (f"Starting now, specifically for {ctx} the rule is \"{new_thr}\"; for every other "
                  f"{noun} the original \"{old_thr}\" still applies.")

    else:                                             # expansion: ADD behavior on the gated outcome
        target = _seed_person(spec.seed) or "the workflow owner (ops-alerts@company.example, " \
                                            "designated by this modification)"
        events = run(old_thr, key_=other if ct in ("rate_limit", "trigger") else None)
        gated_marker = {"counter": ("held",), "rate_limit": ("blocked",), "cap": ("held", "BUT"),
                        "trigger": ("is reached",), "dedup": ("duplicate",)}[ct]
        first = True
        for e in events:
            blob = f"{e.expect.action} {e.expect.reason or ''}" if e.expect else ""
            if any(m.lower() in blob.lower() for m in gated_marker):
                e.expect.action += (f" ADDITIONALLY, a notification about this outcome is sent to "
                                    f"{target} — NEW behavior added by the modification.")
                e.expect.reason = (e.expect.reason or "") + (
                    " THIS IS THE FLIP: before the modification this outcome happened WITHOUT the "
                    "extra notification; the notification is the observable change." if first else
                    " The extra notification is new behavior added by the modification.")
                first = False
        intent = (f"Starting now, ADD a notification step: whenever the {rule_noun} produces its "
                  f"gated outcome ({old_thr}), ALSO send a notification to {target}. "
                  f"Everything else stays unchanged.")

    for i, e in enumerate(events, 1):
        e.id = f"PM{i:03d}"
        e.role = "post_mod"
        e.after_mod_ids = ["M001"]

    # IRRELEVANT post-mod event: a request the workflow HANDLES NORMALLY whose outcome is IDENTICAL
    # before and after the modification (well within limits / first occurrence / fresh state) —
    # proves the modification is scoped. Built from the normal REQUEST phrasing, not a special one.
    tmpl = {p.role: p.template for p in spec.phrasings}
    req = tmpl.get("request") or tmpl.get("submit")
    if req:
        last_abs = max((_ev_abs_day(e.when) for e in events if e.when), default=_ev_abs_day(mod_when))
        irr_id = {"cap": "Q-9001", "counter": "LD-2026-9001"}.get(ct, "REQ-9001")
        # a key the workflow HANDLES that the modification does not target — for removal/
        # correction/temporal/expansion any tracked key works (fresh state, under every limit)
        irr_key = next((k for k in (spec.keys or []) if k != affected_key),
                       spec.key if spec.key != affected_key else (other or spec.key))
        # for analysis workflows NEVER fall back to a decoration — those carry counting terms
        deco = spec.irrelevant_deco or (
            f"with a routine, clearly neutral mention of {irr_key} (no qualifying terms)"
            if spec.analysis_field else (spec.decorations or [""])[0])
        irr_input = (req.replace("{ID}", irr_id).replace("{KEY}", irr_key)
                        .replace("{AMOUNT}", "500").replace("{DECO}", deco))
        irr_input = _re.sub(r"\{[A-Z_]+\}", "", irr_input).strip()
        if spec.analysis_field:
            ok_act = (f"{irr_id} is analyzed against the seeded {spec.analysis_field} rules — its "
                      f"text matches no '{spec.analysis_label or 'counting'}' term, so it is "
                      f"classified 'neutral', does NOT count toward the rule, and normal handling "
                      f"proceeds.")
        else:
            ok_act = {"counter": f"{irr_id} IS assigned to the next available member in rotation and posted normally.",
                  "cap": f"{irr_id} is recorded and routed for approval exactly as before.",
                  "trigger": f"{irr_id} is recorded for {irr_key} (occurrence #1 — far below the quorum); the normal per-event handling proceeds.",
                  "dedup": f"the report {irr_id} IS processed for {irr_key} (no recent duplicate).",
                  "rate_limit": f"the {spec.unit or 'request'} for {irr_key} ({irr_id}) is well within the limit and IS performed."}[ct]
        events.append(SpecEventWithExpect(
            id="IRR001", call_type="send_event", source="__external__", input=irr_input,
            when=_abs_to_day(last_abs + 2) + "T09:00", role="irrelevant", after_mod_ids=["M001"],
            expect=EventExpect(
                action=ok_act,
                reason=f"under BOTH the original rule and the modification this request is handled "
                       f"identically (fresh state, well within every limit), so the modification "
                       f"does not affect it — its outcome is exactly what it would have been before.")))
    return intent, mod_when, events


def publish_analysis_results(spec, post_events) -> None:
    """When the workflow ANALYZES content (sentiment/priority/...), the analysis is a MOCK-API
    capability — agents wrap domain functionality, they never perform the analysis themselves.
    Code publishes the per-event analysis results INTO THE SEED (served verbatim by the analysis
    service's `_data` tool), so the result of every scenario event's analysis is deterministic
    mock data, exactly like every other read service."""
    if not spec.analysis_field:
        return
    import json as _json
    try:
        d = _json.loads(spec.seed)
    except Exception:
        return
    if not isinstance(d, dict):
        return
    label = spec.analysis_label or "matching"
    # RULES-BASED: the analysis maps TEXT CONTENT to the result (a term in the text implies the
    # label), not event ids — the analysis service applies these rules to whatever text it is given
    d["analysis_rules"] = {spec.analysis_field: {
        "value_when_text_contains": list(spec.analysis_terms) or [label],
        "otherwise": "neutral",
    }}
    d.pop("analysis_results", None)
    spec.seed = _json.dumps(d)


def _validate_gen(r: GeneratedScenarioSpec) -> bool:
    """Infuse-response validation with SPECIFIC failure messages (fed back on retry)."""
    if not r.threshold:
        raise ValueError("threshold is empty")
    if not _valid_seed(r.seed):
        raise ValueError("seed is not a non-empty JSON object")
    if not r.phrasings:
        raise ValueError("phrasings are missing")
    classifies = _re.search(r"\b(negative|positive|sentiment|spam|toxic)\b",
                            f"{r.threshold} {r.description}", _re.I)
    if classifies and not (r.analysis_field and r.analysis_terms and r.irrelevant_deco.strip()):
        raise ValueError(
            "this rule CLASSIFIES content, so analysis_field, analysis_terms (3-6 terms) and a "
            "non-empty term-free irrelevant_deco are REQUIRED")
    if r.analysis_terms:
        for d in (r.decorations or []):
            if not any(t.lower() in d.lower() for t in r.analysis_terms):
                raise ValueError(
                    f"decoration {d!r} contains NO analysis term — every decoration must embed "
                    f"one of: {', '.join(r.analysis_terms)}")
        if not r.irrelevant_deco.strip():
            raise ValueError("irrelevant_deco is required (term-free content the analysis filters out)")
        for t in r.analysis_terms:
            if t.lower() in r.irrelevant_deco.lower():
                raise ValueError(f"irrelevant_deco contains the analysis term {t!r} — it must match NONE")
            for k in (r.keys or []):
                if t.lower() in k.lower():
                    raise ValueError(
                        f"key {k!r} embeds the analysis term {t!r} — keys are entity names; the "
                        f"term belongs in event text, not the key")
    if not _build_scenario(r):
        raise ValueError("the scenario builder produced no events from this response "
                         "(check seed shape: counter needs entities/roster, key families need keys)")
    return True


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
        # Validator raises SPECIFIC messages — generate_with_retries feeds them back into the
        # retry prompt, so the model can self-correct (a bare "Validation failed" cannot converge).
        validator=_validate_gen,
    )
    if gen is None:
        return spec, False

    gen.seed = _trim_seed(gen.seed)            # cap entities (reps/approvers/skus) → small scenarios
    spec.seed = gen.seed
    spec.phrasings = gen.phrasings
    spec.decorations = gen.decorations
    spec.key = gen.key or (gen.keys[0] if gen.keys else "")
    spec.unit = gen.unit
    spec.entities = gen.entities
    spec.keys = gen.keys
    spec.irrelevant_key = gen.irrelevant_key
    spec.key_contacts = gen.key_contacts
    spec.analysis_field = gen.analysis_field
    spec.analysis_label = gen.analysis_label
    spec.analysis_terms = gen.analysis_terms
    spec.irrelevant_deco = gen.irrelevant_deco
    spec.branch_demos = gen.branch_demos
    spec.cap_scope = gen.cap_scope
    spec.person_caps = gen.person_caps
    spec.qty_noun = gen.qty_noun
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
    issues = coherence_issues(texts, ct) + concurrent_pair_issues(evd, gen.threshold or "", ct)
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
