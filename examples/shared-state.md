# Example: shared state — a single-writer owner + guarded ops

## Problem statement

Some workflow rules constrain an accumulator that spans many requests: a
cumulative cap, a per-entity rate-limit, a shared pool, a round-robin queue. The
object that *decides* must not hand-roll that accumulator with a
read-modify-write, because the read and the write straddle an LLM step and a
concurrent change can be silently lost. This example shows that race and the
**shared-state** fix: one object **owns** the invariant in its shared-state
partition and mutates it with a single **guarded op** via `set_state`; everyone
else **reads** it with `read_state`.

See [`docs/SHARED_STATE_DESIGN.md`](../docs/SHARED_STATE_DESIGN.md) for the model
and [`docs/SHARED_STATE_SPEC.md`](../docs/SHARED_STATE_SPEC.md) for the store.

## The race (why a hand-rolled accumulator is not enough)

An object's drain is single-threaded, so its own writes are serialized — but that
does not protect a `read_state()` → compute → `set_state(set, …)` round-trip. The
read and the absolute `set` are two separate LLM steps; a change that lands
between them is clobbered by the LLM-computed value. With an absolute `set` there
is no arithmetic guard, so a cumulative total becomes whatever number the LLM
picked — a silent lost update.

| approval | read_state | LLM check | set_state | recorded | actually approved |
|------|-----------|-------|--------|----------|-------------------|
| A (+24K) | 25K | 25K+24K=49K ≤ 50K | `set total=49000` | 49000 | 49000 |
| B (+10K) | 25K | 25K+10K=35K ≤ 50K | `set total=35000` | **35000** | **59000 ✗** |

Both read base=25K before either wrote, so the cap ($50K) is breached and the
recorded total ($35K) understates reality.

---

## The fix — a `discount-budget` owner with a `## Shared State` partition

`discount-budget` owns the cumulative-discount invariant. It declares the
accumulator as its **shared-state partition** (valid JSON in a `## Shared State`
section) and mutates it only through guarded ops — never a read-modify-write.

```markdown
# Discount Budget

## Role

Single-writer owner of the quarter's cumulative approved-discount budget.

## Behavior

When asked to admit a discount, call `set_state` with a single `reserve` op
against the `budget` container (never read-modify-write):
  { "op": "reserve", "path": "budget", "value": <amount>, "hold_id": "<quote-id>" }
If the tool reports the reserve was REJECTED, reply "denied" (the budget would be
exceeded). Otherwise reply "granted <hold_id>".
When the manager later approves, `confirm` the hold; if rejected, `release` it:
  { "op": "confirm", "path": "budget", "hold_id": "<quote-id>" }
  { "op": "release", "path": "budget", "hold_id": "<quote-id>" }

## Shared State

{ "budget": { "cap": 50000, "committed": 0, "holds": [] } }
```

A second object writes nothing — it only **reads**:

```markdown
# Budget Dashboard

## Role

Reports remaining discount headroom on request.

## Behavior

When asked for current budget usage, call `read_state(owner="discount-budget")`
and report `committed`, the open `holds`, and remaining headroom against `cap`.
```

### Why the guarded op closes the race

`reserve` does its **read-check-write in one deterministic `set_state` call**
under the store's lock — the check and the write cannot straddle an LLM step. It
reads the `cap` from the stored container, appends a `{hold_id, amount}` hold
immediately, and rejects if `committed + Σheld + amount > cap`. So the moment A
reserves 24K, B's `reserve` of 10K sees `25K + 24K + 10K = 59K > cap 50K` and is
**rejected** by code, even if B's LLM miscomputes. No lost update, no breached cap.

```mermaid
sequenceDiagram
  participant ApprovalPolicy
  participant DiscountBudget
  participant Manager

  Note over DiscountBudget: budget: committed=25000, holds=[]

  ApprovalPolicy->>DiscountBudget: admit 24000 (Q2)
  Note over DiscountBudget: set_state reserve(24000, h2) → 25000+24000=49000 ≤ 50000, hold appended
  DiscountBudget-->>ApprovalPolicy: granted h2
  ApprovalPolicy->>Manager: await approval for Q2

  ApprovalPolicy->>DiscountBudget: admit 10000 (Q3)
  Note over DiscountBudget: set_state reserve(10000, h3) → 25000+24000+10000=59000 > 50000, REJECTED
  DiscountBudget-->>ApprovalPolicy: denied; escalate Q3 to VP

  Manager-->>ApprovalPolicy: Q2 approved
  ApprovalPolicy->>DiscountBudget: confirm h2
  Note over DiscountBudget: set_state confirm(h2) → committed=49000, holds=[]
```

If the manager rejects Q2, `ApprovalPolicy` sends `release h2` and the headroom
returns — no "undo the approval" needed.

---

## The simpler shape — `incr` with a `max`

When the decision *is* the commit and nothing fallible follows it, skip the
two-phase reserve/confirm and use a single guarded `incr`. Round-robin per-rep
caps are this case — `lead-desk` owns `{ queue, counts, date }` in its shared
state and, per lead, runs one `set_state` batch: a **lazy daily reset** (only when
the stored `date` is stale) followed by a guarded `incr` of the chosen rep's count
with `max: 2`. If the `incr` is rejected the rep is at cap, so rotate and try the
next. Because the counts and the rotation queue couple all reps, this stays a
**single** owner (not one-per-rep) — see the shardability rule in the design doc.

The reset is conditional: the owner's LLM includes the two reset ops **only on the
first lead of a new day** (otherwise it sends just the `incr`). Setting `date`
alone does not reset the counts — the reset must clear `counts` too, or the
guarded `incr` would reject forever once a rep reaches the cap.

```jsonc
// first lead of a new day — one set_state batch, applied in order under the lock:
[
  { "op": "set",  "path": "date",   "value": "2026-06-14" },  // roll the window…
  { "op": "set",  "path": "counts", "value": {} },            // …and clear the per-rep counts
  { "op": "incr", "path": "counts.ana", "by": 1, "max": 2 }   // guarded — rejected at cap
]
// later leads the same day — just the guarded incr:
// { "op": "incr", "path": "counts.ana", "by": 1, "max": 2 }
```

---

## Partitioned, shardable: per-SKU reorder window

Per-SKU reorder counts ([`inventory.md`](inventory.md)) have **no cross-partition
shared state** — SKUs are independent — so the invariant is shardable. Two valid
shapes:

- **One owner, partitioned dict:** `reorder-window` owns
  `{ "SKU-A": {sent:[…]}, "SKU-B": {…} }` in one partition. Simple.
- **One owner per SKU:** spawn `reorder-window-{sku}` from a class
  (`register_class`/`spawn`), each owning its own partition — safe *only because*
  SKUs share nothing.

The rolling 7-day window is enforced by **lazy reset with plain writes** (there is
no windowing guarded op): each entry stores sent timestamps; on every touch, `set`
the entry to only those inside `now − 7d` before applying the guarded mutation.

| case | shared structure? | shape |
|------|-------------------|-------|
| $50K quarter cap | one global total | one owner, `reserve`/`confirm` |
| round-robin per-rep | the rotation queue | one owner (not shardable), `incr` + `max` |
| per-SKU reorder window | none across SKUs | one owner w/ dict, **or** one per SKU |
| cross-instance Order pool | one shared pool | one owner, all instances reserve |

---

## The rule, in one line

An invariant-bearing mutation is a **single guarded `set_state` op** on the owner
(`incr` with `max`, or `reserve`/`confirm`/`release` against `cap`) — never a
`read_state` → compute → `set_state(set, …)` round-trip. Everyone else uses
`read_state` to observe it.
