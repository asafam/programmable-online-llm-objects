# Shared / Concurrent Logic-State in LNL — the Custodian pattern

> How LNL enforces stateful **logic invariants** that span concurrent requests
> and multiple object instances — caps, quotas, rate-limits, running totals,
> shared pools, round-robin queues — without adding runtime locking,
> transactions, or in-object parallelism (all out of scope per
> [`PER_TRACE_PLANS_DESIGN.md`](PER_TRACE_PLANS_DESIGN.md) §5/§8).

This doc has two halves:
1. **The model** — why a single-writer *Custodian* actor is the correct answer,
   and the one discipline that keeps it correct.
2. **Inference** — how the generation pipeline detects such an invariant and
   emits a Custodian object (with a hold/confirm lifecycle and window reset),
   and how the validator stops it from regenerating the race.

---

## 1. The problem — the race is real, *within one object*

A natural first assumption is "the per-object FIFO mailbox serializes everything,
so a cumulative cap on one object is safe." That is **wrong**. The mailbox
serializes message *processing*, not the read-modify-write across a single
plan's *lifetime*.

Verified against the runtime:

- A plan **forks** its working state from master at creation —
  `plan.state = self._state` (`src/lnl/object.py:1035`).
- Deltas **commit** to master only when the plan completes —
  `_auto_close_plan_if_complete` (`src/lnl/object.py:2972-2998`), gated on all
  steps terminal.
- A plan that **suspends** (an async tool dispatch *or* an ask-and-wait to a
  peer) returns early via the pending path (`src/lnl/object.py:1167-1205`,
  `status="pending"`, `finish is None`) — and that path **does not touch master
  state**.
- The `read()` drain loop (`src/lnl/object.py:458-467`) then dequeues the **next**
  message while the first plan is still suspended.

So two plans on the **same** object can both fork the same base, both suspend,
both decide "under cap," and the later commit clobbers the earlier.

Because there is **no arithmetic delta op** — only `set/append/delete/merge`
(`src/lnl/memory.py:156-168`) — a cumulative total is written as an absolute
`set` of an LLM-computed value. So this is a **silent lost update**, not merely
over-approval.

### Worked arithmetic (cumulative $50K discount cap)

Q1=25 commits first → `total=25`. Q2=24 and Q3=10 then arrive together; both
fork base=25:

| Quote | reads base | check | writes |
|------|-----------|-------|--------|
| Q2 | 25 | 25+24=49 ≤ 50 ✓ | `set total=49` |
| Q3 | 25 | 25+10=35 ≤ 50 ✓ | `set total=35` |

Both approved. Recorded `total` = whoever committed last (say 35); **actually
approved = 25+24+10 = 59 > 50**. Cap silently breached *and* the recorded total
understates reality. → **Yes, Q3 is wrongly approved.**

### The race needs a suspension — but real flows have one

A bare read→check→`set` in one turn never suspends: planner-off applies deltas
straight to master in the same message (`src/lnl/object.py:1239-1241`), and a
plan that goes all-terminal in one turn commits that same turn
(`_auto_close_plan_if_complete` reached at `src/lnl/object.py:1271/1283/1313`).
**The race exists only when fork and commit straddle a suspension.**

Real flows have exactly that suspension. See
[`examples/round-robin-lead-assignment.md`](../examples/round-robin-lead-assignment.md):
`LeadAssignment` holds the per-rep counts **and** does an `ask` to
`SalesRepsTable` ("rep in position 1?") mid-decision. Two concurrent leads both
read position-1=Ana and `counts[Ana]`, both assign, both rotate → Ana over-cap,
the queue rotation lost.

---

## 2. The Actor-model answer — a single-writer Custodian

Classic Actor model: there is **no shared mutable memory**; each invariant is
owned by exactly **one** actor; safety comes from that actor's mailbox
serializing messages, **not** from locks. LNL objects already *are* such actors —
the missing piece is giving each invariant a single owner and keeping its
decision atomic.

**Custodian** = a dedicated single-writer object that owns one invariant. Every
mutation is a message to it; its mailbox serializes them. No new runtime
primitive is required — a Custodian is an ordinary LLM-object.

> **Naming.** *Custodian* is the role (single-writer owner of shared state).
> Instances are domain-named (`discount-budget`, `lead-desk`, `reorder-window`).
> The formal pedigree is the DDD *aggregate root* (a single consistency
> boundary). The hold/confirm record the Custodian keeps is its *ledger*.

