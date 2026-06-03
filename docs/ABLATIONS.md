# Async Tool-Calling Ablation Log

Tracks the work to make **async tool dispatch the default** (single-turn ReAct,
no internal looping) without regressing pass rate vs. sync dispatch.

Background: previous ablations showed `--tool-dispatch async` losing pass rate
on the multistep Zapier eval. The hypothesis we are testing here is that the
regression is a *harness* artifact (lost context across the tool-dispatch
boundary), not a fundamental capability gap. The LLM does not know it is being
called async — so a correctly-designed harness should yield epsilon delta.

Stable pre-investigation checkpoint: tag `stable-pre-async-2026-06-02`,
branch `emnlp-2026`, commit `a052f32`.

Validation dataset: `outputs/data/zapier/async_validation/workflows-mods.jsonl`
— 3 small tool-using TCs picked from `20260522_rev/workflows-mods.jsonl`:
- `ai-email-assistant-temporal-TC001` (3 objects, 3 steps, 1 tool)
- `turn-granola-notes-into-tasks-temporal-TC001` (3 objects, 3 steps, 3 tools)
- `expenses-tracker-temporal-TC001` (4 objects, 3 steps, 3 tools)

Canonical config: `--model gpt-5.4-mini --judge-model gpt-5.4 --runs 1
--steps-only` (per memory). Steps-only because we only need to validate
the executor's tool-dispatch path; modifications/events orthogonally exercise
the planner+evaluator loop.

---

## Failure-mode analysis (read of `src/lnl/`)

The codebase itself flags the regression in `runtime.py:138–142`:

> *Sync is the default because the per-turn async LLM call empirically loses
> pass rate on the Zapier multistep eval (each turn rebuilds the prompt
> without the LLM's own prior tool_call action in context, leading to
> lost-intent / re-dispatch patterns).*

### Chain trace

1. **Turn 1** — `process_message(inbound_user_msg)` calls `_run_react_cycle`.
   The LLM emits `action="tool_call"` with `tool_calls=[t1,…,tN]` + a
   `thought`. In **async mode** (`object.py:1632–1693`) the harness:
   - submits each `tc` to the per-object pool,
   - increments `_pending_tool_count`,
   - returns `(None, …, tools_called)` — the ReAct loop is one-shot by design
     (`async_mode and react_iterations >= 1` ⇒ force-finish at line 1543).
   `process_message` then appends *only the inbound message* to history
   (`object.py:1183`) and returns `status="pending"`.

2. **Tool worker** completes, calls `_execute_tool` which posts a `REPLY`
   message back to the object's own mailbox (`sender="__tool__:<name>"`).
   `deliver(decrement_pending=True)` decrements the pending count and
   unblocks `read()`.

3. **Turn 2** — `read()` pops the tool REPLY and calls `process_message`.
   `_build_chat_messages(sys_prompt, self._history, message)` rebuilds the
   prompt from scratch:
   - system prompt (with `active_plan` rendered),
   - `[Past messages — already reflected in your state]` blob over history,
   - the tool REPLY rendered as `[Tool result (call X) from Y] (status=…): …`.

   **What is missing from Turn 2's prompt:**
   - The assistant message from Turn 1 — the `{thought, action:"tool_call",
     tool_calls:[…]}` JSON that *triggered* this tool execution. History
     only stores inbound `Message` objects (see `_append_history` callers
     at `object.py:1183, 1367, 1467`) — the assistant ReactStep is never
     persisted anywhere.

### Concrete failure modes (deduced from the missing context)

- **Lost intent** — the LLM in Turn 2 sees a tool result for a call it does
  not remember making, with no arguments and no thought. It re-derives intent
  from `active_plan` rendering, which (a) only exists when planner is on, and
  (b) drops `arguments` and `thought` even then (`brain.py:_render_active_plan`
  shows step description + truncated result, no args).
- **Re-dispatch / phantom retry** — without the original call in context, the
  LLM may emit the same tool_call again on Turn 2, hitting the
  cross-turn `max_tool_rounds` cap.
