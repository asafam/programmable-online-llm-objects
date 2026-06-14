"""Unified mutable state — one class for private, shared, and plan-dirty state.

A ``State`` wraps a concrete memory backend (``NestedJsonMemory`` /
``FlatKeyValueMemory``) behind a lock. The three usages are the *same class*,
just different instantiations:

- **private** — the object's own master state (object-local). An active plan's
  working copy derives from it via :meth:`clone`/:meth:`derive`.
- **shared** — registered in the ``SharedStateRegistry``; other objects may read
  it (``shared=True``). Only the owner writes it.
- **dirty** — an active plan's working copy: it derives from the private State,
  accumulates deltas during the plan, and is committed back to master on
  completion.

The only real difference is *accessibility* (who may read/write), enforced at the
tool/registry layer — not here. The lock makes a State safe under async tool
dispatch, where a write can run on a pool thread rather than the object's drain.

``State`` is protocol-compatible with ``MemoryBackend`` (``serialize``/
``snapshot``/``apply``/``make_delta``/``parse_delta``/``load``/``set_full``/
``clone``/``name``), so it is a drop-in for the object's ``self._memory``. Two
extra methods serve the new tool path: :meth:`read` (alias for ``snapshot``) and
:meth:`write` (a guarded raw-delta apply returning ``(ok, error)``).
"""
from __future__ import annotations

import json
import threading
from typing import Any, Optional

from .memory import GUARDED_OPS, MemoryBackend, make_backend


class State:
    def __init__(
        self,
        backend: "MemoryBackend | None" = None,
        *,
        backend_name: str = "nested",
        initial: Any = None,
        shared: bool = False,
    ) -> None:
        if backend is None:
            backend = make_backend(backend_name, initial) if initial is not None else make_backend(backend_name)
        self._backend = backend
        self.shared = shared
        self._lock = threading.Lock()

    # --- MemoryBackend protocol passthrough (drop-in for self._memory) --------
    @property
    def name(self) -> str:
        return self._backend.name

    @property
    def prompt_file(self) -> str:
        return self._backend.prompt_file

    @property
    def backend(self) -> "MemoryBackend":
        return self._backend

    def snapshot(self) -> dict:
        with self._lock:
            return self._backend.snapshot()

    def read(self) -> dict:
        """Alias for :meth:`snapshot` — the tool-facing read."""
        return self.snapshot()

    def serialize(self) -> str:
        with self._lock:
            return self._backend.serialize()

    def load(self, state: Any) -> None:
        with self._lock:
            self._backend.load(state)

    def set_full(self, serialized: str) -> None:
        with self._lock:
            self._backend.set_full(serialized)

    def apply(self, deltas: list) -> list[str]:
        """Apply already-parsed deltas; returns changed keys/paths.

        Backend-compatible: the object's existing private-state path passes
        parsed ``StateDelta``/``NestedDelta`` objects here."""
        with self._lock:
            return self._backend.apply(deltas)

    def parse_delta(self, raw: dict):
        return self._backend.parse_delta(raw)

    def make_delta(self, op: str, key: str, value: Any = None):
        return self._backend.make_delta(op, key, value)

    def state_update_schema(self) -> dict:
        return self._backend.state_update_schema()

    def state_update_is_list(self) -> bool:
        return self._backend.state_update_is_list()

    # --- Instantiations: dirty copies that derive from a private State --------
    def clone(self) -> "State":
        """A working ('dirty') copy that derives from this State."""
        return State(backend=self._backend.clone(), shared=self.shared)

    def derive(self, initial: Any) -> "State":
        """A sibling State of the same backend type, seeded from ``initial`` —
        e.g. a plan's serialized dirty state at a delta-apply site."""
        return State(backend_name=self._backend.name, initial=initial if initial is not None else "")

    # --- Tool-facing guarded write -------------------------------------------
    def write(self, raw: Any) -> tuple[bool, Optional[str]]:
        """Apply one raw delta dict or a list of them under the lock.

        Returns ``(ok, error)``. ``ok`` is False when a delta is malformed or a
        guarded op is rejected (e.g. a ``reserve`` past its ``cap``)."""
        raws = raw if isinstance(raw, list) else [raw]
        if not raws:
            return False, "no delta provided"
        with self._lock:
            for r in raws:
                d = self._backend.parse_delta(r)
                if d is None:
                    return False, f"malformed delta: {json.dumps(r, default=str)}"
                changed = self._backend.apply([d])
                if getattr(d, "op", None) in GUARDED_OPS and not changed:
                    return False, (
                        f"guarded op '{d.op}' at path '{getattr(d, 'path', '')}' was "
                        f"rejected — it would break its bound (cap/min/max) or its "
                        f"hold_id was absent. The state was NOT changed."
                    )
        return True, None