### 2.1 The atomicity invariant — the decision is one message

The critical decision (read → check → reserve → commit) **must complete inside a
single `process_message` with no step that suspends the plan** — neither an async
tool dispatch nor an ask-and-wait (both hit the pending path,
`src/lnl/object.py:1167-1205`). State it by its *mechanism* (no suspension
between fork and commit), so a reader doesn't add an async tool and silently
reopen the race.

- **Planner caveat (verified).** `enable_planner` is a per-object constructor
  param (`src/lnl/object.py:71,137`) but the Runtime wires one **global** value
  to every object (`src/lnl/runtime.py:491`); object definitions carry no
  per-object flag. So you cannot selectively run just the Custodian planner-off
  today. Within the no-runtime-change constraint the invariant is upheld by
  **design discipline**: keep the Custodian's decision trivially simple so that —
  planner on or off — it is a single all-terminal turn with no suspending step.
- **Optional future structural fix (out of scope).** Thread a per-object
  `enable_planner` from the object definition through `_register_object`
  (`src/lnl/runtime.py:491`) so a Custodian can be declared planner-off and the
  invariant holds by construction rather than by convention.

### 2.2 Reserve → confirm / release (only when earned)

Two lifecycles, chosen by whether a fallible/suspending step follows the check:

- **admit + commit** (one message) — when the decision *is* the commit and
  nothing fallible happens after it. Round-robin assignment is this: pick
  eligible rep, increment count, rotate, done.
- **reserve → confirm / release** (two-phase) — when a suspending or fallible
  step sits *between* the check and the final commit (e.g. a VP `ask` for a
  budget exception, or an external write that can fail):
  1. **reserve** — atomically record a `held` hold against the cap and reply
     `granted{hold_id}`. *This hold is what closes the race:* a concurrent
     request immediately sees reduced headroom.
  2. the slow/fallible work proceeds — it suspends, but no longer holds the
     invariant.
  3. **confirm(hold_id)** → `held`→`committed`; or **release(hold_id)** → drop
     the hold.

Compensation is therefore "release the hold," never "undo an approval." See
[`mediator_actor.md`](mediator_actor.md) for the compensating-transaction sketch
(note: its `src.actors.mediator_actor` import is stale — design sketch, not
shipped code).

**Lifecycle statuses:** `held` → `confirmed` | `released`. For quotes: `held` =
pending VP exception, `confirmed` = approved, `released` = rejected/expired.

### 2.3 Undo / failure — two distinct cases

- **In-flight plan failure — nothing to add.** Uncommitted plan deltas are
  discarded on abandon: `_sweep_stale_plans` (`src/lnl/object.py:2249-2255`)
  drops `accumulated_deltas`; only `complete` plans commit. Built-in rollback of
  local state.
- **Post-commit irreversible effect** (a quote already *approved*) — no built-in
  undo. Use reserve → confirm/release (§2.2) so the irreversible effect happens
  only *after* an already-atomic reservation.

### 2.4 Time windows — prefer lazy reset

The Custodian's state carries the window definition. Two reset styles:

- **Rolling** ("after a week the count resets for entities out of the window"):
  each hold/event carries a `ts`; on every touch, drop entries older than
  `now − span`, evaluated **per partition key**. Matches the existing
  [`inventory.md`](../examples/inventory.md) "age out entries older than 7 days".
- **Periodic** (daily/quarter): store `window_start`/period-id; on touch, if
  `period(now) ≠ stored`, zero that entry first.

**Prefer lazy/on-touch reset** — no scheduler, no missed-tick drift. Eager reset
via a scheduled "day-start" tick is the alternative (simpler to read; needs a
heartbeat and can drift).

### 2.5 Partitioning & shardability

A per-person / per-SKU counter is a **partitioned** invariant. Partitioning
reduces contention (only same-key requests contend) but does **not** remove the
hazard on a hot key. The rule:

> A partitioned invariant can be sharded into one Custodian-per-key **iff there
> is no cross-partition shared state.**

- **Inventory (per-SKU)** passes the test — SKUs are independent. Either shape
  works: one Custodian holding a `{sku: …}` dict, or one
  `reorder-window-{sku}` Custodian per SKU spawned from a class
  (`src/lnl/runtime.py:416-437`, `register_class`/`spawn`), which lets different
  SKUs run in parallel.
