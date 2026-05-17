"""Unit tests for the pluggable memory backends — FlatKeyValueMemory and
NestedJsonMemory — plus an integration test that drives LLMObject with the
nested backend through a scripted MockBrain.
"""
from __future__ import annotations

import json

import pytest

from src.lnl.brain import MockBrain
from src.lnl.memory import (
    FlatKeyValueMemory,
    NestedDelta,
    NestedJsonMemory,
    make_backend,
)
from src.lnl.object import LLMObject
from src.lnl.types import (
    Message,
    MessageType,
    ObjectDefinition,
    ReactFinish,
    ReactStep,
    StateDelta,
)


def _make_definition(object_id: str = "test-obj") -> ObjectDefinition:
    return ObjectDefinition(
        object_id=object_id,
        role="Test object.",
        behavior="Process messages and update state.",
    )


def _user_msg(content: str, sender: str = "__user__") -> Message:
    return Message(sender=sender, recipient="test-obj", type=MessageType.DOMAIN, content=content)


# ─── Flat backend parity ─────────────────────────────────────────────────────

class TestFlatBackend:
    def test_set_adds_top_level_key(self):
        m = FlatKeyValueMemory()
        m.apply([StateDelta(op="set", key="count", value=1)])
        assert m.snapshot() == {"count": 1}

    def test_delete_removes_key(self):
        m = FlatKeyValueMemory(initial={"a": 1, "b": 2})
        m.apply([StateDelta(op="delete", key="a")])
        assert m.snapshot() == {"b": 2}

    def test_append_creates_list_then_pushes(self):
        m = FlatKeyValueMemory()
        m.apply([StateDelta(op="append", key="log", value="x")])
        m.apply([StateDelta(op="append", key="log", value="y")])
        assert m.snapshot() == {"log": ["x", "y"]}

    def test_serialize_roundtrip(self):
        m = FlatKeyValueMemory(initial={"a": 1})
        clone = FlatKeyValueMemory(initial=m.serialize())
        assert clone.snapshot() == {"a": 1}


# ─── Nested backend ──────────────────────────────────────────────────────────

