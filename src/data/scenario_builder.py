"""
Code-generated scenario structure — logically valid BY CONSTRUCTION.

The LLM supplies only realism (the structured seed + phrasing + decorations). This module builds
the request sequence — counts, timestamps, the boundary pair placed at the LAST remaining slot,
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
    # allow one qualifier word between the number and "day(s)": "1 business day",
    # "30 calendar days", "7-day", "7 days"
    m = re.search(r"(\d+)[-\s]*(?:[a-z]+[-\s]+)?days?\b", threshold or "", re.I)
    return int(m.group(1)) if m else default


def _parse_window_minutes(threshold: str, default: int = 10) -> int:
    """MINUTE-granular window ('10 minutes', '2 hours', '1 day') — dedup windows are short."""
    m = re.search(r"(\d+)[-\s]*(?:[a-z]+[-\s]+)?(minutes?|mins?|hours?|hrs?|days?)\b",
                  threshold or "", re.I)
    if not m:
        return default
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * (1 if unit.startswith("min") else 60 if unit.startswith(("hour", "hr")) else 1440)


def _abs_minutes(when: str) -> int:
    m = re.match(r"W(\d+)-(\d+)T(\d+):(\d+)", when or "")
    if not m:
        return _abs_day(when) * 1440
    return (((int(m.group(1)) - 1) * 7 + int(m.group(2))) * 1440
            + int(m.group(3)) * 60 + int(m.group(4)))


def _minutes_to_when(week: int, day: int, minutes_after_9: int) -> str:
    total = 9 * 60 + minutes_after_9
    day += total // 1440
    total %= 1440
    week += (day - 1) // 7
    day = (day - 1) % 7 + 1
    return f"W{week:02d}-{day}T{total // 60:02d}:{total % 60:02d}"


def _when(day: str, idx: int) -> str:
    total = 9 * 60 + idx * 5          # 09:00, 09:05, … 5 minutes apart
    return f"{day}T{total // 60:02d}:{total % 60:02d}"


def _ev(n: int, text: str, when: str, cg: str | None = None) -> SpecEventWithExpect:
    return SpecEventWithExpect(id=f"E{n:03d}", call_type="send_event", source="__external__",
                              input=text, when=when, role="base", concurrent_group=cg)


def build_counter_scenario(seed: str, threshold: str, phrase, decorations: list,
                           base_day: str = "W01-1", reset_day: str = "W01-2", id_offset: int = 0,
                           outcomes: dict | None = None, unit: str = "assignment",
                           flip_old_limit: int | None = None, entities: list | None = None,
                           exempt: str | None = None, rule_off: bool = False) -> list:
    """Round-robin / per-key daily counter — DOMAIN-GENERIC (reps/channels/agents). Builds,
    BY CONSTRUCTION:
      - (cap*R - 1) requests in round-robin order (all assigned) → leaves exactly ONE slot open,
      - a boundary pair at that last slot (sequential, minutes apart) → first assigned, next held,
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
    # boundary pair, one AFTER the other (minutes apart) — deterministic order, no race:
    # the first takes the last open slot, the next one finds every slot taken
    events.append(_ev(len(events) + 1, phrase(lead(nfill + 1), deco(nfill + 1)), _when(base_day, idx)))
    events.append(_ev(len(events) + 1, phrase(lead(nfill + 2), deco(nfill + 2)), _when(base_day, idx + 1)))
    for j, k in enumerate(range(nfill + 3, nfill + 3 + min(len(reps), 3))):
        events.append(_ev(len(events) + 1, phrase(lead(k), deco(k)), _when(reset_day, j)))

    # Derive every expect deterministically by simulating the rotation over the built sequence.
    sim = simulate_rotation(seed, [{"id": e.id, "input": e.input, "when": e.when,
                                    "concurrent_group": e.concurrent_group} for e in events], threshold,
                            outcomes=outcomes, unit=unit, flip_old_limit=flip_old_limit, entities=entities,
                            exempt=exempt, rule_off=rule_off)
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
                              flip_old_limit: int | None = None, keys: list | None = None,
                              flip_old_window: int | None = None,
                              exempt_key: str | None = None, rule_off: bool = False,
                              single_key: bool = False, contacts: dict | None = None) -> list:
    """Per-key rolling-window rate limit (N per key per D days) — DOMAIN-GENERIC (SKUs/categories/
    contacts). Builds BY CONSTRUCTION for the main key: (N-1) accepted inside the window, a
    sequential boundary pair at the last slot (first accepted, next blocked), a post-window reset. It ALSO
    fires one request for a SECOND key while the main key is at its limit — allowed, proving the
    limit is PER-KEY, not global. `keys` (preferred) lists the limit-tracked key values.
    Mod-dimension flips: `flip_old_window` marks events still in-window under the OLD window length
    (blocked-then, allowed-now); `exempt_key` ignores the limit for that key (its beyond-limit
    events are the FLIP). `phrase(req_id, is_blocked, key)` returns the input text."""
    N = _parse_limit(threshold)
    D = _parse_window_days(threshold)
    DAYS = f"{D} day" + ("s" if D != 1 else "")
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
    pair_day = day + (N - 1) * gap         # boundary pair: sequential, minutes apart (no race) —
    hh = 11 + (0 if gap else N - 1)        # the first fills the last in-window slot, the next is blocked
    n += 1; add(phrase(rid(n), False, key), f"W{week:02d}-{pair_day}T{hh:02d}:30", key)
    n += 1; add(phrase(rid(n), True, key), f"W{week:02d}-{pair_day}T{hh:02d}:35", key)
    if single_key:
        key2 = None
    if key2 and exempt_key:
        # SCOPE PROOF for an exemption: the OTHER key runs to its limit and is still blocked —
        # the exemption changes nothing outside its key
        for j in range(N + 1):
            n += 1
            add(phrase(rid(n), j >= N, key2), f"W{week:02d}-{pair_day}T{13 + j // 12:02d}:{(j * 5) % 60:02d}", key2)
    elif key2:                                       # a SECOND key, same window → ALLOWED (per-key)
        n += 1; add(phrase(rid(n), False, key2), f"W{week:02d}-{pair_day}T13:00", key2)
    reset_abs = (week - 1) * 7 + pair_day + D + 1    # main-key post-window reset
    n += 1; add(phrase(rid(n), False, key), f"W{(reset_abs - 1) // 7 + 1:02d}-{(reset_abs - 1) % 7 + 1}T09:00", key)

    # simulate the sliding window PER KEY
    accepted: dict[str, list[int]] = {}
    out = []
    for e, k in events:
        d = _abs_day(e.when)
        is_exempt = (exempt_key is not None and k == exempt_key) or rule_off
        in_window = sum(1 for ad in accepted.get(k, []) if d - ad < D)
        in_old_window = (sum(1 for ad in accepted.get(k, []) if d - ad < flip_old_window)
                         if flip_old_window else 0)
        if in_window < N or is_exempt:
            aged = any(d - ad >= D for ad in accepted.get(k, []))
            accepted.setdefault(k, []).append(d)
            note = (f" More than {DAYS} have passed since the earlier {unit}s for {k}, which have "
                    f"aged out of the rolling {D}-day window." if aged else "")
            flip = (f" THIS IS THE FLIP: the original limit of {flip_old_limit} would have BLOCKED "
                    f"this {unit} (#{in_window + 1} for {k} in the window), but the modification "
                    f"(limit {N}) ALLOWS it." if flip_old_limit and in_window >= flip_old_limit else "")
            if flip_old_window and in_old_window >= N:
                flip = (f" THIS IS THE FLIP: under the original {flip_old_window}-day window, "
                        f"{in_old_window} earlier {unit}(s) for {k} would still be in-window (>= {N}) "
                        f"and this would have been BLOCKED; the modification ({D}-day window) lets "
                        f"them age out and ALLOWS it.")
            if is_exempt and in_window >= N:
                why = (f"the rolling-window limit was RETIRED by the modification, so the {unit} "
                       f"proceeds (#{in_window + 1} in the window). THIS IS THE FLIP: under the "
                       f"retired limit of {N} per {DAYS} this {unit} would have been BLOCKED."
                       if rule_off else
                       f"{k} is exempt from the rolling-window limit, so the {unit} proceeds "
                       f"(#{in_window + 1} in the window). THIS IS THE FLIP: without the "
                       f"exemption, the limit of {N} per {DAYS} would have BLOCKED this {unit}; "
                       f"the modification exempts {k}, so it is allowed.")
                e.expect = EventExpect(
                    action=_fill_outcome(outcomes, "allowed",
                        f"the {unit} for {k} is within the limit and IS performed ({_ev_ref(e)}).",
                        ID=_ev_ref(e), KEY=k, CONTACT=(contacts or {}).get(k) or "the configured recipient"),
                    reason=why)
            else:
                lead = (f"the rolling-window limit was retired by the modification, so the {unit} "
                        f"proceeds regardless of the window count." if rule_off else
                        f"{k} is exempt from the rolling-window limit, so the {unit} proceeds "
                        f"regardless of the window count." if is_exempt else
                        f"only {in_window} {unit}(s) for {k} in the last {DAYS} (< {N}); the limit "
                        f"is PER key, so {k} is unaffected by other keys.")
                e.expect = EventExpect(
                    action=_fill_outcome(outcomes, "allowed",
                        f"the {unit} for {k} is within the limit and IS performed ({_ev_ref(e)}).",
                        ID=_ev_ref(e), KEY=k, CONTACT=(contacts or {}).get(k) or "the configured recipient"),
                    reason=f"{lead}{note}{flip}")
        else:
            e.expect = EventExpect(
                action=_fill_outcome(outcomes, "blocked",
                    f"the {unit} for {k} ({_ev_ref(e)}) is NOT performed — it is blocked by the rolling-window limit.",
                    ID=_ev_ref(e), KEY=k, CONTACT=(contacts or {}).get(k) or "the configured recipient"),
                reason=f"{in_window} {unit}(s) were already done for {k} within the last {DAYS} (the "
                       f"limit of {N}), so a new one is blocked until that key's window clears.")
        out.append(e)
    return out


