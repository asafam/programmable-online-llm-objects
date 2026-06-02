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

## Iterations

> Each row is **steps-only** on `outputs/data/zapier/async_validation/workflows-mods.jsonl`
> with `--model gpt-5.4-mini --judge-model gpt-5.4 --runs 1`.
> "Score" is the fraction of step-judgements with verdict=PASS across all
> steps in the 3 TCs.

| Iter | Dispatch | Change            | Score | Δ vs sync | Notes |
|-----:|----------|-------------------|------:|----------:|-------|
| 0a   | sync     | baseline (stable) |   TBD |       0.0 | reference |
| 0b   | async    | current state     |   TBD |       TBD | reproduce regression |
| 1    | async    | F1 (assistant turn preserved) | TBD | TBD | |
| 2    | async    | F1+F2 (batched replies, if needed) | TBD | TBD | |

(Filled in as runs land.)

---

## Open questions

- How sensitive is the gap to planner-on vs planner-off? Planner-on
  partially mitigates via plan-step rendering, but loses args/thought.
  Validate F1 on both `--enable-planner` and `--no-enable-planner` once we
  have a reproduction.
- Does the LLM use `call_id` correctly when only one result is shown? The
  assistant-turn rendering carries `tool_calls[i].id`; the REPLY rendering
  carries `[Tool result (call X) …]`. F1 should make the pairing obvious.
