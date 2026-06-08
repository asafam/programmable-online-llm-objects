"""Unit tests for guarded state ops (incr/decr/reserve/confirm/release) in both
memory backends. No API key required.

These ops enforce an invariant deterministically: an op that would break its
bound is a no-op. See docs/CUSTODIAN_SPEC.md.
"""
import json

import pytest

from src.lnl.memory import FlatKeyValueMemory, NestedJsonMemory


# --- helpers ----------------------------------------------------------------

def _flat(initial):
    return FlatKeyValueMemory(initial=initial)


def _nested(initial):
    return NestedJsonMemory(initial=initial)


def _apply(backend, raw):
    """Parse a raw op dict and apply it; return (changed_keys, state_dict)."""
    delta = backend.parse_delta(raw)
    assert delta is not None, f"parse_delta returned None for {raw}"
    changed = backend.apply([delta])
    return changed, json.loads(backend.serialize())


# ============================================================================
# Flat backend
# ============================================================================

def test_flat_incr_accept_and_create():
    # missing key starts at 0
    changed, st = _apply(_flat({}), {"op": "incr", "key": "x", "by": 1})
    assert st["x"] == 1 and changed == ["x"]


def test_flat_incr_rejected_above_max_is_noop():
    b = _flat({"x": 1})
    changed, st = _apply(b, {"op": "incr", "key": "x", "by": 1, "max": 1})
    assert st["x"] == 1            # unchanged — would exceed max
    assert changed == []           # rejection surfaces as "nothing changed"


def test_flat_incr_under_max_accepts():
    changed, st = _apply(_flat({"x": 0}), {"op": "incr", "key": "x", "by": 1, "max": 1})
    assert st["x"] == 1 and changed == ["x"]


def test_flat_decr_below_default_min_zero_is_noop():
    changed, st = _apply(_flat({"x": 0}), {"op": "decr", "key": "x", "by": 1})
    assert st["x"] == 0 and changed == []


def test_flat_decr_accepts_down_to_min():
    changed, st = _apply(_flat({"x": 2}), {"op": "decr", "key": "x", "by": 1})
    assert st["x"] == 1 and changed == ["x"]


def test_flat_reserve_confirm_release_cycle():
    b = _flat({})
    # reserve 24 against cap 50
    _, st = _apply(b, {"op": "reserve", "key": "budget", "value": 24, "cap": 50, "hold_id": "h1"})
    assert st["budget"]["committed"] == 0
    assert st["budget"]["holds"] == [{"hold_id": "h1", "amount": 24}]

    # reserve 30 more would be 0+24+30=54 > 50 → rejected, holds unchanged
    changed, st = _apply(b, {"op": "reserve", "key": "budget", "value": 30, "cap": 50, "hold_id": "h2"})
    assert changed == [] and [h["hold_id"] for h in st["budget"]["holds"]] == ["h1"]

    # reserve 10 → 0+24+10=34 ≤ 50 → accepted (cap remembered from state)
    _, st = _apply(b, {"op": "reserve", "key": "budget", "value": 10, "hold_id": "h3"})
    assert [h["hold_id"] for h in st["budget"]["holds"]] == ["h1", "h3"]

    # confirm h1 → committed 24, hold removed
    _, st = _apply(b, {"op": "confirm", "key": "budget", "hold_id": "h1"})
    assert st["budget"]["committed"] == 24
    assert [h["hold_id"] for h in st["budget"]["holds"]] == ["h3"]

    # release h3 → hold removed, committed unchanged
    _, st = _apply(b, {"op": "release", "key": "budget", "hold_id": "h3"})
    assert st["budget"]["committed"] == 24 and st["budget"]["holds"] == []


def test_flat_confirm_unknown_hold_is_noop():
    b = _flat({"budget": {"committed": 5, "holds": []}})
    changed, st = _apply(b, {"op": "confirm", "key": "budget", "hold_id": "nope"})
    assert changed == [] and st["budget"] == {"committed": 5, "holds": []}


def test_flat_cap_of_one_simulation():
    # The §8 cap-of-1: first reserve wins, second is rejected.
    b = _flat({})
    changed_a, _ = _apply(b, {"op": "reserve", "key": "x", "value": 1, "cap": 1, "hold_id": "A"})
    changed_b, st = _apply(b, {"op": "reserve", "key": "x", "value": 1, "cap": 1, "hold_id": "B"})
    assert changed_a == ["x"] and changed_b == []
    assert [h["hold_id"] for h in st["x"]["holds"]] == ["A"]


def test_flat_reserve_requires_hold_id():
    changed, st = _apply(_flat({}), {"op": "reserve", "key": "x", "value": 1, "cap": 10})
    assert changed == [] and "x" not in st


# ============================================================================
# Nested backend (runtime default)
# ============================================================================

def test_nested_incr_creates_path():
    changed, st = _apply(_nested({}), {"op": "incr", "path": "counts.alice", "by": 1})
    assert st["counts"]["alice"] == 1 and changed == ["counts.alice"]


def test_nested_incr_rejected_above_max_is_noop():
    b = _nested({"counts": {"alice": 2}})
    changed, st = _apply(b, {"op": "incr", "path": "counts.alice", "by": 1, "max": 2})
    assert st["counts"]["alice"] == 2 and changed == []


def test_nested_reserve_confirm_at_path():
    b = _nested({})
    _, st = _apply(b, {"op": "reserve", "path": "budget", "value": 24, "cap": 50, "hold_id": "h1"})
    assert st["budget"]["holds"] == [{"hold_id": "h1", "amount": 24}]

    changed, st = _apply(b, {"op": "reserve", "path": "budget", "value": 30, "cap": 50, "hold_id": "h2"})
    assert changed == []          # 24+30 > 50 → rejected

    _, st = _apply(b, {"op": "confirm", "path": "budget", "hold_id": "h1"})
    assert st["budget"]["committed"] == 24 and st["budget"]["holds"] == []


def test_nested_cap_of_one_simulation():
    b = _nested({})
    ca, _ = _apply(b, {"op": "reserve", "path": "x", "value": 1, "cap": 1, "hold_id": "A"})
    cb, st = _apply(b, {"op": "reserve", "path": "x", "value": 1, "cap": 1, "hold_id": "B"})
    assert ca == ["x"] and cb == []
    assert [h["hold_id"] for h in st["x"]["holds"]] == ["A"]


# ============================================================================
# parse_delta round-trips the new params
# ============================================================================

def test_flat_parse_delta_carries_params():
    d = _flat({}).parse_delta({"op": "incr", "key": "x", "by": 2, "min": 0, "max": 9})
    assert d.op == "incr" and d.by == 2 and d.min == 0 and d.max == 9


def test_nested_parse_delta_carries_params():
    d = _nested({}).parse_delta({"op": "reserve", "path": "b", "value": 5, "cap": 10, "hold_id": "h"})
    assert d.op == "reserve" and d.value == 5 and d.cap == 10 and d.hold_id == "h"


def test_existing_ops_unaffected():
    # set/delete/append still behave as before in both backends.
    _, st = _apply(_flat({}), {"op": "set", "key": "k", "value": 7})
    assert st["k"] == 7
    _, st = _apply(_nested({}), {"op": "set", "path": "a.b", "value": 7})
    assert st["a"]["b"] == 7
