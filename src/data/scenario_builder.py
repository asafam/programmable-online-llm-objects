"""
Code-generated scenario structure — logically valid BY CONSTRUCTION.

The LLM supplies only realism (the structured seed + phrasing + decorations). This module builds
the request sequence — counts, timestamps, the concurrent pair placed at the LAST remaining slot,
the period reset — and derives every expect by simulation. One builder per invariant family, so
the LLM never touches the arithmetic that it gets wrong ~100% of the time.
"""
from __future__ import annotations

from src.data.schema import EventExpect, SpecEventWithExpect
from src.data.generate_state_constraints import _parse_limit, _seed_reps, simulate_rotation


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
