# Shared / Concurrent Logic-State in LNL — the shared-state store

> How LNL enforces stateful **logic invariants** that span concurrent requests
> and multiple object instances — caps, quotas, rate-limits, running totals,
> shared pools, round-robin queues — without adding runtime locking,
> transactions, or in-object parallelism (all out of scope per
> [`PER_TRACE_PLANS_DESIGN.md`](PER_TRACE_PLANS_DESIGN.md) §5/§8).

The answer is a **deterministic per-object shared-state store** (no LLM, no
planner, no evaluator), reached through two built-in tools — `read_state` and
`set_state` — and made correct by **guarded ops**. The implementation spec is
[`SHARED_STATE_SPEC.md`](SHARED_STATE_SPEC.md); this doc is the *why*.

This doc has two halves:
1. **The model** — why a single-writer **shared-state owner** is the correct
   answer, and the one discipline (guarded ops) that keeps it correct.
2. **Inference** — how the generation pipeline detects such an invariant and emits
   a shared-state owner, and how the validator stops it from regenerating the race.

---

## 1. The problem — a naive read-modify-write races

Each object owns a shared-state partition addressed by its id. Any object may
**read** any partition (`read_state`); only the owner may **write** its own
(`set_state`). An object's drain is single-threaded (FIFO mailbox), so the owner's
own writes are serialized — there is no concurrent writer to a given partition.

That serialization alone is **not** sufficient for an invariant. The hazard is a
**read-modify-write split across LLM steps** (classic TOCTOU):

```
read_state()            → reads current total
LLM computes new total  → "under cap, set it to X"
set_state(set, X)       → writes the absolute value the LLM computed
```

Between the read and the set, another object's write — or the owner's own next
message — can change the partition. The LLM-computed absolute `set` then silently
clobbers that change. With an absolute `set` there is no arithmetic guard, so a
cumulative total is written as a number the LLM picked: a **silent lost update**,
not merely over-approval.

### Worked arithmetic (cumulative $50K discount cap)

`total=25` committed first. Two more approvals are decided concurrently, both
reading `total=25` before either writes:

| approval | read_state | LLM check | set_state |
|------|-----------|-------|--------|
| A (+24) | 25 | 25+24=49 ≤ 50 ✓ | `set total=49` |
| B (+10) | 25 | 25+10=35 ≤ 50 ✓ | `set total=35` |

Both approved. Recorded `total` = whoever wrote last (say 35); **actually approved
= 25+24+10 = 59 > 50**. The cap is silently breached *and* the recorded total
understates reality.

**The fix is not "add a lock around the round-trip" — it is to never do the
round-trip.** Replace read → compute → `set` with a single **guarded op** that
read-checks-writes deterministically in one `set_state` call.

---

## 2. The answer — a single-writer shared-state owner + guarded ops

Classic Actor model: there is **no shared mutable memory**; each invariant is
owned by exactly **one** actor; safety comes from that actor serializing its own
writes, **not** from locks. LNL objects already *are* such actors — the missing
piece is giving each invariant a single owner and a deterministic write path.

**Shared-state owner** = a dedicated object that owns one invariant in its
shared-state partition. Requesters either **read** it (`read_state`) or **ask the
owner** to mutate it; only the owner calls `set_state`. No new runtime primitive
is required — the store is plain code reusing `NestedJsonMemory`.

> **Naming.** *Shared-state owner* is the role (single-writer owner of shared
> state). Instances are domain-named (`discount-budget`, `lead-desk`,
> `reorder-window`). The formal pedigree is the DDD *aggregate root* (a single
> consistency boundary). The hold/confirm record the owner keeps is its *ledger*.

### 2.1 The atomicity invariant — guarded ops, not a multi-step round-trip

The owner's drain serializes its writes, so the only remaining hazard is the
TOCTOU split of §1. The discipline is therefore one rule:

> **Invariant-bearing mutations MUST be a single guarded `set_state` op — never a
> `read_state()` → compute → `set_state(set, …)` round-trip.**

A guarded op (`incr` with `max`, `reserve`/`confirm`/`release` against a `cap`)
does its read-check-write inside one deterministic `apply()` under the store's
lock. The check and the write cannot straddle an LLM step, so there is no window
for a lost update — even if the LLM miscomputes, the op applies **nothing** when
it would break its bound (`apply()` reports the rejection). Plain `set`/`merge`
are fine for non-invariant data; they are unsafe **only** as the write half of a
hand-rolled accumulator.

