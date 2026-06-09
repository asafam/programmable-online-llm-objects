"""
Code-generated scenario structure — logically valid BY CONSTRUCTION.

The LLM supplies only realism (the structured seed + phrasing + decorations). This module builds
the request sequence — counts, timestamps, the concurrent pair placed at the LAST remaining slot,
the period reset — and derives every expect by simulation. One builder per invariant family, so
the LLM never touches the arithmetic that it gets wrong ~100% of the time.
"""
from __future__ import annotations

import re

from src.data.schema import EventExpect, SpecEventWithExpect
from src.data.generate_state_constraints import _fill_outcome, _parse_limit, _seed_reps, simulate_rotation


def _abs_day(when: str) -> int:
    """W<week>-<day>T.. → absolute day number, so window arithmetic is comparable."""
    m = re.match(r"W(\d+)-(\d+)", when or "")
    return (int(m.group(1)) - 1) * 7 + int(m.group(2)) if m else 0


def _parse_window_days(threshold: str, default: int = 7) -> int:
    m = re.search(r"(\d+)\s*(?:day|d\b)", threshold or "", re.I)
    return int(m.group(1)) if m else default


def _when(day: str, idx: int) -> str:
    total = 9 * 60 + idx * 5          # 09:00, 09:05, … 5 minutes apart
    return f"{day}T{total // 60:02d}:{total % 60:02d}"


def _ev(n: int, text: str, when: str, cg: str | None = None) -> SpecEventWithExpect:
    return SpecEventWithExpect(id=f"E{n:03d}", call_type="send_event", source="__external__",
                              input=text, when=when, role="base", concurrent_group=cg)


def build_counter_scenario(seed: str, threshold: str, phrase, decorations: list,
                           base_day: str = "W01-1", reset_day: str = "W01-2", id_offset: int = 0,
                           outcomes: dict | None = None, unit: str = "assignment",
                           flip_old_limit: int | None = None, entities: list | None = None) -> list:
    """Round-robin / per-key daily counter — DOMAIN-GENERIC (reps/channels/agents). Builds,
    BY CONSTRUCTION:
      - (cap*R - 1) requests in round-robin order (all assigned) → leaves exactly ONE slot open,
      - a concurrent pair at that last slot (same `when`) → one assigned, one held (the race),
      - a next-day reset (assigned again, daily counter cleared).
    `entities` (preferred) names the rotation members in order; else parsed generically from the
    seed. `phrase(req_id, decoration)` returns the NL input text. Returns [] if no rotation found."""
    reps = _seed_reps(seed, entities)
    if not reps:
        return []
    cap = _parse_limit(threshold)
    nfill = cap * len(reps) - 1        # round-robin fill that leaves exactly one open slot
    deco = lambda k: decorations[(k - 1) % len(decorations)] if decorations else {}
    lead = lambda k: f"LD-2026-{k + id_offset:04d}"

    events: list[SpecEventWithExpect] = []
    idx = 0
    for k in range(1, nfill + 1):
        events.append(_ev(len(events) + 1, phrase(lead(k), deco(k)), _when(base_day, idx)))
        idx += 1
    pw = _when(base_day, idx)          # the two simultaneous arrivals compete for the last slot
    events.append(_ev(len(events) + 1, phrase(lead(nfill + 1), deco(nfill + 1)), pw, "cg_limit"))
    events.append(_ev(len(events) + 1, phrase(lead(nfill + 2), deco(nfill + 2)), pw, "cg_limit"))
    for j, k in enumerate(range(nfill + 3, nfill + 3 + min(len(reps), 3))):
        events.append(_ev(len(events) + 1, phrase(lead(k), deco(k)), _when(reset_day, j)))

    # Derive every expect deterministically by simulating the rotation over the built sequence.
    sim = simulate_rotation(seed, [{"id": e.id, "input": e.input, "when": e.when,
                                    "concurrent_group": e.concurrent_group} for e in events], threshold,
                            outcomes=outcomes, unit=unit, flip_old_limit=flip_old_limit, entities=entities)
    for e, s in zip(events, sim):
        e.expect = EventExpect(action=s["action"], reason=s["reason"])
    return events


def _second_key(seed: str, exclude: str, keys: list | None = None):
    """A DIFFERENT limit-tracked key — DOMAIN-GENERIC. Prefer the explicit `keys` list (the LLM
    names the key values); else scan the seed for any list whose dicts carry a 'sku' field (legacy)."""
    for k in (keys or []):
        if k and k != exclude:
            return k
    import json
    try:
        d = json.loads(seed)
    except Exception:
        return None
    skus = (d.get("catalog") or {}).get("skus") if isinstance(d.get("catalog"), dict) else d.get("skus")
    for s in (skus or []):
        c = s.get("sku") if isinstance(s, dict) else s
        if c and c != exclude:
            return c
    return None


