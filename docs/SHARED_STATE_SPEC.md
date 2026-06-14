# Shared State — Implementation Spec

> Shared state is a **deterministic per-object store** — NO LLM, NO planner, NO
> evaluator. Every LLM-object owns a shared-state partition alongside its private
> state, exposed through two built-in tools (`read_state`, `set_state`) that are
> registered on every object the same way `create_object` is. The store reuses the
> existing guarded-op machinery in `src/lnl/memory.py`.
>
> Source of truth: [`src/lnl/shared_state.py`](../src/lnl/shared_state.py).
> Conceptual background: [`SHARED_STATE_DESIGN.md`](SHARED_STATE_DESIGN.md).

## 1. Model — per-object store, others read, owner writes

| concept | meaning |
|---|---|
| **partition** | each object owns one shared-state partition, addressed by its object id |
| **owner writes** | only the owning object may mutate its partition — via `set_state` |
| **anyone reads** | any object may read any object's partition — via `read_state` (read-only) |
| **deterministic** | the store is plain code: no LLM step, no planner, no evaluator, no read fast-path |

Shared state is **not** an LLM-object and has no message-bus presence of its own.
The owner's LLM produces a delta and calls `set_state`; the store applies it
deterministically. Private state is unchanged — it still flows through the
executor's `updated_state` delta path. `set_state` is the **sole** writer of
shared state.

### Private vs shared — where a value belongs

This is the rule that decides which store to use, and it is about **concurrency**:

- **Shared state** writes land on the **live** store, atomically, under a lock —
  so writes from concurrent cascades **compose** (two `incr`s both apply; two
  per-item `set`s both persist). This is why counters, quotas, running totals,
  rate-limits, and registries written by overlapping requests belong here, with
  guarded ops (`incr`/`decr` bounds, `reserve`/`confirm`/`release` against a `cap`).
- **Private state** is **snapshot-isolated per cascade**: a plan copies master at
  creation, mutates its own copy, and on success the harness **copies that copy
  back over master (last-writer-wins)** — see `Plan.state` and the commit in
  `object.py`. Two concurrent cascades that both update private state will clobber
  each other; the later commit wins wholesale. Private state is for a single
  request's own working facts.

Why not "merge" private state on commit instead of copy? Because the correct
merge is **per-field semantic** — `status` wants last-writer-wins, a counter
wants `+delta`, a quota wants `−delta` — and a deterministic harness can't infer
which from two snapshots ("5 became 8" is ambiguous between "set 8" and "+3").
That semantic knowledge lives in exactly two places: the **guarded ops** of
shared state, or the **LLM's own deltas**. So: if a value needs to stay correct
while multiple cascades touch it, it is shared state — by definition. The harness
commit stays a plain copy.

## 2. Storage (`src/lnl/state.py`, `src/lnl/shared_state.py`)

- **`State`** (`src/lnl/state.py`) — the single state class, instantiated three
  ways (private, shared, plan-dirty); they differ only in *accessibility*. A
  shared partition is just `State(shared=True)` — a memory backend behind a
  `threading.Lock`, the same class that backs the object's private state and an
  active plan's dirty working copy.
  - `read()` → a deep-copied JSON snapshot (safe to read while a write is in flight).
  - `write(delta | [deltas])` → applies under the lock; returns `(ok, error)`.
    `ok` is False when a delta is malformed or a guarded op is rejected (e.g. a
    `reserve` past its `cap`). It shares the exact delta parsing and guarded ops
    the private state already uses (same backend).
- **`SharedStateRegistry`** (`src/lnl/shared_state.py`) — maps owner object-id →
  its shared `State`. One registry per `Runtime`, created in `runtime.py.__init__`;
  each object's partition is provisioned in `_register_object` via
  `registry.ensure(object_id, definition.shared_state or None)`.

## 3. Initial partition — the `## Shared State` markdown section

An object's markdown may declare an initial shared partition with a
`## Shared State` section. The parser (`src/lnl/parser.py`) stores it on
`ObjectDefinition.shared_state` (`src/lnl/types.py`), and the runtime seeds the
store with it. The section body must be **valid JSON** (it is loaded by
`NestedJsonMemory.load`, which drops free-text); omit the section to start empty.

