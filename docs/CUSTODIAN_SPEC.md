# Custodian — Implementation Spec

> A **Custodian** is **not** a new runtime type. It is an *ordinary* LLM-object
> made correct by two generic things: (1) **guarded state ops** that enforce an
> invariant deterministically, and (2) a **peerless** definition that makes its
> decision atomic. No special `kind`, no dedicated handler, no custodian prompt.
>
> Conceptual background: [`SHARED_STATE_DESIGN.md`](SHARED_STATE_DESIGN.md).

## 1. Principle — correctness from two generic properties, not a special path

Earlier drafts made the Custodian a runtime archetype (a `kind` field + a
single-shot handler + its own prompt). **Rejected** — it special-cases the engine
for a usage pattern. Instead, a Custodian is an ordinary object, and its two
correctness guarantees come from generic mechanisms any object can use:

| guarantee | source (generic, not custodian-specific) |
|---|---|
| **invariant never violated** (cap, count, window) | **guarded state ops** in `memory.py` reject out-of-bound mutations in code |
| **decision is atomic** (no interleaving, no lost update) | the object has **no peers and no async tools** → nothing to `ask` → it cannot suspend → commits in one turn; the serial mailbox (`object.py:458-467`) orders concurrent requests |

That's the whole design. Everything below is those two pieces plus the
generation/validation that produces such an object.

## 2. Why peerless ⇒ atomic (no special handling needed)

The race needs a **suspension** between read and commit — the pending path
(`object.py:1167-1205`), which fires only on an outbound `ask`-and-wait or an
async tool dispatch. An object with **no peers and no tools** has nothing to
suspend on:

- planner **off** → deltas apply straight to master in the same message
  (`object.py:1239-1241`).
- planner **on** → a plan whose steps are all `reason`/`finish` (no `ask`/`tool`)
  goes all-terminal in one turn and commits that same turn
  (`_auto_close_plan_if_complete`, reached at `object.py:1271/1283/1313`).

Either way the decision starts and commits inside one `process_message`, with no
other message interleaving. So we need **no** `kind`, **no** `_process_custodian`
path, and **no** per-object `enable_planner` flag. The only definitional
requirement is: **empty `peers`** (and no async tools).

Cap-of-1 proof (A `reserve`, B `reserve`, C read): mailbox serializes A→B; A's
turn commits before it returns; B reads committed state and its `reserve` guard
rejects; C reads the committed value. No fork-across-suspension → no race.
(Full counterfactual in `SHARED_STATE_DESIGN.md` §1.)

## 3. The one real change — guarded state ops (`src/lnl/memory.py`)

Today's ops are `set/delete/append` (flat, `memory.py:156-168`) / `+merge`
(nested) — **no arithmetic, no guards**, so a cap is an absolute `set` of an
LLM-computed number. We add **guarded ops** so the invariant is enforced by code.
These are **generic** (any object may use them) and are surfaced to every
object's executor prompt automatically via the backend's `state_update_schema()`.

### 3.1 New ops (extend `StateDelta`, `types.py:319`, with optional params)
```
StateDelta(op, key, value=None, by=None, min=None, max=None, cap=None,
           hold_id=None, ts=None, span=None)
```

| op | semantics | guard (deterministic) | for |
|---|---|---|---|
| `incr` | `state[key] += by` | clamp/no-op if result < `min` or > `max` | counters, daily caps |
| `decr` | `state[key] -= by` | no-op if result < `min` (default 0) | releasing capacity |
| `reserve` | append `{hold_id, amount}` to `key.holds` | no-op if `committed + Σheld + amount > cap` | two-phase admit |
| `confirm` | move `hold_id`: holds → `committed` | no-op if `hold_id` absent | finalize |
| `release` | drop `hold_id` from holds | — | roll back |
| `window_append` | append `{value, ts}`; **evict** entries with `ts < ref − span` | — | rolling rate-limits |

- **Self-guarding**: a guarded op that would violate its bound applies **nothing**
  for that key (the invariant holds even if the LLM miscomputes). Because the turn
  is atomic, the LLM read of current state is accurate, so its `granted`/`denied`
  reply is reliable; the guard is the deterministic backstop.
