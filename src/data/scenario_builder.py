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
from src.data.generate_state_constraints import _parse_limit, _seed_reps, simulate_rotation


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
                           base_day: str = "W01-1", reset_day: str = "W01-2", id_offset: int = 0) -> list:
    """Round-robin / per-key daily counter. Builds, BY CONSTRUCTION:
      - (cap*R - 1) leads in round-robin order (all assigned) → leaves exactly ONE slot open,
      - a concurrent pair at that last slot (same `when`) → one assigned, one held (the race),
      - a next-day reset (assigned again, daily counter cleared).
    `phrase(lead_id, decoration)` returns the NL input text. Returns [] if the seed has no roster."""
    reps = _seed_reps(seed)
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
                                    "concurrent_group": e.concurrent_group} for e in events], threshold)
    for e, s in zip(events, sim):
        e.expect = EventExpect(action=s["action"], reason=s["reason"])
    return events


def _second_sku(seed: str, exclude: str):
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
                              base_day: str = "W01-1", id_offset: int = 0) -> list:
    """Per-key rolling-window rate limit (N per key per D days). Builds BY CONSTRUCTION for the
    main key: (N-1) accepted inside the window, a concurrent pair at the last slot (one accepted,
    one blocked), a post-window reset. It ALSO fires one request for a SECOND key while the main
    key is at its limit — allowed, proving the limit is PER-KEY, not global. Simulated per-key.
    `phrase(req_id, is_blocked, key)` returns the input text (never states the outcome)."""
    N = _parse_limit(threshold)
    D = _parse_window_days(threshold)
    week, day = int(base_day[1:3]), int(base_day[4:])
    key2 = _second_sku(seed, key)
    events: list = []   # list of (event, key)

    def add(text, when, k, cg=None):
        events.append((_ev(len(events) + 1, text, when, cg), k))

    rid = lambda i: f"REQ-{i + id_offset:04d}"
    n = 0
    for i in range(N - 1):                          # (N-1) accepted for the main key, in-window
        n += 1; add(phrase(rid(n), False, key), f"W{week:02d}-{day + i * 2}T09:00", key)
    pair_day = day + (N - 1) * 2                     # concurrent pair at the last in-window slot
    n += 1; add(phrase(rid(n), False, key), f"W{week:02d}-{pair_day}T11:00", key, "cg_limit")
    n += 1; add(phrase(rid(n), True, key), f"W{week:02d}-{pair_day}T11:00", key, "cg_limit")
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
            note = (f" More than {D} days have passed since the earlier reorders for {k}, which have "
                    f"aged out of the rolling {D}-day window." if aged else "")
            e.expect = EventExpect(
                action=f"{_ev_ref(e)} for {k} is at/below its reorder level and a reorder request IS sent to the supplier.",
                reason=f"only {in_window} reorder(s) for {k} in the last {D} days (< {N}); the limit is "
                       f"PER SKU, so {k} is unaffected by other SKUs.{note}")
        else:
            e.expect = EventExpect(
                action=f"NO new reorder is sent for {_ev_ref(e)} ({k}); only the routine inventory reply goes out.",
                reason=f"{in_window} reorders were already sent for {k} within the last {D} days (the "
                       f"limit of {N}), so a new reorder is blocked until that SKU's window clears.")
        out.append(e)
    return out


def build_cap_scenario(seed: str, threshold: str, submit_phrase, approve_phrase,
                       approvers: list, base_day: str = "W01-1", starting_total: int = 0, id_offset: int = 0) -> list:
    """Cumulative cap with an APPROVER (submit + approve per quote). Builds amounts BY
    CONSTRUCTION so the running approved total approaches the cap from `starting_total`, a
    concurrent pair of approvals sits at the boundary (one fits the remaining budget, one
    doesn't), and a clear over-cap one is blocked. `starting_total` carries a prior cumulative
    (the modification scenario continues the base's running total). Each quote = a HubSpot
    submission (always handled) + the approver's Slack decision (gated)."""
    cap = _parse_limit(threshold)
    apr = approvers or ["the approver"]
    budget = max(4, cap - starting_total)  # headroom remaining under the cap
    # amounts: two well under, then a boundary pair, then an over-cap one
    a = max(1, budget // 4)
    amounts = [a, a]
    remaining = budget - sum(amounts)
    pair_amt = max(1, remaining - max(1, budget // 10))  # fits once, not twice
    amounts += [pair_amt, pair_amt]        # boundary pair
    week, day = int(base_day[1:3]), int(base_day[4:])

    events: list[SpecEventWithExpect] = []
    quotes = []                            # (qid, amount, is_pair)
    t = 0
    for i, amt in enumerate(amounts):
        qid = f"Q-{1001 + id_offset + i}"
        quotes.append((qid, amt, i >= 2))  # last two are the pair
        events.append(_ev(len(events) + 1, submit_phrase(qid, amt), f"W{week:02d}-{day}T{9 + t:02d}:00"))
        t += 1

    # approvals: non-pair approved in order; the two pair approvals share one `when` (the race)
    total = starting_total                 # continue a prior cumulative (mod scenario carries the base total)
    approve_events = []
    for j, (qid, amt, is_pair) in enumerate(quotes):
        apr_name = apr[j % len(apr)]
        when = f"W{week:02d}-{day}T13:00" if is_pair else f"W{week:02d}-{day}T{10 + j:02d}:30"
        cg = "cg_limit" if is_pair else None
        e = _ev(len(events) + len(approve_events) + 1, approve_phrase(qid, apr_name), when, cg)
        approve_events.append((e, qid, amt, apr_name))

    # simulate the cap over approvals in (when, id) order
    ordered = sorted(approve_events, key=lambda x: (x[0].when, x[0].id))
    for e, qid, amt, apr_name in ordered:
        if total + amt <= cap:
            total += amt
            e.expect = EventExpect(
                action=f"{apr_name} approves {qid} (${amt:,}); it is approved in HubSpot and the "
                       f"approval is posted in Slack.",
                reason=f"approving ${amt:,} keeps the quarter's approved discount at ${total:,}, "
                       f"within the ${cap:,} cap.")
        else:
            e.expect = EventExpect(
                action=f"{apr_name} approves {qid}, BUT the system holds it for exception handling and "
                       f"no approval is recorded — the approver acts, but the approval cannot take effect "
                       f"because it would breach the cap.",
                reason=f"approving ${amt:,} would push the quarter's approved discount from "
                       f"${total:,} to ${total + amt:,}, over the ${cap:,} cap, so the system intercepts "
                       f"the approval and routes it to exception handling.")
    # submissions are always handled
    for e in events:
        qid = re.search(r"Q-\d+", e.input)
        e.expect = EventExpect(action=f"The quote {qid.group() if qid else ''} is recorded in HubSpot "
                                      f"and an approval request is sent to the approvers.",
                               reason="a submission is always accepted; it does not approve anything.")
    return events + [e for e, *_ in approve_events]


def _ev_ref(e) -> str:
    m = re.search(r"\bREQ-\d+\b", e.input or "")
    return m.group(0) if m else "the request"
