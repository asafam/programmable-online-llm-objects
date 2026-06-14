"""Pluggable memory backends for LLM-object state.

Two backends:

- `flat` (`FlatKeyValueMemory`): the original behaviour. State is a flat dict;
  the LLM emits one `{op, key, value}` delta per ReAct step with
  `op ∈ {set, delete, append}`. Top-level keys only — nested fields are
  re-emitted as full sub-dicts.

- `nested` (`NestedJsonMemory`): Redux-style. State is a nested JSON object;
  the LLM emits a list of `{op, path, value}` actions per ReAct step with
  `op ∈ {set, merge, delete, append}` and dotted paths
  (e.g. `"tickets.T-042.status"`). Apply is immutable (copy-on-write) and
  reports the list of paths that actually changed.

Selection is global per run via `SystemConfig.memory_backend` (string) or the
`--memory {flat,nested}` CLI flag; see `Runtime`.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from .types import StateDelta


# --- Nested-backend delta ----------------------------------------------------

@dataclass
class NestedDelta:
    """A single action emitted by the LLM at any ReAct step (nested backend).

    `path` is a dotted string addressing a nested location (`"a.b.c"`).
    The empty string `""` addresses the root.

    Guarded ops (incr/decr/reserve/confirm/release) target the leaf at `path`
    and use the same optional params as the flat backend's StateDelta. See
    docs/SHARED_STATE_SPEC.md.
    """
    op: str               # set | merge | delete | append | incr | decr | reserve | confirm | release
    path: str             # dotted path, "" = root
    value: Any = None     # set/merge/append value; reserve: the amount to hold
    # ── Guarded-op params (optional) ──────────────────────────────────────────
    by: Any = None        # incr/decr: amount to add (decr negates it)
    min: Any = None       # incr/decr: reject if result < min (decr defaults min=0)
    max: Any = None       # incr: reject if result > max
    cap: Any = None       # reserve: reject if committed + held + amount > cap
    hold_id: Any = None   # reserve/confirm/release: hold identifier


# --- Protocol ----------------------------------------------------------------

class MemoryBackend(Protocol):
    name: str
    prompt_file: str

    def snapshot(self) -> dict: ...
    def serialize(self) -> str: ...
    def load(self, state: Any) -> None: ...
    def apply(self, deltas: list) -> list[str]: ...
    def set_full(self, serialized: str) -> None: ...
    def clone(self) -> "MemoryBackend": ...
    def parse_delta(self, raw: dict) -> Any: ...
    def state_update_schema(self) -> dict: ...
    def state_update_is_list(self) -> bool: ...
    def make_delta(self, op: str, key: str, value: Any = None) -> Any: ...


# --- Flat (current) backend --------------------------------------------------

_FLAT_STATE_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Optional. Emit ONLY when a value genuinely changed. Omit entirely if nothing changed — do not invent updates.",
    "properties": {
        "op": {
            "type": "string",
            "enum": ["set", "delete", "append", "incr", "decr", "reserve", "confirm", "release"],
            "description": (
                "set: add/update a key. delete: remove a key. append: add to a list. "
                "GUARDED ops keep an invariant correct — a guarded op that would break "
                "its bound does nothing: "
                "incr: add `by` to the number at key (rejected if result > max or < min). "
                "decr: subtract `by` (rejected if result < min, default 0). "
                "reserve: hold `value` against `cap` in key's `holds` (rejected if "
                "committed + already-held + value > cap); requires `hold_id`. "
                "confirm: move `hold_id`'s amount into `committed`. "
                "release: drop `hold_id` from `holds`."
            ),
        },
        "key": {"type": "string", "description": "The state key to modify."},
        "value": {"description": "New value (set/append); the amount to hold (reserve). Omit for delete."},
        "by": {"type": "number", "description": "incr/decr: amount to add/subtract."},
        "min": {"type": "number", "description": "incr/decr lower bound; a result below it is rejected."},
        "max": {"type": "number", "description": "incr upper bound; a result above it is rejected."},
        "cap": {"type": "number", "description": "reserve ceiling for committed + held + value."},
        "hold_id": {"type": "string", "description": "reserve/confirm/release hold identifier."},
    },
    "required": ["op", "key"],
    "additionalProperties": False,
}


class FlatKeyValueMemory:
    """Flat top-level key/value state. Preserves pre-refactor behaviour exactly."""

    name = "flat"
    prompt_file = "executor_flat.yaml"

    def __init__(self, initial: Any = "") -> None:
        self._state: str = ""
        self.load(initial)

    # --- I/O ---

    def snapshot(self) -> dict:
        return _coerce_to_dict(self._state)

    def serialize(self) -> str:
        return self._state

    def load(self, state: Any) -> None:
        if isinstance(state, dict):
            self._state = json.dumps(state)
        elif state is None:
            self._state = ""
        else:
            self._state = str(state)

    def set_full(self, serialized: str) -> None:
        # Legacy fallback path: LLM returned a full updated_state string.
        self._state = serialized or ""

    def clone(self) -> "FlatKeyValueMemory":
        return FlatKeyValueMemory(initial=self._state)

    # --- Apply ---

    def apply(self, deltas: list) -> list[str]:
        if not deltas:
            return []
        current = _coerce_to_dict(self._state)
        if not isinstance(current, dict):
            current = {}
        changed: list[str] = []
        for d in deltas:
            before = current.get(d.key) if d.op != "delete" else None
            current = _apply_flat_delta(current, d)
            if d.op == "delete":
                changed.append(d.key)
            elif current.get(d.key) != before:
                changed.append(d.key)
        self._state = json.dumps(current)
        return changed

    # --- Delta parsing + schema ---

    def parse_delta(self, raw: dict) -> Optional[StateDelta]:
        if not isinstance(raw, dict):
            return None
        op = raw.get("op")
        key = raw.get("key")
        if not op or not key:
            return None
        return StateDelta(
            op=op, key=key, value=raw.get("value"),
            by=raw.get("by"), min=raw.get("min"), max=raw.get("max"),
            cap=raw.get("cap"), hold_id=raw.get("hold_id"),
        )

    def state_update_schema(self) -> dict:
        return copy.deepcopy(_FLAT_STATE_UPDATE_SCHEMA)

    def state_update_is_list(self) -> bool:
        return False

    def make_delta(self, op: str, key: str, value: Any = None) -> StateDelta:
        """Construct a backend-native delta for a runtime-built state write
        (knowledge-gap tracking, sink shim, etc.). For flat backend, `key` is
        the top-level state key."""
        return StateDelta(op=op, key=key, value=value)


def _apply_flat_delta(state: dict, delta: StateDelta) -> dict:
    """Apply one flat delta in-place and return the dict."""
    if delta.op == "set":
        state[delta.key] = delta.value
    elif delta.op == "delete":
        state.pop(delta.key, None)
    elif delta.op == "append":
        lst = state.get(delta.key, [])
        if not isinstance(lst, list):
            lst = [lst]
        lst.append(delta.value)
        state[delta.key] = lst
    elif delta.op in GUARDED_OPS:
        new, changed = _apply_guarded(state.get(delta.key), delta)
        if changed:
            state[delta.key] = new
    return state


# --- Guarded ops (shared by both backends) -----------------------------------
#
# These enforce an invariant deterministically: a guarded op that would violate
# its bound is a no-op (returns changed=False, leaving the value untouched), so
# the invariant holds regardless of what the LLM computed. Each helper takes the
# CURRENT value at a location and returns (new_value, changed); the backend then
# writes new_value only when changed. The delta carries the params (by/min/max/
# cap/hold_id) — both StateDelta and NestedDelta expose them, so this logic is
# backend-agnostic. See docs/SHARED_STATE_SPEC.md.

GUARDED_OPS: tuple[str, ...] = ("incr", "decr", "reserve", "confirm", "release")


def _num(v: Any, default: float = 0) -> float:
    """Coerce to a number; bools and non-numbers fall back to `default`."""
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return v
    return default


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _hold_container(cur: Any) -> tuple[dict, float, list]:
    """Normalize a reservation container `{committed, holds, cap?}`."""
    c = cur if isinstance(cur, dict) else {}
    committed = _num(c.get("committed", 0), 0)
    holds = c.get("holds")
    holds = holds if isinstance(holds, list) else []
    return c, committed, holds


def _guarded_incr(cur: Any, op: str, by: Any, lo: Any, hi: Any) -> tuple[Any, bool]:
    amt = _num(by, 0)
    if op == "decr":
        amt = -amt
        if lo is None:
            lo = 0
    base = _num(cur, 0)
    new = base + amt
    if hi is not None and _is_num(hi) and new > hi:
        return cur, False        # would exceed max → reject
    if lo is not None and _is_num(lo) and new < lo:
        return cur, False        # would fall below min → reject
    if new == base and _is_num(cur):
        return cur, False        # no actual change
    return new, True


def _guarded_reserve(cur: Any, amount: Any, cap: Any, hold_id: Any) -> tuple[Any, bool]:
    if hold_id is None:
        return cur, False        # a hold needs an id
    c, committed, holds = _hold_container(cur)
    eff_cap = cap if cap is not None else c.get("cap")
    amt = _num(amount, 0)
    held = sum(_num(h.get("amount", 0), 0) for h in holds if isinstance(h, dict))
    if eff_cap is not None and _is_num(eff_cap) and committed + held + amt > eff_cap:
        return cur, False        # no headroom → reject
    new = dict(c)
    new["committed"] = committed
    new["holds"] = list(holds) + [{"hold_id": hold_id, "amount": amt}]
    if eff_cap is not None:
        new["cap"] = eff_cap
    return new, True


def _guarded_confirm(cur: Any, hold_id: Any) -> tuple[Any, bool]:
    c, committed, holds = _hold_container(cur)
    match = next((h for h in holds if isinstance(h, dict) and h.get("hold_id") == hold_id), None)
    if match is None:
        return cur, False
    new = dict(c)
    new["committed"] = committed + _num(match.get("amount", 0), 0)
    new["holds"] = [h for h in holds if h is not match]
    return new, True


def _guarded_release(cur: Any, hold_id: Any) -> tuple[Any, bool]:
    c, committed, holds = _hold_container(cur)
    if not any(isinstance(h, dict) and h.get("hold_id") == hold_id for h in holds):
        return cur, False
    new = dict(c)
    new["committed"] = committed
    new["holds"] = [h for h in holds if not (isinstance(h, dict) and h.get("hold_id") == hold_id)]
    return new, True


def _apply_guarded(cur: Any, delta: Any) -> tuple[Any, bool]:
    """Dispatch a guarded op against the current value at a location.

    `delta` is a StateDelta or NestedDelta — only the op-param attributes
    (by/min/max/cap/hold_id/value) are read, never key/path."""
    op = delta.op
    if op in ("incr", "decr"):
        by = delta.by if delta.by is not None else delta.value
        return _guarded_incr(cur, op, by, delta.min, delta.max)
    if op == "reserve":
        amount = delta.value if delta.value is not None else delta.by
        return _guarded_reserve(cur, amount, delta.cap, delta.hold_id)
    if op == "confirm":
        return _guarded_confirm(cur, delta.hold_id)
    if op == "release":
        return _guarded_release(cur, delta.hold_id)
    return cur, False


# --- Nested JSON backend -----------------------------------------------------

_NESTED_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": ["set", "merge", "delete", "append", "incr", "decr", "reserve", "confirm", "release"],
            "description": (
                "set: write value at path (auto-creates parent dicts). "
                "merge: deep-merge a dict value into the existing dict at path. "
                "delete: remove the leaf at path. "
                "append: push value onto the array at path (creates [] if missing). "
                "GUARDED ops keep an invariant correct — one that would break its bound does nothing: "
                "incr: add `by` to the number at path (rejected if result > max or < min). "
                "decr: subtract `by` (rejected if result < min, default 0). "
                "reserve: hold `value` against `cap` in the container at path (rejected if "
                "committed + already-held + value > cap); requires `hold_id`. "
                "confirm: move `hold_id`'s amount into `committed`. "
                "release: drop `hold_id`."
            ),
        },
        "path": {
            "type": "string",
            "description": (
                "Dotted path to the location, e.g. 'tickets.T-042.status'. "
                "Empty string '' addresses the root."
            ),
        },
        "value": {"description": "New value (set/merge/append); the amount to hold (reserve). Omit for delete."},
        "by": {"type": "number", "description": "incr/decr: amount to add/subtract."},
        "min": {"type": "number", "description": "incr/decr lower bound; a result below it is rejected."},
        "max": {"type": "number", "description": "incr upper bound; a result above it is rejected."},
        "cap": {"type": "number", "description": "reserve ceiling for committed + held + value."},
        "hold_id": {"type": "string", "description": "reserve/confirm/release hold identifier."},
    },
    "required": ["op", "path"],
    "additionalProperties": False,
}

_NESTED_STATE_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": (
        "Optional. List of state actions applied in order. Emit ONLY actions "
        "that genuinely change state — omit entirely (or send []) if nothing changed."
    ),
    "items": _NESTED_ACTION_SCHEMA,
}


class NestedJsonMemory:
    """Nested JSON state with Redux-style action deltas.

    Actions are `NestedDelta{op, path, value}`. Apply is pure (copy-on-write)
    and returns the dotted paths that actually changed.
    """

    name = "nested"
    prompt_file = "executor.yaml"

    def __init__(self, initial: Any = None) -> None:
        self._state: dict = {}
        self.load(initial)

    # --- I/O ---

    def snapshot(self) -> dict:
        # Deep-copy so callers can't mutate our state through the snapshot.
        return copy.deepcopy(self._state)

    def serialize(self) -> str:
        return json.dumps(self._state)

    def load(self, state: Any) -> None:
        if state is None or state == "":
            self._state = {}
            return
        if isinstance(state, dict):
            self._state = copy.deepcopy(state)
            return
        if isinstance(state, str):
            try:
                parsed = json.loads(state)
            except (json.JSONDecodeError, ValueError):
                # Free-text initial_state — not meaningful in nested mode; start fresh.
                self._state = {}
                return
            self._state = parsed if isinstance(parsed, dict) else {}
            return
        self._state = {}

    def set_full(self, serialized: str) -> None:
        # Nested-only deltas: a full-replacement update isn't supported because
        # it bypasses the action model. Fall back to load() which accepts JSON.
        self.load(serialized)

    def clone(self) -> "NestedJsonMemory":
        return NestedJsonMemory(initial=copy.deepcopy(self._state))

    # --- Apply (immutable) ---

    def apply(self, deltas: list) -> list[str]:
        if not deltas:
            return []
        new_state = copy.deepcopy(self._state)
        changed: list[str] = []
        for d in deltas:
            new_state, did_change = _apply_nested(new_state, d)
            if did_change:
                changed.append(d.path)
        self._state = new_state
        return changed

    # --- Delta parsing + schema ---

    def parse_delta(self, raw: dict) -> Optional[NestedDelta]:
        if not isinstance(raw, dict):
            return None
        op = raw.get("op")
        path = raw.get("path")
        if op not in ("set", "merge", "delete", "append") and op not in GUARDED_OPS:
            return None
        if not isinstance(path, str):
            return None
        return NestedDelta(
            op=op, path=path, value=raw.get("value"),
            by=raw.get("by"), min=raw.get("min"), max=raw.get("max"),
            cap=raw.get("cap"), hold_id=raw.get("hold_id"),
        )

    def state_update_schema(self) -> dict:
        return copy.deepcopy(_NESTED_STATE_UPDATE_SCHEMA)

    def state_update_is_list(self) -> bool:
        return True

    def make_delta(self, op: str, key: str, value: Any = None) -> NestedDelta:
        """Construct a backend-native delta. `key` is interpreted as the
        dotted path (a single top-level segment is the common case for
        runtime-built writes like knowledge-gap tracking and the sink shim)."""
        return NestedDelta(op=op, path=key, value=value)


def _split_path(path: str) -> list[str]:
    """Split a dotted path into segments. Empty string → []."""
    if not path:
        return []
    return path.split(".")


def _apply_nested(state: dict, delta: NestedDelta) -> tuple[dict, bool]:
    """Apply one nested delta to a state dict and return (new_state, changed).

    Mutates `state` in place (callers pass a deep-copy). `changed` is True iff
    the operation actually altered the tree (no-op writes return False).
    """
    segments = _split_path(delta.path)
    op = delta.op

    # Root-level operations.
    if not segments:
        if op == "merge" and isinstance(delta.value, dict):
            before = copy.deepcopy(state)
            _deep_merge(state, delta.value)
            return state, state != before
        if op == "set" and isinstance(delta.value, dict):
            if state == delta.value:
                return state, False
            state.clear()
            state.update(delta.value)
            return state, True
        # set/merge non-dict at root or delete root: refuse — keeps invariant
        # that the root is always a dict.
        return state, False

    # Walk to the parent of the leaf, creating dicts as needed for write ops.
    parent: Any = state
    creating = op in ("set", "merge", "append") or op in GUARDED_OPS
    for seg in segments[:-1]:
        if not isinstance(parent, dict):
            return state, False
        if seg not in parent or not isinstance(parent[seg], dict):
            if not creating:
                return state, False
            parent[seg] = {}
        parent = parent[seg]

    if not isinstance(parent, dict):
        return state, False
    leaf = segments[-1]

    if op in GUARDED_OPS:
        new, changed = _apply_guarded(parent.get(leaf), delta)
        if changed:
            parent[leaf] = new
        return state, changed

    if op == "set":
        if leaf in parent and parent[leaf] == delta.value:
            return state, False
        parent[leaf] = delta.value
        return state, True

    if op == "delete":
        if leaf not in parent:
            return state, False
        del parent[leaf]
        return state, True

    if op == "merge":
        if not isinstance(delta.value, dict):
            return state, False
        existing = parent.get(leaf)
        if not isinstance(existing, dict):
            # If the leaf is missing or not a dict, merge degenerates to set.
            if existing == delta.value:
                return state, False
            parent[leaf] = copy.deepcopy(delta.value)
            return state, True
        before = copy.deepcopy(existing)
        _deep_merge(existing, delta.value)
        return state, existing != before

    if op == "append":
        existing = parent.get(leaf)
        if existing is None:
            parent[leaf] = [delta.value]
            return state, True
        if not isinstance(existing, list):
            parent[leaf] = [existing, delta.value]
            return state, True
        existing.append(delta.value)
        return state, True

    return state, False


def _deep_merge(dest: dict, src: dict) -> None:
    """Recursively merge src into dest. Dict values are merged; others replace."""
    for k, v in src.items():
        if k in dest and isinstance(dest[k], dict) and isinstance(v, dict):
            _deep_merge(dest[k], v)
        else:
            dest[k] = copy.deepcopy(v) if isinstance(v, (dict, list)) else v


# --- Shared coercion (used by FlatKeyValueMemory and external callers) ------

def _coerce_to_dict(s: Any) -> Any:
    """Return state as dict if possible, otherwise the raw string (or {} if empty).

    Kept compatible with the pre-refactor `_coerce_state` helper so prompt
    rendering, snapshots, and the sink shim continue to see the same shape.
    """
    if isinstance(s, dict):
        return s
    if not s:
        return {}
    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return s


# --- Factory -----------------------------------------------------------------

_REGISTRY: dict[str, type] = {
    "flat": FlatKeyValueMemory,
    "nested": NestedJsonMemory,
}


def make_backend(name: str, initial: Any = None) -> MemoryBackend:
    """Build a memory backend by name. Defaults to `flat` on unknown names."""
    cls = _REGISTRY.get(name, FlatKeyValueMemory)
    return cls(initial=initial) if initial is not None else cls()