def build_rate_limit_scenario(seed: str, threshold: str, key: str, phrase,
                              base_day: str = "W01-1", id_offset: int = 0,
                              outcomes: dict | None = None, unit: str = "reorder",
                              flip_old_limit: int | None = None, keys: list | None = None) -> list:
    """Per-key rolling-window rate limit (N per key per D days) — DOMAIN-GENERIC (SKUs/categories/
    contacts). Builds BY CONSTRUCTION for the main key: (N-1) accepted inside the window, a
    concurrent pair at the last slot (one accepted, one blocked), a post-window reset. It ALSO
    fires one request for a SECOND key while the main key is at its limit — allowed, proving the
    limit is PER-KEY, not global. `keys` (preferred) lists the limit-tracked key values.
    `phrase(req_id, is_blocked, key)` returns the input text (never states the outcome)."""
    N = _parse_limit(threshold)
    D = _parse_window_days(threshold)
    week, day = int(base_day[1:3]), int(base_day[4:])
    key2 = _second_key(seed, key, keys)
    events: list = []   # list of (event, key)

    def add(text, when, k, cg=None):
        events.append((_ev(len(events) + 1, text, when, cg), k))

    rid = lambda i: f"REQ-{i + id_offset:04d}"
    n = 0
    # spacing must keep all (N-1) accepted + the pair INSIDE one rolling window: spread by 2 days
    # when the window is wide enough, else same-day hourly steps (e.g. a 1-day window).
    gap = 2 if (N - 1) * 2 < D else 0
    for i in range(N - 1):                          # (N-1) accepted for the main key, in-window
        n += 1
        add(phrase(rid(n), False, key), f"W{week:02d}-{day + i * gap}T{9 + (0 if gap else i):02d}:00", key)
    pair_day = day + (N - 1) * gap                   # concurrent pair at the last in-window slot
    pair_t = f"T{11 + (0 if gap else N - 1):02d}:30"
    n += 1; add(phrase(rid(n), False, key), f"W{week:02d}-{pair_day}{pair_t}", key, "cg_limit")
    n += 1; add(phrase(rid(n), True, key), f"W{week:02d}-{pair_day}{pair_t}", key, "cg_limit")
    if key2:                                         # a SECOND key, same window → ALLOWED (per-key)
        n += 1; add(phrase(rid(n), False, key2), f"W{week:02d}-{pair_day}T13:00", key2)
    reset_abs = (week - 1) * 7 + pair_day + D + 1    # main-key post-window reset
    n += 1; add(phrase(rid(n), False, key), f"W{(reset_abs - 1) // 7 + 1:02d}-{(reset_abs - 1) % 7 + 1}T09:00", key)

    # simulate the sliding window PER KEY
    accepted: dict[str, list[int]] = {}
    out = []
    for e, k in events:
        d = _abs_day(e.when)
        in_window = sum(1 for ad in accepted.get(k, []) if d - ad < D)
        if in_window < N:
            aged = any(d - ad >= D for ad in accepted.get(k, []))
            accepted.setdefault(k, []).append(d)
            note = (f" More than {D} days have passed since the earlier {unit}s for {k}, which have "
                    f"aged out of the rolling {D}-day window." if aged else "")
            flip = (f" THIS IS THE FLIP: the original limit of {flip_old_limit} would have BLOCKED "
                    f"this {unit} (#{in_window + 1} for {k} in the window), but the modification "
                    f"(limit {N}) ALLOWS it." if flip_old_limit and in_window >= flip_old_limit else "")
            e.expect = EventExpect(
                action=_fill_outcome(outcomes, "allowed",
                    f"the {unit} for {k} is within the limit and IS performed ({_ev_ref(e)}).",
                    ID=_ev_ref(e), KEY=k),
                reason=f"only {in_window} {unit}(s) for {k} in the last {D} days (< {N}); the limit is "
                       f"PER key, so {k} is unaffected by other keys.{note}{flip}")
        else:
            e.expect = EventExpect(
                action=_fill_outcome(outcomes, "blocked",
                    f"the {unit} for {k} ({_ev_ref(e)}) is NOT performed — it is blocked by the rolling-window limit.",
                    ID=_ev_ref(e), KEY=k),
                reason=f"{in_window} {unit}(s) were already done for {k} within the last {D} days (the "
                       f"limit of {N}), so a new one is blocked until that key's window clears.")
        out.append(e)
    return out