- **Round-robin (per-rep)** fails the test — the rotation *queue* couples all
  reps. Even leads to different reps contend on it, so the assignment must stay a
  **single** Custodian (`lead-desk`) owning both the queue and the counts.

Same axis as the cross-instance case: a *shared* pool (budget, queue) needs
**one** owner; a *cleanly partitioned* count can be one owner with a dict, or
many sharded owners.

### 2.6 Canonical Custodian state shape

```jsonc
{
  "window": { "kind": "rolling", "span": "7d" },     // or {kind:"period", unit:"day|week|quarter"}
  "entries": {
    "<partition-key>": {           // per-rep, per-SKU, or "__global__"
      "committed": 0,              // count or running total
      "holds": [ { "hold_id": "...", "amount": 0, "ts": "...", "status": "held" } ],
      "window_start": "..."        // ts or period-id
    }
  }
}
```

---

## 3. Inference — auto-introducing a Custodian during generation

Object decomposition is a single LLM "architect" step,
[`identify_objects.yaml`](../config/prompts/data-gen/identify_objects.yaml)
(stage 1b of `src/data/generate_workflows.py`). Today it factors out service
objects and business-logic objects but has **no notion of a shared-state owner**,
so the accumulator gets lumped into the business-logic object's free-text
`state_description` — exactly what reproduces the race (the decider and the
invariant-holder are the same object, free to suspend mid-decision).

### 3.1 Detection — when to factor out a Custodian

Signals in the workflow rule (the architect should look for these):

- a constraint over an **accumulator across requests** — "cumulative … cannot
  exceed", "running total", a cap/quota/budget/pool;
- a **rate-limit** — "no more than N per {day|week}", "rolling N-day window";
- a **shared allocated resource** — round-robin queue, seat/slot pool;
- a **cross-instance** invariant — many spawned entities sharing one pool;
- explicit period/reset language — "per day", "reset at the start of each …".

**Negative rule:** do *not* introduce a Custodian for purely local,
single-request decisions (the decision reads only the current message). Avoid
over-engineering.

### 3.2 What the architect emits

A Custodian object (ordinary `ObjectDefinition` fields — `object_id`, `role`,
`state_description`, `behavior`, `peers`):

- **role** — declares it the **single writer/owner** of the named invariant.
- **state_description** — the structured shape of §2.6 (entries, holds, window).
- **behavior** — the atomic protocol (admit+commit, or reserve→confirm/release)
  written as a **single-message, no-`ask` critical section**, plus the
  window-reset rule (lazy by default).
- and the **wiring rewrite**: requesters point *to* the Custodian and keep **no
  copy** of the accumulator.

```
Before:  lead-assignment            (decides AND owns counts+queue)
After:   lead-assignment  --admit?-->        lead-desk (Custodian: single writer of counts+queue)
         lead-desk        --granted/denied--> lead-assignment
         lead-assignment  --notify (tell, after commit)--> slack-write
```

### 3.3 Enforcement — three generation-side touchpoints (no runtime change)

1. **Prompt** — `identify_objects.yaml` gains a Custodian checklist item +
   field guidance + a validation-checklist line.
2. **Exemplar** — the worked example
   [`examples/custodian-shared-state.md`](../examples/custodian-shared-state.md)
   doubles as the few-shot template for the structured state + lifecycle +
   window block.
3. **Validation** — `validate_workflow_objects` enforces the invariant:
   - *Deterministic* (`src/data/validate_workflow_objects.py`,
     `_health_check_object`): scan the workflow steps for invariant signals; if
     present, flag when no single-writer owner exists, and check that a present
     Custodian is reachable (has inbound requester peers).
   - *LLM judge* (`validate_workflow_objects.yaml`): grade whether the shared
     invariant is owned by exactly one object, whether its decision avoids
     suspension (no ask-and-wait between check and commit), whether it carries
     the hold/confirm lifecycle + window reset where needed, and whether
     requesters duplicate the accumulator.

This is the key shift: the atomicity rule moves from "discipline a human
remembers" to "an invariant the generator emits and the validator enforces."

---

## Non-goals

No runtime locking, transactions, rollback primitives, in-object parallelism, or
per-object `enable_planner` wiring — consistent with `PER_TRACE_PLANS_DESIGN.md`.
The Custodian pattern needs none of them; it is built entirely from existing
runtime mechanics.
