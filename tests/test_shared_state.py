"""Unit tests for the deterministic per-object shared-state store and its two
tools (read_state / set_state). No API key required."""
import json

import pytest

from src.lnl.brain import MockBrain
from src.lnl.runtime import Runtime, SystemConfig
from src.lnl.parser import parse_object_text
from src.lnl.shared_state import (
    ReadStateExecutor,
    SetStateExecutor,
    SharedStateRegistry,
)
from src.lnl.state import State
from src.lnl.tools import ToolRegistry
from src.lnl.types import LLMResponse, ToolCall


def _call(tool, args):
    return ToolCall(id="c1", tool=tool, arguments=args)


# --- Store ------------------------------------------------------------------

class TestSharedStateStore:
    def test_read_apply_roundtrip(self):
        store = State(initial='{"items": []}')
        ok, err = store.write({"op": "append", "path": "items", "value": "a"})
        assert ok and err is None
        assert store.read() == {"items": ["a"]}

    def test_guarded_incr_max_rejected(self):
        store = State(initial='{"n": 90}')
        ok, err = store.write({"op": "incr", "path": "n", "by": 5, "max": 100})
        assert ok and store.read()["n"] == 95
        ok2, err2 = store.write({"op": "incr", "path": "n", "by": 50, "max": 100})
        assert not ok2 and "rejected" in (err2 or "")
        assert store.read()["n"] == 95  # unchanged after a rejected guard

    def test_guarded_reserve_cap_rejected(self):
        store = State()
        ok, _ = store.write({"op": "reserve", "path": "pool", "value": 8,
                             "cap": 10, "hold_id": "h1"})
        assert ok
        ok2, err2 = store.write({"op": "reserve", "path": "pool", "value": 5,
                                 "cap": 10, "hold_id": "h2"})
        assert not ok2 and err2  # 8 + 5 > 10 → rejected

    def test_malformed_delta_rejected(self):
        store = State()
        ok, err = store.write({"op": "bogus", "path": "x"})
        assert not ok and "malformed" in (err or "")

    def test_batch_applied_in_order(self):
        store = State()
        ok, _ = store.write([
            {"op": "set", "path": "a", "value": 1},
            {"op": "set", "path": "b", "value": 2},
        ])
        assert ok and store.read() == {"a": 1, "b": 2}

    def test_set_merge_delete_and_nested_paths(self):
        store = State()
        assert store.write({"op": "set", "path": "tickets.T-1.status", "value": "open"})[0]
        assert store.read() == {"tickets": {"T-1": {"status": "open"}}}
        assert store.write({"op": "merge", "path": "tickets.T-1", "value": {"owner": "me"}})[0]
        assert store.read()["tickets"]["T-1"] == {"status": "open", "owner": "me"}
        assert store.write({"op": "delete", "path": "tickets.T-1.owner"})[0]
        assert store.read()["tickets"]["T-1"] == {"status": "open"}

    def test_decr_min_bound_rejected(self):
        store = State(initial='{"credits": 3}')
        assert store.write({"op": "decr", "path": "credits", "by": 2, "min": 0})[0]
        assert store.read()["credits"] == 1
        ok, err = store.write({"op": "decr", "path": "credits", "by": 5, "min": 0})
        assert not ok and err  # would go below 0 → rejected
        assert store.read()["credits"] == 1


class TestGuardedReserveLifecycle:
    """reserve → confirm → release against a cap, the two-phase admission flow."""

    def test_reserve_confirm_release(self):
        store = State()
        # reserve two holds against a cap of 10
        assert store.write({"op": "reserve", "path": "pool", "value": 6, "cap": 10, "hold_id": "h1"})[0]
        assert store.write({"op": "reserve", "path": "pool", "value": 3, "cap": 10, "hold_id": "h2"})[0]
        pool = store.read()["pool"]
        assert pool["committed"] == 0
        assert {h["hold_id"]: h["amount"] for h in pool["holds"]} == {"h1": 6, "h2": 3}

        # a third reserve has no headroom (6 + 3 + 2 > 10) → rejected
        assert not store.write({"op": "reserve", "path": "pool", "value": 2, "cap": 10, "hold_id": "h3"})[0]

        # confirm h1 → its amount moves into committed, hold drops
        assert store.write({"op": "confirm", "path": "pool", "hold_id": "h1"})[0]
        pool = store.read()["pool"]
        assert pool["committed"] == 6
        assert [h["hold_id"] for h in pool["holds"]] == ["h2"]

        # release h2 → hold drops, committed unchanged
        assert store.write({"op": "release", "path": "pool", "hold_id": "h2"})[0]
        pool = store.read()["pool"]
        assert pool["committed"] == 6 and pool["holds"] == []

    def test_confirm_unknown_hold_is_rejected(self):
        store = State()
        store.write({"op": "reserve", "path": "pool", "value": 1, "cap": 10, "hold_id": "h1"})
        ok, err = store.write({"op": "confirm", "path": "pool", "hold_id": "ghost"})
        assert not ok and err

    def test_reserve_without_hold_id_rejected(self):
        store = State()
        ok, _ = store.write({"op": "reserve", "path": "pool", "value": 1, "cap": 10})
        assert not ok  # a hold needs an id