def build_trigger_scenario(seed: str, threshold: str, key: str, phrase,
                           base_day: str = "W01-1", id_offset: int = 0,
                           outcomes: dict | None = None, unit: str = "escalation",
                           keys: list | None = None, flip_old_limit: int | None = None,
                           exempt_key: str | None = None, rule_off: bool = False,
                           single_key: bool = False, contacts: dict | None = None) -> list:
    """Quorum / threshold-trigger — the INVERSE of a rate limit: the Nth related occurrence for a
    key within the rolling window FIRES the gated action (an escalation, a digest, a ticket);
    earlier occurrences only accumulate. After firing, that key's count RESETS. Builds BY
    CONSTRUCTION: (N-1) accumulating events for the main key, one mid-stream event for a SECOND
    key (counts are per-key), a sequential boundary pair at the quorum (the first is the Nth and
    FIRES; the next is CONSOLIDATED into the open fired unit), and one post-window event.
    Mod flips: `flip_old_limit` (quorum changed) marks fires/records that flip vs the old quorum;
    `exempt_key` never fires (its quorum-reaching event is the FLIP).
    `phrase(req_id, key)` returns the raw-stimulus input text."""
    N = _parse_limit(threshold)
    D = _parse_window_days(threshold)
    DAYS = f"{D} day" + ("s" if D != 1 else "")
    week, day = int(base_day[1:3]), int(base_day[4:])
    key2 = None if single_key else _second_key(seed, key, keys)
    events: list = []   # (event, key)

    def add(text, when, k, cg=None):
        events.append((_ev(len(events) + 1, text, when, cg), k))

    rid = lambda i: f"REQ-{i + id_offset:04d}"
    n = 0
    gap = 2 if (N - 1) * 2 < D else 0
    for i in range(N - 1):                           # (N-1) accumulating events for the main key
        n += 1
        add(phrase(rid(n), key), f"W{week:02d}-{day + i * gap}T{9 + (0 if gap else i):02d}:00", key)
        if i == 0 and key2 and not exempt_key:       # a SECOND key mid-stream → counts are PER KEY
            n += 1
            add(phrase(rid(n), key2), f"W{week:02d}-{day + i * gap}T{10 + (0 if gap else i):02d}:00", key2)
    pair_day = day + (N - 1) * gap         # boundary pair: sequential, minutes apart (no race) —
    hh = 11 + (0 if gap else N - 1)        # the first reaches the quorum and FIRES; the next lands
    n += 1; add(phrase(rid(n), key), f"W{week:02d}-{pair_day}T{hh:02d}:30", key)     # on the freshly
    n += 1; add(phrase(rid(n), key), f"W{week:02d}-{pair_day}T{hh:02d}:35", key)     # fired state
    if single_key:
        key2 = None
    if key2 and exempt_key:
        # SCOPE PROOF for an exemption: the OTHER key reaches its quorum and still FIRES —
        # the exemption changes nothing outside its key
        for j in range(N):
            n += 1
            add(phrase(rid(n), key2), f"W{week:02d}-{pair_day}T{13 + j // 12:02d}:{(j * 5) % 60:02d}", key2)
    post_abs = (week - 1) * 7 + pair_day + D + 1     # past the window → fresh accumulation
    n += 1; add(phrase(rid(n), key), f"W{(post_abs - 1) // 7 + 1:02d}-{(post_abs - 1) % 7 + 1}T09:00", key)

    # simulate the quorum PER KEY: accumulate in-window; the Nth fires; while the fired unit's
    # window is still open, further occurrences are CONSOLIDATED into it (visible merge), and a
    # fresh accumulation starts only past the window
    acc: dict[str, list[int]] = {}
    fired_at: dict[str, int] = {}
    out = []
    for e, k in events:
        d = _abs_day(e.when)
        c = sum(1 for ad in acc.get(k, []) if d - ad < D) + 1   # incl. this occurrence
        is_exempt = (exempt_key is not None and k == exempt_key) or rule_off
        if k in fired_at and d - fired_at[k] < D and not is_exempt:
            ago = d - fired_at[k]
            # artifact-neutral wording: a sent email cannot be amended — post-fire occurrences are
            # LOGGED AGAINST the already-issued unit. An LLM 'consolidated' phrasing (allowed only
            # for MUTABLE artifacts like tickets/docs) overrides the action wording.
            e.expect = EventExpect(
                action=_fill_outcome(outcomes, "consolidated",
                    f"{_ev_ref(e)} is logged against the {unit} already issued for {k}; "
                    f"NO new {unit} fires.",
                    ID=_ev_ref(e), KEY=k, CONTACT=(contacts or {}).get(k) or "the configured recipient"),
                reason=f"a {unit} for {k} was already issued {ago} day(s) ago; until its {DAYS} "
                       f"window clears, further occurrences are only logged against that record — "
                       f"no new {unit} is started.")
            out.append(e)
            continue
        if c >= N and not is_exempt:
            acc[k] = []                                          # fired → reset the key's count
            fired_at[k] = d
            flip = (f" THIS IS THE FLIP: under the original quorum of {flip_old_limit} this is only "
                    f"occurrence #{c} and would NOT have fired yet; the modification (quorum {N}) "
                    f"fires it." if flip_old_limit and c < flip_old_limit else "")
            e.expect = EventExpect(
                action=_fill_outcome(outcomes, "fired",
                    f"the {unit} FIRES for {k} ({_ev_ref(e)}).", ID=_ev_ref(e), KEY=k, CONTACT=(contacts or {}).get(k) or "the configured recipient"),
                reason=f"this is occurrence #{c} for {k} within the last {DAYS} — the quorum of {N} "
                       f"is reached, so the {unit} fires and {k}'s count resets. The fired {unit} "
                       f"covers ALL of {k}'s occurrences accumulated in the window, not only this "
                       f"event.{flip}")
        else:
            acc.setdefault(k, []).append(d)
            if rule_off and c == N:
                flip = (f" THIS IS THE FLIP: the quorum rule was RETIRED by the modification — "
                        f"occurrence #{c} would have fired the {unit} under the retired quorum of "
                        f"{N}; now it is only recorded.")
            elif is_exempt and c == N:
                flip = (f" THIS IS THE FLIP: without the exemption, occurrence #{c} reaches the "
                        f"quorum of {N} and the {unit} would have FIRED; the modification exempts "
                        f"{k}, so it is only recorded.")
            elif flip_old_limit and c >= flip_old_limit and c < N:
                flip = (f" THIS IS THE FLIP: under the original quorum of {flip_old_limit} this "
                        f"occurrence #{c} would have FIRED; the modification (quorum {N}) only "
                        f"records it.")
            else:
                flip = ""
            # the exempt key's beyond-quorum events are recorded BECAUSE of the exemption — the
            # "below the quorum" lead-in would be false for them (occurrence #c >= N)
            lead = (f"the quorum rule was retired by the modification, so the occurrence is simply "
                    f"recorded; nothing fires anymore and no quorum is counted." if rule_off else
                    f"{k} is exempt from the quorum, so occurrence #{c} is only recorded; the "
                    f"exemption keeps {k} accumulating without firing."
                    if is_exempt and c >= N else
                    f"only {c} occurrence(s) for {k} within the last {DAYS} — below the quorum "
                    f"of {N}; counts are PER key.")
            e.expect = EventExpect(
                action=_fill_outcome(outcomes, "recorded",
                    f"{_ev_ref(e)} is recorded for {k}; NO {unit} fires.", ID=_ev_ref(e), KEY=k, CONTACT=(contacts or {}).get(k) or "the configured recipient"),
                reason=f"{lead}{flip}")
        out.append(e)
    return out