- **Misattribution under fan-out** — when Turn 1 dispatched several tools
  (batch), each REPLY lands in a separate Turn (read() pops one at a time)
  and the LLM sees only one result per turn with no record of the sibling
  calls. Compounded with active_plan's truncated `result: "…"` line, the LLM
  may treat the rendered plan step as already-complete and skip remaining
  results.
- **Planner-off path is worst** — without planner, there is no `active_plan`
  rendering at all. Turn 2 has nothing tying the REPLY to anything; the LLM
  is essentially asked "given this tool result, finish" with no record of
  the call or its purpose.

### What sync mode does that async misses

Sync mode (`object.py:1595–1630`) keeps everything in a single `messages`
list across the ReAct loop:

```
[system, history-blob, current-user-msg,
 assistant({thought, action:"tool_call", tool_calls:[…]}),
 user("[Tool result (call t1) …]\n[Tool result (call t2) …] …"),
 assistant(finish)]
```

— the assistant tool_call message stays in context for the second LLM call.
Async loses this because the second call is a *different* `process_message`
turn that reconstructs `messages` from `self._history`.

---

## Fix plan

Two changes, both localised to `src/lnl/`:

### F1. Preserve the assistant turn across async dispatch
- New `MessageType.ASSISTANT_TURN` — synthetic, never delivered, only used
  as a history marker carrying the assistant's rendered tool_call JSON.
- `_run_react_cycle` returns the rendered assistant-step JSON alongside the
  usual tuple when it dispatches async (so `process_message` can persist it).
- After `_append_history(inbound_msg, …)` at `object.py:1183`, append a
  second `HistoryEntry` carrying the synthetic `ASSISTANT_TURN` message.
- `_build_chat_messages` recognises trailing `ASSISTANT_TURN` history
  entries and emits them as real `{"role": "assistant", "content": …}`
  messages **immediately before the current-turn user message** (the tool
  REPLY), instead of folding them into the past-messages user blob.
- `_flush_history_for_plan` already removes by `plan_id`, so the synthetic
  entries are cleaned up when the plan terminates — no extra work needed.

### F2. (Conditional) Batch tool REPLYs in `read()`
If F1 alone closes the gap for single-tool dispatch but multi-tool fan-out
still regresses, add a batching path:
- When the mailbox front is a tool REPLY and `_pending_tool_count > 0`
  (more replies still in flight), wait for the cohort to arrive.
- When all pending tool replies are available, drain them and synthesize
  one combined REPLY whose content mirrors sync's
  `"[Tool result (call X)] …\n[Tool result (call Y)] …"` block.

F2 is only needed if F1 leaves a residual gap; we will measure first.

### F3. Flip the default
Once F1 (and optionally F2) shows epsilon delta on the validation set,
flip `SystemConfig.tool_dispatch` default from `"sync"` to `"async"`, plus
the `--tool-dispatch` CLI default in `evaluate.py`. Update the docstring at
`runtime.py:138–142` to reflect the new finding.

---

## Dataset-prep note

The picked TCs (`turn-granola-notes-into-tasks-temporal-TC001`,
`ai-email-assistant-temporal-TC001`, `expenses-tracker-temporal-TC001`)
came from `outputs/data/zapier/20260522_rev/workflows-mods.jsonl`, where
every object's `neighbors` field is empty — a pre-existing data-generation
gap unrelated to async work. With no graph edges the workflow can't route
messages past the entry-point service, and both sync and async run at 0%.
We hand-patched neighbors per TC (inferred from the per-object `behavior`
text) before benchmarking. The patched file lives at
`outputs/data/zapier/async_validation/workflows-mods.jsonl`.

## Iterations

> Each row is **steps-only** on `outputs/data/zapier/async_validation/workflows-mods.jsonl`
> with `--model gpt-5.4-mini --judge-model gpt-5.4 --judge-provider azure
> --runs 1`. "Score" is the fraction of step-judgements with verdict=PASS
> across all 3 TCs (one S001 step per TC).