This is purely a property of the deterministic store. There is no planner, no
"peerless object," and no read fast-path involved — the old runtime mechanism (a
peerless LLM-object made atomic plus a deterministic-read shortcut) is **gone**.

### 2.2 Reserve → confirm / release (only when earned)

Two lifecycles, chosen by whether a fallible/slow step follows the check:

- **incr + commit** (one op) — when the decision *is* the commit and nothing
  fallible happens after it. Round-robin assignment is this: pick eligible rep,
  `incr` its count (guarded by the per-day `max`), rotate, done.
- **reserve → confirm / release** (two-phase) — when a fallible or slow step sits
  *between* the check and the final commit (e.g. a VP approval, or an external
  write that can fail):
  1. **reserve** — `set_state(reserve, value, cap, hold_id)` atomically records a
     hold against the cap and returns granted/denied. *This hold is what closes
     the race:* a concurrent requester that reads the partition immediately sees
     reduced headroom.
  2. the slow/fallible work proceeds — it no longer holds the invariant.
  3. **confirm(hold_id)** → held → committed; or **release(hold_id)** → drop the
     hold.

Compensation is therefore "release the hold," never "undo an approval." See
[`mediator_actor.md`](mediator_actor.md) for the compensating-transaction sketch
(note: its `src.actors.mediator_actor` import is stale — design sketch, not
shipped code).

**Lifecycle:** a `reserve` appends a `{hold_id, amount}` hold; `confirm` moves
that amount into `committed` and drops the hold; `release` drops it without
committing. (The state is the hold's *presence*, not a stored `status` field.) For
quotes: holding = pending VP exception, confirmed = approved, released =
rejected/expired.

### 2.3 Undo / failure

- **A reservation that never confirms** — `release(hold_id)` returns the headroom;
  no "undo the approval" needed. Holds that are never confirmed simply sit until
  released (or until a window/period reset clears them — §2.4).
- **Post-commit irreversible effect** (a quote already *approved*) — no built-in
  undo. Use reserve → confirm/release so the irreversible effect happens only
  *after* an already-atomic reservation.

### 2.4 Time windows — lazy reset via plain writes

The window is just data in the partition (`window_start` / a period-id, plus the
per-key counts). There is **no** windowing guarded op — `GUARDED_OPS` is
`incr`/`decr`/`reserve`/`confirm`/`release` only. Reset is the owner's job, done
with ordinary `set`/`merge` deltas **on touch**:

- **Periodic** (daily/quarter): store `window_start`/period-id; when a request
  arrives, if `period(now) ≠ stored`, `set` that entry's count back to 0 (and
  update `window_start`) before applying the guarded `incr`/`reserve`.
- **Rolling** ("after a week the count resets for entities out of the window"):
  keep per-entry timestamps and, on touch, `set` the entry to only those still
  inside `now − span`.

Because the reset and the guarded mutation are two ops in the **same**
`set_state` batch (or two consecutive messages to the single-writer owner, with no
concurrent writer), the count the guard checks is the freshly-reset one. Prefer
lazy/on-touch reset — no scheduler, no missed-tick drift.

### 2.5 Partitioning & shardability

A per-person / per-SKU counter is a **partitioned** invariant. Partitioning
reduces contention (only same-key requests contend) but does **not** remove the
TOCTOU hazard on a hot key — which is why same-key mutations still go through a
guarded op. The rule:

> A partitioned invariant can be sharded into one owner-per-key **iff there is no
> cross-partition shared state.**

- **Inventory (per-SKU)** passes the test — SKUs are independent. Either shape
  works: one owner holding a `{sku: …}` dict in its partition, or one
  `reorder-window-{sku}` owner per SKU spawned from a class
  (`src/lnl/runtime.py`, `register_class`/`spawn`), each owning its own partition.
- **Round-robin (per-rep)** fails the test — the rotation *queue* couples all
  reps. Even leads to different reps mutate it, so the assignment must stay a
  **single** owner (`lead-desk`) owning both the queue and the counts.