class TestStoreInternals:
    def test_init_from_dict_and_json_string_equivalent(self):
        from_str = State(initial='{"a": 1}')
        from_dict = State(initial={"a": 1})
        assert from_str.read() == from_dict.read() == {"a": 1}

    def test_empty_init_is_empty_dict(self):
        assert State().read() == {}
        assert State(initial=None).read() == {}

    def test_serialize_is_valid_json(self):
        store = State(initial='{"a": 1}')
        store.write({"op": "set", "path": "b", "value": 2})
        assert json.loads(store.serialize()) == {"a": 1, "b": 2}

    def test_read_returns_isolated_snapshot(self):
        store = State(initial='{"items": [1, 2]}')
        snap = store.read()
        snap["items"].append(999)  # mutate the snapshot
        assert store.read() == {"items": [1, 2]}  # store is untouched


# --- Registry ---------------------------------------------------------------

class TestRegistry:
    def test_ensure_is_idempotent(self):
        reg = SharedStateRegistry()
        s1 = reg.ensure("A", '{"x": 1}')
        s2 = reg.ensure("A")  # already exists — same store, initial ignored
        assert s1 is s2 and reg.get("A").read() == {"x": 1}

    def test_get_unknown_is_none(self):
        assert SharedStateRegistry().get("nope") is None

    def test_owners_are_isolated(self):
        reg = SharedStateRegistry()
        reg.ensure("A", '{"v": 1}')
        reg.ensure("B", '{"v": 2}')
        reg.get("A").write({"op": "set", "path": "v", "value": 99})
        assert reg.get("A").read() == {"v": 99}
        assert reg.get("B").read() == {"v": 2}  # B untouched


class TestBackendDialectMatchesPrivate:
    """Shared state must speak the SAME delta dialect as private state — flat is
    key-based, nested is path-based — so the teaching ('same format as
    state_update') is true and the LLM isn't handed two dialects."""

    def test_set_state_spec_matches_backend(self):
        from src.lnl.shared_state import build_set_state_spec
        nested = set(build_set_state_spec("nested").arguments_schema["properties"])
        flat = set(build_set_state_spec("flat").arguments_schema["properties"])
        assert "path" in nested and "key" not in nested
        assert "key" in flat and "path" not in flat

    def test_store_accepts_backend_native_delta(self):
        # nested store takes a path; flat store takes a key
        assert SharedStateRegistry("nested").ensure("A").write({"op": "set", "path": "x", "value": 1})[0]
        assert SharedStateRegistry("flat").ensure("A").write({"op": "set", "key": "x", "value": 1})[0]


# --- Tools ------------------------------------------------------------------