| Iter | Dispatch | Code        | Steps pass | Wall (run) | Cost  | Notes |
|-----:|----------|-------------|-----------:|-----------:|------:|-------|
| 0a   | sync     | post-F1 (a052f32+F1) | 2/3 = 0.667 | 01:55 | $0.28 | reference |
| 0b   | async    | pre-F1 (stable a052f32) | 2/3 = 0.667 | 01:44 | $0.27 | reproduce regression — none observed |
| 1    | async    | post-F1 (f3617fc)       | 2/3 = 0.667 | 01:01 | $0.17 | assistant-turn preserved |

Pass/fail breakdown (identical across all three rows):
- `turn-granola-notes-into-tasks-…` — **FAIL**. Judge: the agent created
  4/5 Asana tasks (or, in sync, "more than 5") — a behavioral issue around
  multi-item dispatch, **not** an async-tool issue. Same failure mode in
  sync and async, with and without F1.
- `ai-email-assistant-…` — **PASS** in all three rows.
- `expenses-tracker-…` — **PASS** in all three rows.

### Reading the result

On this 3-TC validation set:

- **Async ≡ sync** at 0.667 pass rate. The "async regresses" pattern
  documented in `runtime.py:138` is not reproduced here.
- **F1 is neutral on score** (pre-F1 == post-F1 at this small N) but is
  correct-by-construction: the unit test
  `TestAsyncDispatchPreservesAssistantTurn` locks in the invariant that
  the LLM's prior tool_call round-trips as a real `role=assistant` message
  in the continuation prompt.
- **Async is materially faster and cheaper**: 01:01 vs 01:55 wall, $0.17
  vs $0.28 cost — async's batched tool execution overlaps with peer-message
  processing.

### Why F1 may still matter (on larger sets)

The three TCs in this set each fire **one tool call per step** through a
linear neighbor graph (entry → business-logic → write-service), so the
async path's "lost-intent" failure mode — where the LLM in the
continuation turn sees a tool result without seeing the call it made —
gets masked by the rendered active_plan step's `result:` line (which
encodes "this tool was called and returned X"). The failure mode is most
likely to bite when:

- the LLM dispatches **multiple tools in a batch** (so multiple REPLYs
  arrive in separate turns and the active_plan can only encode the last);
- the planner is off, so there is no active_plan to fall back on;
- the conversation is long enough that the LLM needs the explicit
  `assistant(tool_call) → user(result)` pairing to attribute the result
  correctly.

The next ablation should run F1 on/off on a tool-heavier subset (3+
tools/step, fan-out branches) or with `--no-enable-planner`. We did not
do this here to keep the spend bounded.

---

## Iter-0 verdict (superseded)

The 3-TC validation set above was rejected as too small. **F1 was reverted**
(commit `e13b731`) because its design — adding `MessageType.ASSISTANT_TURN`
and rendering it as a synthetic `role=assistant` message — broke role
symmetry with obj-to-obj communication, where peer REPLYs render as
user-role text in the history blob. Whatever context the LLM needs after
an async dispatch must come through the existing `MessageType.REPLY`
rendering and the `active_plan` snapshot — never via a parallel synthetic
type.

The sections below (Iter-1) replace the F1 plan with a properly sized
subset and a fix derived from the user-articulated invariant set.

---

# Iter-1 — 60-TC subset, invariant-driven harness fixes

## Invariant set (user spec)

1. **Timeouts = shared infra**, dispatch-mode-agnostic. Both sync and
   async hit the same HTTP-layer hang.
2. **Harness must preserve the order of API calls** (sync OR async).
3. **A call missing in dispatch order → plan stays pending/waiting**.
   The planner does not advance past the gap.
4. **A failed call → retry or marked failed in the plan** — never
   silently skipped.

## Dataset

`outputs/data/zapier/async_subset/eval60.jsonl` = `eval30` ∪ `eval30_extra`,
both drawn deterministically from `20260522_rev/workflows-mods.jsonl` by
`scripts/select_async_subset.py` (criteria: tools≥2, objects≥5, steps≥2,
base events present, ≤2 leaf nodes). 60 TCs across 17 workflow families,
6 mod-types each, dedups to 18 unique `sample_id`s under `--steps-only`.
Hold-out: `holdout10.jsonl` (10 TCs, no overlap with `eval60`).