def build_dedup_scenario(seed: str, threshold: str, key: str, phrase,
                         base_day: str = "W01-1", id_offset: int = 0,
                         outcomes: dict | None = None, unit: str = "complaint",
                         keys: list | None = None, flip_old_window_min: int | None = None,
                         exempt_key: str | None = None, rule_off: bool = False,
                         single_key: bool = False, contacts: dict | None = None) -> list:
    """Duplicate suppression in a SHORT rolling window (minute-granular): the first occurrence for
    a key is processed; an identical repeat within W of the last processed one is IGNORED as a
    duplicate; past the window it is processed again as new. Builds BY CONSTRUCTION: processed →
    in-window repeat (ignored) → a DIFFERENT key mid-window (processed; dedup is per-key) → a
    CONCURRENT PAIR on a fresh key (exactly one processed, one deduplicated) → a same-key repeat
    past the window (processed again — the window expired).
    Mod flips: `flip_old_window_min` (window shortened) marks repeats processed-now/ignored-before;
    `exempt_key` is never deduplicated (its in-window repeat is the FLIP).
    `phrase(req_id, key)` returns the raw-stimulus input text."""
    W = _parse_window_minutes(threshold)
    WTXT = (f"{W} minute" + ("s" if W != 1 else "")) if W < 60 else \
           (f"{W // 60} hour" + ("s" if W // 60 != 1 else "")) if W % 60 == 0 and W < 1440 else \
           f"{W // 1440} day" + ("s" if W // 1440 != 1 else "")
    week, day = int(base_day[1:3]), int(base_day[4:])
    key2 = None if single_key else (_second_key(seed, key, keys) or f"{key}-B")
    events: list = []   # (event, key)

    def add(text, mins, k, cg=None):
        events.append((_ev(len(events) + 1, text, _minutes_to_when(week, day, mins), cg), k))

    rid = lambda i: f"REQ-{i + id_offset:04d}"
    # repeats land mid-window; the flip-window variant places the repeat between the NEW (shorter)
    # window and the OLD one, so it is processed now but would have been ignored before
    gap_in = (W + (flip_old_window_min - W) // 2) if flip_old_window_min and flip_old_window_min > W \
        else max(1, W // 3)
    add(phrase(rid(1), key), 0, key)                            # processed (first occurrence)
    add(phrase(rid(2), key), gap_in, key)                       # in-window repeat → ignored (or flip)
    if key2:
        pair_t = gap_in + 2
        # boundary pair on key2's FIRST contact, sequential minutes apart (no race): the first is
        # processed (also proving dedup is PER KEY — key's window is active), the repeat is deduped
        add(phrase(rid(3), key2), pair_t, key2)
        add(phrase(rid(4), key2), pair_t + 2, key2)
    add(phrase(rid(5), key), gap_in + W + 5, key)               # past the window → processed again

    # simulate: per key, the time of the LAST PROCESSED occurrence
    last: dict[str, int] = {}
    out = []
    for e, k in events:
        t = _abs_minutes(e.when)
        delta = t - last[k] if k in last else None
        is_exempt = (exempt_key is not None and k == exempt_key) or rule_off
        if delta is not None and delta < W and not is_exempt:
            e.expect = EventExpect(
                action=_fill_outcome(outcomes, "ignored",
                    f"{_ev_ref(e)} is recognized as a DUPLICATE and merged into the open {unit} "
                    f"for {k}; no new {unit} is created.",
                    ID=_ev_ref(e), KEY=k, CONTACT=(contacts or {}).get(k) or "the configured recipient"),
                reason=f"an identical {unit} for {k} was processed only {delta} minute(s) ago — "
                       f"within the {WTXT} dedup window, so this one is handled as a duplicate "
                       f"(merged, not re-processed).")
        else:
            expired = delta is not None and delta >= W
            if rule_off and delta is not None and delta < W:
                flip = (f" THIS IS THE FLIP: deduplication was RETIRED by the modification — this "
                        f"repeat ({delta} minute(s) after the last) would have been handled as a "
                        f"duplicate under the retired {WTXT} window; now it is processed.")
            elif is_exempt and delta is not None and delta < W:
                flip = (f" THIS IS THE FLIP: without the exemption, this repeat ({delta} minute(s) "
                        f"after the last) falls inside the {WTXT} window and would have been "
                        f"IGNORED as a duplicate; the modification exempts {k}.")
            elif flip_old_window_min and delta is not None and delta < flip_old_window_min:
                flip = (f" THIS IS THE FLIP: under the original {flip_old_window_min}-minute window "
                        f"this repeat ({delta} minute(s) after the last) would have been IGNORED as "
                        f"a duplicate; the modification ({WTXT} window) processes it.")
            else:
                flip = ""
            note = (f" The dedup window has expired — more than {WTXT} have passed since the last "
                    f"processed {unit} for {k}." if expired and not flip else "")
            last[k] = t
            # the exempt in-window repeat is processed BECAUSE of the exemption — the "no recent
            # occurrence" lead-in would be false for it (one WAS processed delta minutes ago)
            lead = (f"deduplication was retired by the modification, so the {unit} is processed "
                    f"despite the repeat." if rule_off and delta is not None and delta < W else
                    f"{k} is exempt from deduplication, so the {unit} is processed despite the "
                    f"repeat." if is_exempt and delta is not None and delta < W else
                    f"no {unit} for {k} was processed within the last {WTXT}; dedup is PER key.")
            e.expect = EventExpect(
                action=_fill_outcome(outcomes, "allowed",
                    f"the {unit} {_ev_ref(e)} IS processed.", ID=_ev_ref(e), KEY=k, CONTACT=(contacts or {}).get(k) or "the configured recipient"),
                reason=f"{lead}{note}{flip}")
        out.append(e)
    return out


def _qty_noun(threshold: str) -> str:
    """The quantity noun for non-monetary caps ("15 vacation days per ..." → "vacation days")."""
    m = re.search(r"\d[\d,]*\s+([a-z][a-z -]*?)(?:\s+per\b|\s+within\b|\s+across\b|,|\.|$)",
                  threshold or "", re.I)
    return (m.group(1).strip() if m else "units")


def build_cap_scenario(seed: str, threshold: str, submit_phrase, approve_phrase,
                       submitters: list, base_day: str = "W01-1", starting_total: int = 0, id_offset: int = 0,
                       outcomes: dict | None = None, unit: str = "approval",
                       flip_old_limit: int | None = None, per_person: bool = False,
                       person_caps: dict | None = None, qty_noun: str = "") -> list:
    """Cumulative cap with an approver chain. `submitters` is [(rep, manager)]. Each request: the
    REP submits (the event is just the stimulus — it does NOT choose the approver); the submission
    EXPECT verifies the SYSTEM routed the request to the rep's MANAGER; the manager's decision is
    the gated action. Amounts approach the cap with a sequential boundary pair, one over-cap held.
    UNIT-AWARE: "$" amounts only when the threshold is monetary; otherwise the threshold's own
    quantity noun ("vacation days"). `per_person=True` keeps a SEPARATE running total per
    submitter (per-person budgets): person A gets the boundary treatment; person B's mid-stream
    request proceeds under B's OWN total — proving the cap is per person, not shared."""
    money = "$" in (threshold or "")
    noun = (qty_noun or _qty_noun(threshold)) if not money else ""
    F = (lambda v: f"${v:,}") if money else (lambda v: f"{v:,} {noun}")
    scope = "their" if per_person else "the"
    subm = submitters or [("an account executive", "the approver")]
    # per-person caps can DIFFER per person (each employee's own seeded balance)
    cap_for = lambda rep: (person_caps or {}).get(rep) or _parse_limit(threshold)
    cap = cap_for(subm[0][0])
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
        # per-person: the boundary person (A) submits everything; B's scope-proof comes after
        rep, mgr = subm[0] if per_person else subm[i % len(subm)]
        quotes.append((qid, amt, i >= 2, rep, mgr))
    if per_person and len(subm) > 1:
        repB, mgrB = subm[1]
        quotes.insert(2, (f"Q-{1001 + id_offset + 4}", a, False, repB, mgrB))
    for i, (qid, amt, is_pair, rep, mgr) in enumerate(quotes):
        e = _ev(len(events) + 1, submit_phrase(qid, amt, rep), f"W{week:02d}-{day}T{9 + i:02d}:00")
        # The submission expect VERIFIES the system routed the approval to the rep's MANAGER.
        e.expect = EventExpect(
            action=_fill_outcome(outcomes, "submitted",
                f"{qid} is recorded; the system routes the approval request to {rep}'s manager, {mgr}.",
                ID=qid, SUBMITTER=rep, MANAGER=mgr),
            reason=f"the system's routing policy sends each request to the submitter's manager ({mgr} "
                   f"for {rep}); the submission itself is always recorded and is not the gated action.")
        events.append(e)

    # approvals: the routed MANAGER decides; cap-gated. Boundary approvals are SEQUENTIAL,
    # minutes apart (no same-instant race — outcomes stay deterministic per event).
    totals: dict = {}
    approve_events = []
    pair_seen = 0
    for j, (qid, amt, is_pair, rep, mgr) in enumerate(quotes):
        if is_pair:
            when = f"W{week:02d}-{day}T14:{pair_seen * 5:02d}"
            pair_seen += 1
        else:
            when = f"W{week:02d}-{day}T{10 + j:02d}:30"
        e = _ev(len(events) + len(approve_events) + 1, approve_phrase(qid, mgr), when)
        approve_events.append((e, qid, amt, rep, mgr))

    ordered = sorted(approve_events, key=lambda x: (x[0].when, x[0].id))
    for e, qid, amt, rep, mgr in ordered:
        key = rep if per_person else "__shared__"
        total = totals.get(key, starting_total if (not per_person or rep == subm[0][0]) else 0)
        whose = f"{rep}'s" if per_person else "the"
        per_note = f"; {unit} totals are PER PERSON — {rep}'s own budget, unaffected by anyone else's"             if per_person else ""
        if total + amt <= cap_r:
            totals[key] = total + amt
            total += amt
            flip = (f" THIS IS THE FLIP: this approval brings {whose} running total to {F(total)}, above "
                    f"the original {F(flip_old_limit)} cap which would have HELD it, but the modification "
                    f"({F(cap)}) ALLOWS it." if flip_old_limit and total > flip_old_limit else "")
            e.expect = EventExpect(
                action=_fill_outcome(outcomes, "approved",
                    f"{mgr} approves {qid} ({F(amt)}); the {unit} is recorded.",
                    ID=qid, ENTITY=mgr, AMOUNT=f"{amt:,}"),
                reason=f"the {unit} of {F(amt)} keeps {whose} running approved total at {F(total)}, "
                       f"within {cap_phrase}{per_note}.{flip}")
        else:
            e.expect = EventExpect(
                action=_fill_outcome(outcomes, "held",
                    f"{mgr} acts on {qid}, BUT the system holds it for exception handling — the {unit} cannot "
                    f"take effect because it would breach the cap, so none is recorded.",
                    ID=qid, ENTITY=mgr, AMOUNT=f"{amt:,}"),
                reason=f"the {unit} of {F(amt)} would push {whose} running total from {F(total)} to "
                       f"{F(total + amt)}, over {cap_phrase}, so the system intercepts it and "
                       f"routes it to exception handling{per_note}.")
    return events + [e for e, *_ in approve_events]


def _ev_ref(e) -> str:
    m = re.search(r"\bREQ-\d+\b", e.input or "")
    return m.group(0) if m else "the request"