class TestNestedBackend:
    def test_set_creates_nested_parents(self):
        m = NestedJsonMemory()
        m.apply([NestedDelta(op="set", path="tickets.T-042.status", value="open")])
        assert m.snapshot() == {"tickets": {"T-042": {"status": "open"}}}

    def test_set_replaces_existing_value(self):
        m = NestedJsonMemory(initial={"tickets": {"T-042": {"status": "open"}}})
        m.apply([NestedDelta(op="set", path="tickets.T-042.status", value="closed")])
        assert m.snapshot() == {"tickets": {"T-042": {"status": "closed"}}}

    def test_merge_deep_merges_subtree(self):
        m = NestedJsonMemory(initial={"tickets": {"T-042": {"status": "open", "priority": "P1"}}})
        m.apply([NestedDelta(op="merge", path="tickets.T-042", value={"status": "closed", "assignee": "Alice"})])
        assert m.snapshot() == {
            "tickets": {"T-042": {"status": "closed", "priority": "P1", "assignee": "Alice"}}
        }

    def test_merge_into_missing_path_acts_as_set(self):
        m = NestedJsonMemory()
        m.apply([NestedDelta(op="merge", path="settings", value={"theme": "dark"})])
        assert m.snapshot() == {"settings": {"theme": "dark"}}

    def test_delete_nested_leaf(self):
        m = NestedJsonMemory(initial={"users": {"alice": {"role": "admin"}, "bob": {"role": "user"}}})
        m.apply([NestedDelta(op="delete", path="users.bob")])
        assert m.snapshot() == {"users": {"alice": {"role": "admin"}}}

    def test_delete_missing_is_noop(self):
        m = NestedJsonMemory(initial={"a": 1})
        changed = m.apply([NestedDelta(op="delete", path="nope.missing")])
        assert changed == []
        assert m.snapshot() == {"a": 1}

    def test_append_creates_array_then_pushes(self):
        m = NestedJsonMemory()
        m.apply([NestedDelta(op="append", path="audit_log", value={"who": "alice"})])
        m.apply([NestedDelta(op="append", path="audit_log", value={"who": "bob"})])
        assert m.snapshot() == {"audit_log": [{"who": "alice"}, {"who": "bob"}]}

    def test_append_to_scalar_coerces_to_list(self):
        m = NestedJsonMemory(initial={"x": "first"})
        m.apply([NestedDelta(op="append", path="x", value="second")])
        assert m.snapshot() == {"x": ["first", "second"]}

    def test_root_merge(self):
        m = NestedJsonMemory(initial={"a": 1})
        m.apply([NestedDelta(op="merge", path="", value={"b": 2})])
        assert m.snapshot() == {"a": 1, "b": 2}

    def test_noop_set_reports_no_change(self):
        m = NestedJsonMemory(initial={"a": {"b": 1}})
        changed = m.apply([NestedDelta(op="set", path="a.b", value=1)])
        assert changed == []

    def test_changed_paths_lists_what_actually_changed(self):
        m = NestedJsonMemory(initial={"a": 1})
        changed = m.apply([
            NestedDelta(op="set", path="a", value=1),       # no-op
            NestedDelta(op="set", path="b", value=2),       # changes
            NestedDelta(op="delete", path="missing"),       # no-op
            NestedDelta(op="merge", path="c", value={"d": 3}),  # changes
        ])
        assert changed == ["b", "c"]

    def test_apply_is_immutable_on_caller_input(self):
        # The backend should never mutate the dict passed via initial / load —
        # callers can keep using their original reference.
        original = {"users": {"alice": {"role": "admin"}}}
        m = NestedJsonMemory(initial=original)
        m.apply([NestedDelta(op="set", path="users.alice.role", value="superuser")])
        assert original == {"users": {"alice": {"role": "admin"}}}

    def test_clone_is_independent(self):
        m = NestedJsonMemory(initial={"a": {"b": 1}})
        c = m.clone()
        c.apply([NestedDelta(op="set", path="a.b", value=99)])
        assert m.snapshot() == {"a": {"b": 1}}
        assert c.snapshot() == {"a": {"b": 99}}

    def test_serialize_load_roundtrip(self):
        m = NestedJsonMemory(initial={"users": {"alice": {"role": "admin"}}})
        clone = NestedJsonMemory(initial=m.serialize())
        assert clone.snapshot() == m.snapshot()

    def test_parse_delta_accepts_valid_shapes(self):
        m = NestedJsonMemory()
        d = m.parse_delta({"op": "set", "path": "a.b", "value": 1})
        assert isinstance(d, NestedDelta) and d.op == "set" and d.path == "a.b" and d.value == 1

    def test_parse_delta_rejects_bad_op(self):
        m = NestedJsonMemory()
        assert m.parse_delta({"op": "replace", "path": "a", "value": 1}) is None
        assert m.parse_delta({"op": "set"}) is None
        assert m.parse_delta(None) is None


# ─── Factory ─────────────────────────────────────────────────────────────────

def test_make_backend_factory():
    assert isinstance(make_backend("flat"), FlatKeyValueMemory)
    assert isinstance(make_backend("nested"), NestedJsonMemory)
    # Unknown name falls back to flat (safer default than raising).
    assert isinstance(make_backend("unknown"), FlatKeyValueMemory)


# ─── Integration: LLMObject with nested backend ──────────────────────────────