```markdown
## Shared State

{ "budget": { "committed": 0, "holds": [], "cap": 50000 } }
```

The executor prompt surfaces the object's own shared state **read-only** through a
`{shared_state}` block (`config/prompts/lnl/executor.yaml`). That block is context
only — it is NOT changed by the executor's `state_update`; the only way to mutate
shared state is the `set_state` tool.

## 4. The two tools (registered on every object)

### `read_state(owner?)` — `ReadStateExecutor`
Returns an owner's shared-state JSON. `owner` defaults to the caller. **Any**
object may read **any** object's shared state. Unknown owner → error.

### `set_state(delta | deltas)` — `SetStateExecutor`
Applies delta(s) to the **caller's own** store only. A single delta is given
inline (`op`/`path`/…); a batch via `deltas` (applied in order; when present the
inline `op` is ignored). Passing `owner` for anyone other than the caller is
rejected — you cannot write another object's store. On success the tool echoes the
updated shared state.

## 5. Delta schema — the nested-backend action schema

`set_state` reuses the nested backend's single-action schema (it imports it from
`NestedJsonMemory().state_update_schema()`), so it exposes the guarded-op params
without duplicating them. Fields:

`op`, `path` (dotted string; `""` = root), `value`, `by`, `min`, `max`, `cap`,
`hold_id`.

| op | semantics | guard (deterministic) | for |
|---|---|---|---|
| `set` / `merge` / `delete` / `append` | plain structural edits | — | non-invariant data |
| `incr` | numeric leaf `+= by` | reject if result < `min` or > `max` | counters, daily caps |
| `decr` | numeric leaf `-= by` | reject if result < `min` (default 0) | releasing capacity |
| `reserve` | append `{hold_id, amount}` to the leaf's `holds` | reject if `committed + Σheld + value > cap` | two-phase admit |
| `confirm` | move `hold_id`'s amount from `holds` → `committed` | reject if `hold_id` absent | finalize |
| `release` | drop `hold_id` from `holds` | — | roll back |

The guarded ops are `incr`, `decr`, `reserve`, `confirm`, `release`
(`GUARDED_OPS` in `memory.py`). A guarded op that would break its bound applies
**nothing** — `apply()` reports it as a rejection, so the invariant holds even if
the LLM miscomputes.

**Targeting.** A guarded op must address a **non-root leaf** `path` — root
(`path: ""`) accepts only `set`/`merge`/`delete`. `incr`/`decr` target a numeric
leaf; `reserve`/`confirm`/`release` target a reservation container
`{committed, holds, cap?}` (the `reserve` reads `cap` from the delta, falling back
to the container's stored `cap`, and records each hold as `{hold_id, amount}`).

## 6. Atomicity / correctness (load-bearing)

An object's drain is single-threaded (FIFO mailbox), so its own `set_state`
writes are serialized — there is **no concurrent writer** to its partition. The
per-store lock only makes a cross-object `read_state` consistent against the
owner's in-flight write.

Invariant-bearing mutations **MUST** use guarded ops (`incr` with `max`,
`reserve` + `confirm`). A `read_state()` → compute → `set_state(set, …)`
round-trip is a read-modify-write that is **NOT atomic across LLM steps** (classic
TOCTOU) and must not be used for invariants: another object's write — or the
owner's own later message — can land between the read and the set, so the
LLM-computed absolute value silently clobbers it. The guarded ops are what keep
invariants correct without an LLM: they read-check-write in one deterministic
`write()` under the lock.

## 7. File map

| file | role |
|---|---|
| `src/lnl/state.py` | `State` — the one class for private/shared/plan-dirty state (`read`/`write`/`apply`/`derive`/`clone`) |
| `src/lnl/shared_state.py` | `SharedStateRegistry`, `read_state`/`set_state` executors |
| `src/lnl/memory.py` | `NestedJsonMemory` + guarded ops (`GUARDED_OPS`) reused by `State` |
| `src/lnl/runtime.py` | one registry per Runtime; registers the two tools; provisions each partition |
| `src/lnl/parser.py` / `src/lnl/types.py` | parse `## Shared State` → `ObjectDefinition.shared_state` |
| `config/prompts/lnl/executor.yaml` | surfaces the owner's own shared state read-only (`{shared_state}`) |
