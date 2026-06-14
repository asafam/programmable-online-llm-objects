"""Per-object shared state: a deterministic store with two tools.

Each LLM-object owns a **shared-state partition** alongside its private state.
The partition is a plain deterministic store — no LLM, no planner, no evaluator —
backed by :class:`NestedJsonMemory` so it reuses the exact same delta parsing and
guarded-op machinery (``incr``/``decr`` bounds, ``reserve``/``confirm``/``release``
against a ``cap``) the private state already uses.

Access model:
- ``set_state`` — the **owner only** writes its own shared partition (the owner's
  LLM produces the delta and calls the tool).
- ``read_state`` — **any** object reads an owner's shared partition (read-only).

Atomicity: each ``set_state`` applies its guarded op under the per-store lock, so
a single write is atomic. The lock is load-bearing for writes too — under async
tool dispatch a tool runs on a pool thread, not the object's drain, so two
in-flight ``set_state`` calls from the same owner can be concurrent. It also makes
a cross-object ``read_state`` consistent against an in-flight write. What the lock
does NOT cover is a ``read_state`` → compute → ``set_state(set, …)`` round-trip:
that read-modify-write is not atomic across LLM steps, so invariant-bearing
mutations MUST use guarded ops (``incr`` with ``max``, ``reserve``/``confirm``).
The guarded ops are what keep invariants correct without an LLM.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .memory import make_backend
from .state import State
from .tools import ToolSpec
from .types import ToolCall, ToolResult


class SharedStateRegistry:
    """Maps owner object-id → its shared :class:`~src.lnl.state.State`.

    A shared State is just an ordinary State with ``shared=True`` — same class as
    the object's private and plan-dirty state, differing only in accessibility
    (others may read it; only the owner writes it)."""

    def __init__(self, backend_name: str = "nested") -> None:
        import threading
        # Shared State uses the SAME backend type as the object's private state
        # (global per run), so the delta dialect is identical — flat key-based or
        # nested path-based. Set by the Runtime from its configured backend.
        self._backend_name = backend_name
        self._stores: dict[str, State] = {}
        self._lock = threading.Lock()

    def ensure(self, owner_id: str, initial: Any = None) -> State:
        with self._lock:
            store = self._stores.get(owner_id)
            if store is None:
                store = State(initial=initial, backend_name=self._backend_name, shared=True)
                self._stores[owner_id] = store
            return store

    def get(self, owner_id: str) -> Optional[State]:
        with self._lock:
            return self._stores.get(owner_id)


# --- Tool argument schemas ---------------------------------------------------

_READ_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "owner": {
            "type": "string",
            "description": "Object id whose shared state to read. Defaults to yourself.",
        },
    },
    "required": [],
    "additionalProperties": False,
}


def _single_action_schema(backend_name: str) -> dict[str, Any]:
    """The backend's single-delta schema (op/path|key/value/guarded params)."""
    backend = make_backend(backend_name)
    schema = backend.state_update_schema()
    # nested: state_update is a list → the single action is schema["items"];
    # flat: state_update is already the single-action object schema.
    return schema.get("items", schema) if backend.state_update_is_list() else schema


def build_set_state_spec(backend_name: str = "nested") -> ToolSpec:
    """Build the set_state ToolSpec so its delta shape matches the object's own
    backend dialect (flat key-based or nested path-based)."""
    action = _single_action_schema(backend_name)
    schema = {
        "type": "object",
        "description": (
            "Mutate YOUR OWN shared state with one delta (inline, the same shape as "
            "your private state_update) or a batch via 'deltas'. Other objects can "
            "READ your shared state but only you write it. For invariants use GUARDED "
            "ops (incr with max, reserve+confirm against cap) — never read-modify-write."
        ),
        "properties": {
            **(action.get("properties", {})),
            "deltas": {
                "type": "array",
                "items": action,
                "description": "Optional batch applied in order; when present, the inline delta is ignored.",
            },
        },
        "required": [],
        "additionalProperties": False,
    }
    return ToolSpec(description=schema["description"], arguments_schema=schema)


class ReadStateExecutor:
    """`read_state(owner?)` — return an owner's shared state (read-only, any caller)."""

    SPEC = ToolSpec(
        description=(
            "Read the shared state of an object. Pass `owner` to read a peer's shared "
            "state; omit it to read your own. Returns the current shared-state JSON."
        ),
        arguments_schema=_READ_STATE_SCHEMA,
    )

    def __init__(self, registry: SharedStateRegistry) -> None:
        self._registry = registry

    def execute(self, call: ToolCall, context: dict[str, Any]) -> ToolResult:
        args = call.arguments or {}
        owner = args.get("owner") or context.get("self_id")
        if not owner:
            return ToolResult(id=call.id, output="", error="no owner to read (missing self_id)")
        store = self._registry.get(owner)
        if store is None:
            return ToolResult(
                id=call.id, output="",
                error=f"unknown object '{owner}' — it has no shared state.",
            )
        return ToolResult(id=call.id, output=json.dumps(store.read(), indent=2))


class SetStateExecutor:
    """`set_state(delta)` — write the CALLER's own shared state (owner-only).

    The schema is built per-Runtime from the configured backend via
    :func:`build_set_state_spec`; this default (nested) is a fallback for direct
    construction in tests."""

    SPEC = build_set_state_spec("nested")

    def __init__(self, registry: SharedStateRegistry) -> None:
        self._registry = registry

    def execute(self, call: ToolCall, context: dict[str, Any]) -> ToolResult:
        self_id = context.get("self_id")
        if not self_id:
            return ToolResult(id=call.id, output="", error="no self_id — cannot write shared state")
        args = dict(call.arguments or {})

        owner = args.pop("owner", None)
        if owner is not None and owner != self_id:
            return ToolResult(
                id=call.id, output="",
                error=(f"you can only write your OWN shared state, not '{owner}'s. "
                       f"Ask that object to update its shared state instead."),
            )

        batch = args.pop("deltas", None)
        if batch:
            raws: Any = batch
        elif args.get("op"):
            raws = [args]
        else:
            return ToolResult(id=call.id, output="", error="no delta provided (need 'op'/'path' or 'deltas')")

        store = self._registry.ensure(self_id)
        ok, err = store.write(raws)
        if not ok:
            return ToolResult(id=call.id, output="", error=err or "shared-state update rejected")
        return ToolResult(
            id=call.id,
            output="Shared state updated. Current shared state:\n" + json.dumps(store.read(), indent=2),
        )