class TestLLMObjectNestedBackend:
    def test_nested_delta_applies_through_object(self):
        brain = MockBrain()
        brain.script_react(ReactStep(
            thought="Add a ticket.",
            action="finish",
            state_updates=[
                NestedDelta(op="set", path="tickets.T-042",
                            value={"status": "open", "priority": "P1"}),
            ],
            finish=ReactFinish(reply="ok"),
        ))
        obj = LLMObject(_make_definition(), brain, memory_backend="nested",
                        enable_planner=False, enable_evaluator=False)
        result = obj.process_message(_user_msg("add T-042"))
        assert result.state_after == {"tickets": {"T-042": {"status": "open", "priority": "P1"}}}
        assert obj.state == {"tickets": {"T-042": {"status": "open", "priority": "P1"}}}

    def test_nested_targeted_update_does_not_clobber_siblings(self):
        brain = MockBrain()
        brain.script_react(ReactStep(
            thought="Close T-042.",
            action="finish",
            state_updates=[
                NestedDelta(op="set", path="tickets.T-042.status", value="closed"),
            ],
            finish=ReactFinish(reply="closed"),
        ))
        obj = LLMObject(_make_definition(), brain, memory_backend="nested",
                        enable_planner=False, enable_evaluator=False)
        obj.set_state({"tickets": {
            "T-042": {"status": "open", "priority": "P1", "assignee": "Alice"},
            "T-017": {"status": "open", "priority": "P2", "assignee": "Bob"},
        }})
        obj.process_message(_user_msg("close T-042"))
        # The targeted set only touched the status of T-042 — every other field
        # is preserved, which is the whole point of the nested backend.
        assert obj.state == {"tickets": {
            "T-042": {"status": "closed", "priority": "P1", "assignee": "Alice"},
            "T-017": {"status": "open", "priority": "P2", "assignee": "Bob"},
        }}

    def test_make_delta_returns_nested_for_nested_backend(self):
        # Regression: runtime call-sites (sink shim, knowledge-gap tracking)
        # build top-level deltas via backend.make_delta — they must round-trip
        # through NestedJsonMemory.apply without crashing.
        m = NestedJsonMemory()
        m.apply([m.make_delta("set", "auto_completion", {"status": "completed"})])
        m.apply([m.make_delta("append", "knowledge_gaps", {"question": "?"})])
        assert m.snapshot() == {
            "auto_completion": {"status": "completed"},
            "knowledge_gaps": [{"question": "?"}],
        }

    def test_runtime_built_deltas_dispatch_under_nested(self):
        # End-to-end: an object running with the nested backend that hits the
        # knowledge-gap path must not raise AttributeError on the runtime-built
        # delta. (Pre-fix this crashed in production with "StateDelta has no
        # attribute 'path'".)
        from src.lnl.types import KnowledgeGap
        brain = MockBrain()
        brain.script_react(ReactStep(
            thought="Asking peer.",
            action="finish",
            finish=ReactFinish(reply="", knowledge_gap=KnowledgeGap(question="who?", context="")),
        ))
        obj = LLMObject(
            _make_definition(), brain, memory_backend="nested",
            enable_planner=False, enable_evaluator=False,
            auto_track_knowledge_gaps=True,
        )
        obj.process_message(_user_msg("anything"))
        snap = obj.state
        assert "knowledge_gaps" in snap
        assert snap["knowledge_gaps"][0]["question"] == "who?"

    def test_nested_multiple_deltas_one_turn(self):
        brain = MockBrain()
        brain.script_react(ReactStep(
            thought="Add and log.",
            action="finish",
            state_updates=[
                NestedDelta(op="set", path="tickets.T-042",
                            value={"status": "open"}),
                NestedDelta(op="append", path="audit_log",
                            value={"who": "alice", "what": "created T-042"}),
            ],
            finish=ReactFinish(reply="ok"),
        ))
        obj = LLMObject(_make_definition(), brain, memory_backend="nested",
                        enable_planner=False, enable_evaluator=False)
        obj.process_message(_user_msg("create"))
        assert obj.state == {
            "tickets": {"T-042": {"status": "open"}},
            "audit_log": [{"who": "alice", "what": "created T-042"}],
        }