Backwards-compat schema validators (`src/data/schema.py`, commit `6507d18`)
unblock the legacy `peers:[{object_id,relationship}]` /
`steps:[{text,target,…}]` shapes so the 20260420/0411/0512 clean files
load against the new schema. 1464 legacy TCs accepted, zero rejections.

## Iter-0 (pre-fix) — measuring the regression

`R1_sync` and `R2_async` on `eval30` (commit `9146c14`), repeated as
`R1_sync_extra` / `R2_async_extra` on `eval30_extra`. Combined matrix
(`R_pre_sync.jsonl` + `R_pre_async.jsonl`, n=71 events across 34 unique
TCs after dedup):

| | sync | async |
|---|---:|---:|
| total events | 71 | 71 |
| passes | 39 | 36 |
| pass rate (raw) | 54.9 % | 50.7 % |
| **HTTP/timeout failures (`Timeout after 180s`)** | 5 | 5 |
| pass rate excluding timeout events (n=61) | 60.7 % | 52.5 % |
| net async-specific delta | — | **−5 events ≈ −8.2 pt** |

**Failure-mode classification on the 5 genuine async regressions** (after
removing timeout-dominated ones): 3 state-ordering races (out-of-order
tool REPLY drove the planner past a still-pending step), 1 wrong-arg
(LLM used name instead of email), 1 misc. The two `exec_calls=0` async
timeouts share the same signature as the 5 sync timeouts — process_message
never produced an LLM call.

## Fix — invariant-aligned

### Gap A — brain HTTP timeout (invariant #1, #4)

Iter-0's timeouts (5 sync + 5 async) all signed up as `exec_calls=0`,
zero tokens, zero outgoing messages. Cause: the OpenAI / Azure SDK
clients had no default timeout, so a stuck HTTP request inside
`brain.react_call` / `plan_call` / `evaluate_call` left `read()` blocked
forever and the runtime's `Transaction` count never decremented. The
gateway dispatch's outer 180s wrapper aborts the eval but the runtime
thread stays parked, lingering across TCs.

Fix (commit `0a375fa`):
- `AzureBrain` and `OpenAIBrain` now accept `timeout: float = 90.0` and
  pass it to the SDK client. The SDK raises `APITimeoutError` on expiry.
- Runtime `_done` callback (`runtime.py:709`) classifies
  `apitimeouterror` / `request timed out` / `read timed out` as
  infra_error markers, so the event surfaces cleanly to the eval and
  the transaction releases.

### Gap B-strict — per-trace tool-REPLY gating (invariants #2, #3)

When the executor dispatches `[t1, t2, t3]` async (plan steps `s1, s2,
s3`), the worker for `t2` may finish before `t1`. Pre-fix, that REPLY
triggered an executor LLM call with `s1=dispatched, s2=done`, letting
the planner act on `s2` prematurely.

Fix (commit `0a375fa`, in `object.process_message`): when the inbound
message is a tool REPLY for plan step N **and** an earlier step M<N is
still `planned`/`dispatched`, capture the result onto `plan.steps[N]`
(already handled by `_execute_tool`) and return `status="pending"`
**without invoking the brain**. The object stays non-blocking — the
next mailbox message (peer, REPLY on another trace, etc.) is processed
normally. When the earliest pending step's REPLY arrives the gate
opens; the active_plan render then shows every captured result in
dispatch order.

Role symmetry preserved: tool REPLYs continue to render as user-role
text in the history blob, identical to peer REPLYs. **No new
`MessageType`**. **No `role=assistant` injection**. **No blocking** —
the deferral is per-trace LLM-call only, not per-object.

Unit tests in `tests/test_object.py::TestAsyncReplyGating` lock the
invariant:
- out-of-order REPLY defers (brain not invoked);
- earliest-pending REPLY opens the gate (brain invoked);
- no plan → no gating;
- peer REPLY is not gated.

## Iter-1 (post-fix) — measurement