### 2.6 Canonical shared-state shape

```jsonc
{
  "window": { "kind": "rolling", "span": "7d" },   // or {kind:"period", unit:"day|week|quarter"}
  "entries": {
    "<partition-key>": {           // per-rep, per-SKU, or "__global__"; the reserve/confirm leaf
      "committed": 0,              // count or running total
      "holds": [ { "hold_id": "...", "amount": 0 } ],   // appended by reserve
      "cap": 50000,                // optional stored bound (reserve falls back to it)
      "window_start": "..."        // ts or period-id, for lazy reset
    }
  }
}
```

This is the JSON an object declares in its `## Shared State` section (see
[`SHARED_STATE_SPEC.md`](SHARED_STATE_SPEC.md) §3) and that `read_state` returns.

---

## 3. Inference — auto-introducing a shared-state owner during generation

Object decomposition is a single LLM "architect" step,
[`identify_objects.yaml`](../config/prompts/data-gen/identify_objects.yaml)
(stage 1b of `src/data/generate_workflows.py`). Without a notion of a shared-state
owner, the accumulator gets lumped into a business-logic object's free-text
`state_description` — exactly what invites the read-modify-write race (the decider
hand-rolls the accumulator instead of using a guarded op on a single owner).

### 3.1 Detection — when to factor out a shared-state owner

Signals in the workflow rule (the architect should look for these):

- a constraint over an **accumulator across requests** — "cumulative … cannot
  exceed", "running total", a cap/quota/budget/pool;
- a **rate-limit** — "no more than N per {day|week}", "rolling N-day window";
- a **shared allocated resource** — round-robin queue, seat/slot pool;
- a **cross-instance** invariant — many spawned entities sharing one pool;
- explicit period/reset language — "per day", "reset at the start of each …".

**Negative rule:** do *not* introduce a shared-state owner for purely local,
single-request decisions (the decision reads only the current message). Avoid
over-engineering.

### 3.2 What the architect emits

A shared-state owner object (ordinary `ObjectDefinition` fields — `object_id`,
`role`, `state_description`, `behavior`, `peers` — plus the
`owns_shared_state: true` flag; see `src/data/schema.py`):

- **role** — MUST begin with the mandated prefix "Single-writer owner of " naming
  the invariant.
- **shared state** — the structured shape of §2.6 (entries, holds, cap, window),
  declared as the object's `## Shared State` partition.
- **behavior** — mutate the invariant with a **single guarded `set_state` op**
  (`incr` with `max`, or `reserve`/`confirm`/`release` against `cap`), plus the
  lazy window-reset rule. Never read-modify-write.
- and the **wiring rewrite**: requesters point *to* the owner and keep **no copy**
  of the accumulator; read-only consumers use `read_state`.

```
Before:  lead-assignment            (decides AND hand-rolls counts+queue)
After:   lead-assignment  --assign?-->          lead-desk (owns counts+queue in shared state)
         lead-desk        --assigned/hold-->     lead-assignment
         lead-assignment  --notify (tell, after commit)--> slack-write
```

### 3.3 Enforcement — generation-side touchpoints (no runtime change)

1. **Prompt** — `identify_objects.yaml` carries a shared-state-owner checklist
   item + field guidance + a validation-checklist line.
2. **Exemplar** — the worked example [`examples/shared-state.md`](../examples/shared-state.md)
   doubles as the few-shot template for the partition + guarded-op lifecycle.
3. **Validation** — `validate_workflow_objects` (`src/data/validate_workflow_objects.py`)
   enforces the invariant deterministically: detect a shared-state owner by the
   explicit `owns_shared_state` flag (or, for legacy data, the "Single-writer
   owner of " role prefix); when invariant signals appear in the steps, flag if no
   owner holds it; and require each owner to be reachable (have inbound peers).

This is the key shift: the discipline moves from "something a human remembers" to
"an invariant the generator emits and the validator enforces" — backed by the
deterministic guarded ops so it holds at runtime without an LLM.

---

## Non-goals

No runtime locking, transactions, rollback primitives, or in-object parallelism —
consistent with `PER_TRACE_PLANS_DESIGN.md`. The shared-state store needs none of
them: correctness comes from one single-writer owner per invariant plus the
deterministic guarded ops.