class TestTools:
    def _setup(self):
        reg = SharedStateRegistry()
        reg.ensure("A", '{"committed": 0, "cap": 100}')
        reg.ensure("B")
        return reg, SetStateExecutor(reg), ReadStateExecutor(reg)

    def test_owner_writes_own_state(self):
        reg, setx, _ = self._setup()
        r = setx.execute(_call("set_state", {"op": "incr", "path": "committed",
                                             "by": 30, "max": 100}), {"self_id": "A"})
        assert not r.error
        assert reg.get("A").read()["committed"] == 30

    def test_guarded_reject_surfaces_as_error(self):
        reg, setx, _ = self._setup()
        r = setx.execute(_call("set_state", {"op": "incr", "path": "committed",
                                             "by": 9999, "max": 100}), {"self_id": "A"})
        assert r.error and not r.output

    def test_cross_object_read(self):
        reg, setx, readx = self._setup()
        setx.execute(_call("set_state", {"op": "set", "path": "committed",
                                         "value": 42}), {"self_id": "A"})
        # B reads A's shared state
        r = readx.execute(_call("read_state", {"owner": "A"}), {"self_id": "B"})
        assert not r.error and json.loads(r.output)["committed"] == 42

    def test_read_defaults_to_self(self):
        reg, setx, readx = self._setup()
        r = readx.execute(_call("read_state", {}), {"self_id": "A"})
        assert not r.error and json.loads(r.output)["cap"] == 100

    def test_foreign_write_rejected(self):
        reg, setx, _ = self._setup()
        r = setx.execute(_call("set_state", {"owner": "A", "op": "set",
                                             "path": "x", "value": 1}), {"self_id": "B"})
        assert r.error and "own" in r.error.lower()
        assert "x" not in reg.get("A").read()  # A's state untouched

    def test_read_unknown_owner_errors(self):
        _, _, readx = self._setup()
        r = readx.execute(_call("read_state", {"owner": "Z"}), {"self_id": "B"})
        assert r.error and "unknown" in r.error.lower()

    def test_set_state_with_no_delta_errors(self):
        reg, setx, _ = self._setup()
        r = setx.execute(_call("set_state", {}), {"self_id": "A"})
        assert r.error


# --- Concurrency smoke ------------------------------------------------------

# --- Integration: real ReAct dispatch through a Runtime ---------------------

_OWNER_MD = """# Quota Owner

## Role
Single-writer owner of the daily discount budget.

## Shared State
{"committed": 0, "cap": 100}
"""


@pytest.mark.parametrize("dispatch", ["sync", "async"])
def test_set_state_tool_call_mutates_store_through_runtime(dispatch):
    """The real feature path: an LLM emits a set_state tool call, the ReAct loop
    dispatches it (sync inline OR async REPLY round-trip), and the peerless leaf
    object's shared store is mutated — then the finish reply is produced."""
    brain = MockBrain()
    # Round 1: emit a guarded set_state tool call (no finish yet).
    brain.script("quota-owner", LLMResponse(
        updated_state={}, reply="",
        tool_calls=[ToolCall(id="t1", tool="set_state",
                             arguments={"op": "incr", "path": "committed", "by": 40, "max": 100})],
    ))
    # Round 2 (after the tool result): finish.
    brain.script("quota-owner", LLMResponse(updated_state={}, reply="Reserved 40 of the budget."))

    rt = Runtime(brain, tool_registry=ToolRegistry(),
                 system_config=SystemConfig(tool_dispatch=dispatch))
    defn, _ = parse_object_text(_OWNER_MD)
    obj = rt._register_object(defn)
    assert not defn.peers  # peerless leaf — the canonical shared-state owner

    rt.send("quota-owner", "Reserve 40 against the budget.")

    # The store was mutated via the deterministic guarded op, self_id threaded
    # through the real factory-built context.
    assert obj._shared_state_store.read()["committed"] == 40
    assert brain.call_log, "brain should have been invoked"


@pytest.mark.parametrize("dispatch", ["sync", "async"])
def test_guarded_reject_through_runtime_leaves_store_unchanged(dispatch):
    """A guarded op that breaks its cap is rejected deterministically; the tool
    result surfaces an error and the store is not mutated."""
    brain = MockBrain()
    brain.script("quota-owner", LLMResponse(
        updated_state={}, reply="",
        tool_calls=[ToolCall(id="t1", tool="set_state",
                             arguments={"op": "incr", "path": "committed", "by": 999, "max": 100})],
    ))
    brain.script("quota-owner", LLMResponse(updated_state={}, reply="Could not reserve."))

    rt = Runtime(brain, tool_registry=ToolRegistry(),
                 system_config=SystemConfig(tool_dispatch=dispatch))
    defn, _ = parse_object_text(_OWNER_MD)
    obj = rt._register_object(defn)

    rt.send("quota-owner", "Reserve 999 against the budget.")

    assert obj._shared_state_store.read()["committed"] == 0  # unchanged


def test_concurrent_reads_during_writes_stay_consistent():
    import threading

    store = State(initial='{"n": 0}')
    errors = []

    def writer():
        for _ in range(200):
            store.write({"op": "incr", "path": "n", "by": 1})

    def reader():
        for _ in range(200):
            snap = store.read()
            if not isinstance(snap.get("n"), int):
                errors.append(snap)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader),
               threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert store.read()["n"] == 200