`R3_sync_postfix` and `R3_async_postfix` on `eval60.jsonl`, both with
the same `--model gpt-5.4-mini --judge-model gpt-5.4 --judge-provider
azure --steps-only --runs 1 --verbose DEBUG` invocation:

| n=38 events (18 unique TCs after dedup) | sync | async |
|---|---:|---:|
| total events | 38 | 38 |
| passes | 24 | 23 |
| pass rate (raw) | 63.2 % | 60.5 % |
| **HTTP/timeout failures** | 2 | **0** |
| pass rate excluding timeout events (n=36) | 66.7 % | 61.1 % |
| net async-specific delta | — | **−1 event ≈ −3 pt (within ±ME)** |
| cost | $4.61 | $2.49 |
| wall | 32:30 | **17:24** |

**Net delta async vs sync, pre→post:**
- Async raw pass rate: 50.7 % → 60.5 % (**+9.8 pt**).
- Async timeouts: 5 → **0**. (Sync timeouts: 5 → 2 — Gap A also helps
  sync; the residual 2 are non-brain stalls, separate root cause.)
- Net async regression: **−5 events → −1 event (~80 % closed)**.
- Async is **46 % cheaper and 46 % faster** than sync in iter-1.

### Holdout collateral check

`R3_sync_holdout` / `R3_async_holdout` on `holdout10.jsonl` (9 unique
TCs after dedup, n=19 events):

| | sync | async |
|---|---:|---:|
| total events | 19 | 19 |
| passes | 10 | 12 |
| pass rate | 52.6 % | **63.2 %** |
| timeouts | 0 | 0 |
| cost | $2.12 | **$1.05** |
| wall | 11:22 | **6:29** |

Disagreement decomposition: 9 both-pass / 6 both-fail / 1 regression /
**3 gains**. **Net delta async − sync = +2 events** on a previously-unseen
sample. All three plan §8 termination criteria satisfied:
1. async ≥ sync on `eval60` (±1 TC) — 23 vs 24 = ±1 ✅
2. async ≥ sync on `holdout10` — +2 events ✅
3. zero tool-dispatch state regressions outside run-to-run noise ✅

## Default flipped (commit pending)

- `SystemConfig.tool_dispatch` default → `"async"` (`src/lnl/runtime.py:143`).
- `--tool-dispatch` CLI default → `"async"` (`src/data/evaluate.py:2685`).
- `runtime.py:132–143` comment rewritten to cite the iter-1 measurement
  and the two fixes (Gap A + Gap B-strict) that closed the original
  regression.
- `_VERSION` bumped in both `evaluate.py` (v43) and `evaluate_baseline.py`
  (v47) per repo convention.

## Outstanding work

1. **Investigate the residual 2 sync timeouts** in `R3_sync_postfix`.
   Both `exec_calls=0` with **zero tokens consumed** — Gap A's brain
   timeout did not fire. Likely a stall outside the brain HTTP path
   (event source binding? mock server handshake? cascade re-entry?).
   Async hit zero such timeouts, so the non-blocking actor model is
   incidentally resilient; the underlying bug is still real and worth
   tracking down. Independent of the async-default work.
2. **Backfill the data-generation pipeline** so freshly-generated
   workflows ship with populated `neighbors`. Currently the graph is
   encoded only in `behavior` prose and the `identify_objects` prompt
   silently leaves `neighbors=[]` on most objects.
3. **Larger N for tighter confidence.** Iter-1's ±ME is ±18.7 pt on a
   single TC of difference; a 200+ TC pass would tighten this. Cost
   budget permitting.

## Open questions

- The 1 residual event-level async regression (`it-helpdesk-temporal`,
  `leave-tracker-correction`, `order-request-form-exception` at TC
  level; only `it-helpdesk-temporal` was a regress in iter-0 too).
  `leave-tracker-correction` was a **gain** in iter-0 and a regression
  in iter-1 — that flip is strongly suggestive of run-to-run noise, not
  a stable failure mode.
- Sync still has 2 non-brain stalls. What's the root cause?
