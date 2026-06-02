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

## Outstanding work

1. **Validate on a tool-heavier subset.** The current 3-TC set is too
   simple to exercise F1's intended fix. Pick TCs with ≥3 tool calls per
   step (or fan-out plans), or run a planner-off ablation.
2. **Flip the default to async** once (1) confirms epsilon delta.
   `SystemConfig.tool_dispatch` (`runtime.py:143`) and `--tool-dispatch`
   (`evaluate.py:2685`) both default to `"sync"`. The docstring at
   `runtime.py:138–142` will need a rewrite to reflect "post-F1, async is
   the default".
3. **Backfill the data-generation pipeline** so freshly-generated
   workflows (20260522_rev style) ship with populated `neighbors` lists.
   Currently the graph is encoded only in `behavior` prose and the
   identify_objects prompt expects the model to set neighbors explicitly.
4. **Optional F2 (batch tool REPLYs in `read()`)** — only needed if a
   tool-heavier ablation still shows a gap. Sketched in the fix-plan
   above; not implemented.

---

## Open questions

- How sensitive is the gap to planner-on vs planner-off? Planner-on
  partially mitigates via plan-step rendering, but loses args/thought.
  Validate F1 on both `--enable-planner` and `--no-enable-planner` once we
  have a reproduction.
- Does the LLM use `call_id` correctly when only one result is shown? The
  assistant-turn rendering carries `tool_calls[i].id`; the REPLY rendering
  carries `[Tool result (call X) …]`. F1 should make the pairing obvious.