def build_cap_scenario(seed: str, threshold: str, submit_phrase, approve_phrase,
                       submitters: list, base_day: str = "W01-1", starting_total: int = 0, id_offset: int = 0,
                       outcomes: dict | None = None, unit: str = "approval",
                       flip_old_limit: int | None = None) -> list:
    """Cumulative cap with an approver chain. `submitters` is [(rep, manager)]. Each quote: the
    REP submits (the event is just the stimulus — it does NOT choose the approver); the submission
    EXPECT verifies the SYSTEM (quote-approval-policy) routed the request to the rep's MANAGER via
    email; then that manager's decision is the gated action (cap). Amounts are built so the running
    total approaches the cap from `starting_total` with a boundary pair, and an over-cap one held."""
    cap = _parse_limit(threshold)
    subm = submitters or [("an account executive", "the approver")]
    budget = max(4, cap - starting_total)
    a = max(1, budget // 4)
    amounts = [a, a]
    remaining = budget - sum(amounts)
    pair_amt = max(1, remaining - max(1, budget // 10))
    amounts += [pair_amt, pair_amt]
    week, day = int(base_day[1:3]), int(base_day[4:])

    events: list[SpecEventWithExpect] = []
    quotes = []                            # (qid, amount, is_pair, rep, manager)
    for i, amt in enumerate(amounts):
        qid = f"Q-{1001 + id_offset + i}"
        rep, mgr = subm[i % len(subm)]
        quotes.append((qid, amt, i >= 2, rep, mgr))
        e = _ev(len(events) + 1, submit_phrase(qid, amt, rep), f"W{week:02d}-{day}T{9 + i:02d}:00")
        # The submission expect VERIFIES the system routed the approval to the rep's MANAGER.
        e.expect = EventExpect(
            action=_fill_outcome(outcomes, "submitted",
                f"{qid} is recorded; the system routes the approval request to {rep}'s manager, {mgr}.",
                ID=qid, SUBMITTER=rep, MANAGER=mgr),
            reason=f"the system's routing policy sends each request to the submitter's manager ({mgr} "
                   f"for {rep}); the submission itself is always recorded and is not the gated action.")
        events.append(e)

    # approvals: the routed MANAGER decides; cap-gated. pair share one `when` (the race).
    total = starting_total
    approve_events = []
    for j, (qid, amt, is_pair, rep, mgr) in enumerate(quotes):
        when = f"W{week:02d}-{day}T13:00" if is_pair else f"W{week:02d}-{day}T{10 + j:02d}:30"
        cg = "cg_limit" if is_pair else None
        e = _ev(len(events) + len(approve_events) + 1, approve_phrase(qid, mgr), when, cg)
        approve_events.append((e, qid, amt, mgr))

    ordered = sorted(approve_events, key=lambda x: (x[0].when, x[0].id))
    for e, qid, amt, mgr in ordered:
        if total + amt <= cap:
            total += amt
            flip = (f" THIS IS THE FLIP: this approval brings the running total to ${total:,}, above the "
                    f"original ${flip_old_limit:,} cap which would have HELD it, but the modification "
                    f"(${cap:,}) ALLOWS it." if flip_old_limit and total > flip_old_limit else "")
            e.expect = EventExpect(
                action=_fill_outcome(outcomes, "approved",
                    f"{mgr} approves {qid} (${amt:,}); the {unit} is recorded.",
                    ID=qid, ENTITY=mgr, AMOUNT=f"{amt:,}"),
                reason=f"the {unit} of ${amt:,} keeps the running approved total at ${total:,}, within the ${cap:,} cap.{flip}")
        else:
            e.expect = EventExpect(
                action=_fill_outcome(outcomes, "held",
                    f"{mgr} acts on {qid}, BUT the system holds it for exception handling — the {unit} cannot "
                    f"take effect because it would breach the cap, so none is recorded.",
                    ID=qid, ENTITY=mgr, AMOUNT=f"{amt:,}"),
                reason=f"the {unit} of ${amt:,} would push the running total from ${total:,} to ${total + amt:,}, "
                       f"over the ${cap:,} cap, so the system intercepts it and routes it to exception handling.")
    return events + [e for e, *_ in approve_events]


def _ev_ref(e) -> str:
    m = re.search(r"\bREQ-\d+\b", e.input or "")
    return m.group(0) if m else "the request"