- `window_append` eviction reference time = the **incoming message timestamp**
  (per `object.yaml`'s "use the payload timestamp") — passed into the op; the
  apply path has no ambient clock.

### 3.2 Plumbing (additive — existing `set/delete/append` untouched)
- `_apply_flat_delta` (`memory.py:156`) + the nested apply: add the new op cases.
- `parse_delta` + `state_update_schema` in **both** backends: accept the new op
  params so the LLM can emit them and they round-trip.
- `apply()` keeps its signature; guarded ops simply self-enforce during apply.
  (No separate `try_apply_guarded` and no special caller — there is no special
  path.) A no-op'd guarded op returns no changed key, which is how the rest of
  the runtime already represents "nothing changed."

## 4. Signaling (no handler — uses the normal finish)

The object emits its decision on the ordinary `ReactFinish` (`types.py:480`):

- **ASK** (`message.expects_reply`): reply carries the decision. `granted`/read →
  `status="ok"`; `denied` → `finish.status="failed"` with the reason in
  `finish.error`, so the asker's awaiting step reflects "reservation didn't
  happen" via the existing failure-REPLY routing.
- **TELL** (`expects_reply=False`): no reply is awaited; the accept/reject is just
  recorded in state. `status="ok"`.

This is all existing runtime behavior — nothing custodian-specific.

## 5. Generation — emit a peerless owner that uses the guarded ops

- `config/prompts/data-gen/identify_objects.yaml`: when a cross-request /
  cross-instance invariant is detected (cap, quota, rate-limit, running total,
  shared pool, round-robin queue), emit **one owner object** that:
  - has **no peers** (requesters peer *to* it; it only ever **replies**),
  - holds the invariant in its own `## State`,
  - states the rule in `behavior` using the guarded ops (`reserve`/`confirm`/
    `release`, or `incr` with a cap), deciding in a single message.
  Requesters keep **no private copy** of the accumulator. No `kind` tag — the
  owner is an ordinary object distinguished only by "owns the invariant, no peers."

## 6. Validation (`src/data/validate_workflow_objects.py`)

Deterministic, crisp (extends the existing `_custodian_graph_issues`):
1. invariant signals present in steps → **an owner object exists** that holds it
   (detected by role marker "owns"/"single-writer", as today);
2. that owner has **empty `peers`** (a peer means it can suspend mid-decision →
   flag);
3. requesters declare the owner as a peer (it's reachable).

The LLM-judge custodian rubric is dropped — these three structural checks
subsume it.

## 7. File-by-file change list (the whole thing)

| file | change |
|---|---|
| `src/lnl/types.py` | extend `StateDelta` with guarded-op params (`by/min/max/cap/hold_id/ts/span`) |
| `src/lnl/memory.py` | implement guarded ops in both backends' apply + `parse_delta` + `state_update_schema` |
| `config/prompts/data-gen/identify_objects.yaml` | emit a peerless invariant-owner using the guarded ops |
| `src/data/validate_workflow_objects.py` | owner-exists + empty-peers + reachable checks |
| `tests/` | guarded-op unit tests + a MockBrain cap-of-1 runtime test |

No `kind`, no `parser.py`, no `object.py`/`runtime.py` routing, no `brain.py`, no
`custodian.yaml`. A Custodian is an ordinary object + generic guarded ops +
empty peers.

## 8. Decisions (resolved)

1. **Validate depth** → deterministic op-guard only (it lives in the guarded op).
2. **Guarded ops** → in shared `memory.py`, additive to `apply`; existing ops
   untouched.
3. **Signaling** → ASK replies (granted=`ok`, denied=`failed`+reason); TELL emits
   no reply, records state only. Via the normal finish — no handler.
4. **No special custodian kind** → there is no `kind` field and no dedicated
   runtime path. A Custodian is an ordinary object made correct by guarded ops +
   empty peers.

## 9. Test plan

- **Unit (no API):** the guarded ops — `incr` clamps at `max`/`min`;
  `reserve` rejects past `cap`, `confirm`/`release` move/return held amounts;
  `window_append` evicts entries older than `ref − span`. Plus `parse_delta`
  round-trips the new op params.
- **Runtime (MockBrain):** the cap-of-1 simulation — A `reserve`→granted, B
  `reserve`→denied (`status="failed"` on the ASK), C read→committed value; assert
  the owner emits **no** outgoing messages and never returns `status="pending"`.
- **Generation:** regenerate `round-robin-lead-assignment`; assert the
  invariant-owner has **empty peers** and the deterministic validator is clean;
  assert an owner *with* peers is flagged.
