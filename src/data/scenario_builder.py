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
                           base_day: str = "W01-1", reset_day: str = "W01-2") -> list:
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
    lead = lambda k: f"LD-2026-{k:04d}"

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


def build_rate_limit_scenario(seed: str, threshold: str, key: str, phrase,
                              base_day: str = "W01-1") -> list:
    """Per-key rolling-window rate limit (N per key per D days). Builds BY CONSTRUCTION for one
    key: (N-1) accepted requests inside the window, a concurrent pair at the last slot (one
    accepted, one blocked), and a post-window request (>D days later) that is allowed again.
    `phrase(req_id, is_blocked, key)` returns the input text (the input never states the outcome)."""
    N = _parse_limit(threshold)
    D = _parse_window_days(threshold)
    week, day = int(base_day[1:3]), int(base_day[4:])
    events, descr = [], []  # descr: parallel {when, concurrent_group} with derived outcome

    def add(text, when, cg=None):
        events.append(_ev(len(events) + 1, text, when, cg))

    rid = lambda k: f"REQ-{k:04d}"
    n = 0
    # (N-1) accepted, spaced 2 days apart, all inside the window
    for i in range(N - 1):
        n += 1
        add(phrase(rid(n), False, key), f"W{week:02d}-{day + i * 2}T09:00")
    # concurrent pair at the last in-window slot
    pair_day = day + (N - 1) * 2
    n += 1; add(phrase(rid(n), False, key), f"W{week:02d}-{pair_day}T11:00", "cg_limit")
    n += 1; add(phrase(rid(n), True, key), f"W{week:02d}-{pair_day}T11:00", "cg_limit")
    # post-window reset: > D days after the LAST in-window request (the pair), so all aged out
    reset_abs = (week - 1) * 7 + pair_day + D + 1
    n += 1; add(phrase(rid(n), False, key), f"W{(reset_abs - 1) // 7 + 1:02d}-{(reset_abs - 1) % 7 + 1}T09:00")

    # simulate the sliding window (single key) to derive every expect
    accepted_days: list[int] = []
    for e in events:
        d = _abs_day(e.when)
        in_window = sum(1 for ad in accepted_days if d - ad < D)
        if in_window < N:
            aged = any(d - ad >= D for ad in accepted_days)
            accepted_days.append(d)
            reset_note = (f" More than {D} days have passed since the earlier reorders, which have aged "
                          f"out of the rolling {D}-day window, so the limit no longer applies." if aged else "")
            e.expect = EventExpect(
                action=f"{_ev_ref(e)} for {key} is at/below its reorder level and a reorder request "
                       f"IS sent to the supplier.",
                reason=f"only {in_window} reorder(s) for {key} in the last {D} days (< {N}), so the "
                       f"reorder is allowed.{reset_note}")
        else:
            e.expect = EventExpect(
                action=f"NO new reorder is sent for {_ev_ref(e)} ({key}); only the routine inventory "
                       f"reply goes out.",
                reason=f"{in_window} reorders were already sent for {key} within the last {D} days "
                       f"(the limit of {N}), so a new reorder is blocked until the window clears.")
    return events


def build_cap_scenario(seed: str, threshold: str, submit_phrase, approve_phrase,
                       approvers: list, base_day: str = "W01-1") -> list:
    """Cumulative cap with an APPROVER (submit + approve per quote). Builds amounts BY
    CONSTRUCTION so the running approved total approaches the cap, a concurrent pair of approvals
    sits at the boundary (one fits the remaining budget, one doesn't), and a clear over-cap one is
    blocked. Each quote = a HubSpot submission (always handled) + the approver's Slack decision
    (gated). submit_phrase(qid, amount) / approve_phrase(qid, approver) return input text."""
    cap = _parse_limit(threshold)
    apr = approvers or ["the approver"]
    # amounts: two well under, then a boundary pair, then an over-cap one
    a = cap // 4
    amounts = [a, a]                       # 2*(cap/4) = cap/2 approved
    remaining = cap - sum(amounts)         # = cap/2 left
    pair_amt = remaining - max(1, cap // 10)  # fits once, not twice (amt <= remaining < 2*amt)
    amounts += [pair_amt, pair_amt]        # boundary pair
    week, day = int(base_day[1:3]), int(base_day[4:])

    events: list[SpecEventWithExpect] = []
    quotes = []                            # (qid, amount, is_pair)
    t = 0
    for i, amt in enumerate(amounts):
        qid = f"Q-{1001 + i}"
        quotes.append((qid, amt, i >= 2))  # last two are the pair
        events.append(_ev(len(events) + 1, submit_phrase(qid, amt), f"W{week:02d}-{day}T{9 + t:02d}:00"))
        t += 1

    # approvals: non-pair approved in order; the two pair approvals share one `when` (the race)
    total = 0
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
                action=f"{apr_name} does NOT approve {qid}; it is held for exception handling and no "
                       f"approval is recorded.",
                reason=f"approving ${amt:,} would push the quarter's approved discount from "
                       f"${total:,} to ${total + amt:,}, over the ${cap:,} cap.")
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
