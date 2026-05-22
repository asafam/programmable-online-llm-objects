# LNL LLM-Object: Architecture & Contributor Guide

This document explains what an **LLM-object** is in the LNL runtime, how it processes a single message, and the machinery around it: the plan–execute–evaluate loop, the message bus, tool dispatch, memory backends, wait correlation, heartbeats, and more.

Pair this with [CLAUDE.md](CLAUDE.md) (project-level commands) and [EVALUATION.md](EVALUATION.md) (running the eval).

---

## 1. What is an LLM-object?

An **LLM-object** is a virtual actor whose definition is written in natural language (a markdown file with `## Role`, `## Behavior`, `## Peers`, ...). It owns a private mailbox, processes messages sequentially, holds mutable state across turns, and can dispatch to declared peers, call tools, or update its own state.

The atomic unit:

| Field | Type | Source |
|---|---|---|
| `object_id` | str | Unique identifier; used by the bus for routing |
| `role` | str | One-line description (purpose) |
| `behavior` | str | Multi-paragraph spec of what the object does on each event |
| `peers` | list[(peer_id, relationship)] | The ONLY object_ids this object may message |
| `skills` | list[str] | Named capabilities (advisory; not registered as tools) |
| `subscriptions` | list[str] | Pub/sub topics this object listens to |
| `event_sources` | list[str] | External-event source descriptors |
| `initial_state` | str | Optional markdown state seed |

Definition lives in `src/lnl/types.py:ObjectDefinition`; runtime instance is `src/lnl/object.py:LLMObject`.

**State** is a free-form string by default; the executor coerces to dict when JSON-parseable. State writes from concurrent traces are serialized by the mailbox FIFO — there is no concurrent mutation. Working memory for in-flight cascades lives on the **plan** (see §4), not state.

**Mailbox + drain loop** (`src/lnl/object.py:LLMObject.read`): messages enqueue in `_mailbox` (FIFO `deque`). The drain loop runs on a shared `ThreadPoolExecutor` and processes one message at a time until the mailbox empties AND no async tool futures are still pending. Different objects drain concurrently — the runtime's `pool_size=4` (default) determines parallelism.

---

## 2. The big picture

```
                  ┌──────────────┐
event ─────────► │   MessageBus  │ ◄──── outgoing messages
                  └──────┬───────┘
                         │ deliver
                         ▼
              ┌─────────────────────┐
              │  LLMObject mailbox  │
              └──────────┬──────────┘
                         │ drain()
                         ▼
              ╔═════════════════════╗
              ║  Plan ─ Execute ─ Evaluate  ║
              ╚══════════╤══════════╝
                         │ outgoings / tool_calls / state_update
                         ▼
                  (back to MessageBus)
```

A single message can trigger a **cascade** through many LLM-objects. The cascade is identified by a `trace_id` that propagates through every reply, tool call, and downstream tell/ask. The cascade ends when the active plan auto-closes (all steps terminal) or no further outgoings are produced.

---

## 3. The plan–execute–evaluate loop

For each DOMAIN message (the first event in a new cascade), an LLM-object runs a three-tier loop:

```
                 ┌──────────────────────────┐
                 │  1. Planner LLM (once)   │   reads role + behavior + event
                 └────────────┬─────────────┘   produces a Plan: goal + steps
                              │
                              ▼
            ┌────────────────────────────────────┐
            │  2. Executor LLM (ReAct loop)      │   reads active plan
            │  ── one or many turns ──           │   emits outgoing_messages,
            └────────────┬───────────────────────┘   tool_calls, state_update,
                         │                            plan_update
                         ▼
            ┌────────────────────────────────────┐
            │  3. Evaluator LLM (after finish)   │   grades each plan step
            │  PASS → close plan & commit state  │   sub-item by sub-item
            │  FAIL → re-enter executor with     │   returns structured criteria
            │         feedback (≤ N cycles)      │
            └────────────────────────────────────┘
```

Each tier is a **separate LLM call**, optionally on a different model:

| Tier | Brain | Config switch | Prompt file |
|---|---|---|---|
| Planner | `planner_brain` (defaults to executor brain) | `enable_planner` (default true) | `planner_sequential.yaml` / `planner_dag.yaml` |
| Executor | `brain` | always on | `executor_nested.yaml` (nested memory) / `executor.yaml` (flat memory) |
| Evaluator | `evaluator_brain` (defaults to executor brain) | `enable_evaluator` (default true) | `evaluator.yaml` |

### 3a. Planner

Fires **once per trace** for DOMAIN messages when `enable_planner=True`. Skipped on replies, tool returns, heartbeats, and continuations within the same trace — the plan is created once and re-used.

The planner's output is a `Plan` with a `goal` and a list of `PlanStep` items. The plan is stored in `_active_plans[trace_id]` and rendered into the executor's system prompt on every subsequent ReAct turn.

Canonical site: `src/lnl/object.py` (planner invocation block; search for `_planner_brain.plan_call`).

### 3b. Executor (ReAct)

The executor loop (`src/lnl/object.py:_run_react_cycle`) iterates until either:

- The LLM returns `action="finish"` (commitment — the turn is done), or
- The cross-turn `max_tool_rounds` cap is hit (default 5; cap is per-trace, not per-turn).

Each iteration the LLM emits one of:

- `action="tool_call"` (or `tool_calls=[...]` for a batch) — dispatch tool(s) and loop.
- `action="finish"` with a `finish` object containing:
  - `reply`: the response to the sender (empty string if none).
  - `outgoing_messages`: zero or more peer dispatches (tells and asks).
  - `state_update`: optional delta(s) — applied to the **plan's working state** if a plan exists, otherwise to master state.
  - `plan_update`: optional `{step_updates, add_steps, status}` — incremental plan mutation.

The executor sees the **active plan rendered as text** in its system prompt; step IDs are stable (`s1`, `s2`, ...) and captured step results (peer replies, tool returns) are rendered alongside their step.

### 3c. Evaluator

After every executor finish, the evaluator (if enabled and a plan exists) grades the turn at **sub-item granularity** — one criterion per required field, per destination, per audit-log entry. Returns a structured verdict:

- **PASS** → runtime auto-closes any `kind="reason"` steps still planned (they have no outgoing to close them), then auto-closes the plan if all steps are terminal.
- **FAIL** with actionable feedback → runtime synthesizes a feedback heartbeat and re-enters the executor with the feedback as a user message. Capped at `evaluator_max_cycles_per_trace` (default 3) to bound cost.

Canonical site: `src/lnl/object.py` (evaluator block; search for `_evaluator_brain`).

---

## 4. Plan & PlanStep schema

`src/lnl/types.py:Plan / PlanStep`.

### PlanStep

| Field | Description |
|---|---|
| `id` | Stable string id (`s1`, `s2`, ..., final marker is `"final"`). The LLM references results across steps via these ids ("post URL from `s2.result`"). |
| `kind` | `"ask"` \| `"tell"` \| `"tool"` \| `"reason"` \| `"wait"` |
| `target` | peer_id (for ask/tell), tool name (for tool), `"self"` (for reason), `None` (for wait) |
| `description` | One-line NL of what to dispatch / record |
| `depends_on` | list[str] of step ids whose results this step consumes (DAG mode uses this for ready-set computation) |
| `status` | `"planned"` → `"dispatched"` → `"done"` \| `"failed"` \| `"skipped"` |
| `result` | Captured output (NL for peer replies, JSON for tool returns); rendered into the executor prompt for downstream steps to reference verbatim |
| `result_kind` | `"nl"` \| `"tool"` \| `"reason"` \| `"event"` |
| `wait_predicate`, `wait_source`, `wait_timeout_seconds` | Only on `kind="wait"` |

### Plan

| Field | Description |
|---|---|
| `goal` | One-sentence summary |
| `steps` | Ordered list of `PlanStep` |
| `status` | `"active"` → `"waiting"` (if any wait registered) → `"complete"` \| `"cancelled"` \| `"abandoned"` \| `"failed"` |
| `trace_id` | Cascade identity this plan owns |
| `state` | Working copy of master state, frozen at plan creation; deltas accumulate here until the plan closes |
| `additional_trace_ids` | Secondary traces absorbed via wait-step matching (one plan can span multiple cascades) |
| `tool_rounds` | Cross-turn tool-dispatch counter (caps to `max_tool_rounds`) |

### Step lifecycle

- **tell** steps → marked `done` on dispatch (fire-and-forget).
- **ask** steps → marked `dispatched` on outgoing send; flip to `done` when the reply arrives (via `_auto_mark_step_on_reply`).
- **tool** steps → marked on dispatch; result captured when the tool REPLY arrives.
- **reason** steps → closed by the evaluator on PASS (they produce no outgoing).
- **wait** steps → registered in `_pending_waits`; closed when the wait_matcher binds an inbound event.

Auto-close: `_auto_close_plan_if_complete` (`src/lnl/object.py`) fires after every turn; if all steps are terminal (`done`/`failed`/`skipped`), the plan moves to `_completed_plans`, accumulated deltas commit to master state, and the trace ends.

Plan retirement: plans idle for `stale_plan_seconds` (default 180s) move to `"abandoned"`; if a single object holds more than `max_active_plans_per_object` (default 32), the oldest-by-`last_progress_at` is force-retired.

---

## 5. Planner modes: `sequential` vs `dag`

Configured by `SystemConfig.planner_mode` (also `--planner-mode` CLI on `evaluate.py`).

### Sequential (default)

The planner prompt (`planner_sequential.yaml`) instructs: *"another component will execute the plan one step per turn"*. Even when behaviors say "send to A and B simultaneously," the planner emits two separate steps for the executor to walk one at a time. The executor's `active_plan` rendering lists all steps but does not flag readiness.

Best for: chained workflows where step N consumes step N-1's reply; reproducibility against historical runs; conservative latency budgets.

### DAG (opt-in)

The planner prompt (`planner_dag.yaml`) instructs: *"design your plan as a DAG, not a sequence — independent steps will be dispatched IN PARALLEL in the same turn"*. The same `PlanStep` schema is used (no code changes!) but the planner authors `depends_on` deliberately: only when sN's payload literally embeds sM's result.

The executor's `active_plan` rendering gets a `ready: [s1, s2, ...]` header listing every step whose `depends_on` is empty or fully satisfied, and tags those steps `READY` inline. A DAG-mode addendum is injected into the executor prompt instructing: *"if `ready:` lists N dispatch steps, your finish MUST produce N entries across `outgoing_messages` and `tool_calls` combined"*.

Best for: fan-out heavy workflows (multi-peer notifications, parallel writes); reducing wall-clock latency by 1 LLM turn per parallel branch.

The schema, runtime correlation (`_correlate_outgoing`), and plan auto-close logic already accommodate multiple ready steps per turn — the change is purely on the prompt side plus a config toggle.

See `config/prompts/lnl/planner_dag.yaml` for the anti-pattern list and the worked examples (fan-out, chained, ask fan-out, tool+tell with shared input, mixed). Sequential mode rendering is byte-identical to pre-DAG output (regression-locked by test).

---

## 6. Tool dispatch: `async` vs `sync`

Configured by `SystemConfig.tool_dispatch` (also `--tool-dispatch` on `evaluate.py`).

### Async (default)

When the executor emits `tool_calls=[...]`, each tool is submitted to the object's private `ThreadPoolExecutor` (lazy-initialized, `tool_pool_size=4`). The executor returns immediately (status: pending) and the ReAct cycle suspends. Each tool, when it completes, posts a `MessageType.REPLY` back to the object's own mailbox with `sender=f"__tool__:{tool}"` and `status="ok"|"failed"`.

The drain loop unblocks once the tool replies arrive and processes them as the next message(s), continuing the ReAct cycle. This means **a single ReAct turn can take multiple LLM calls** — one per tool-batch cycle.

Pros: tools run truly in parallel; the object can interleave tool replies with other inbound messages on the same mailbox.
Cons: extra LLM call per tool round; tool_pool needs sizing.

### Sync

When set to `"sync"`, the tool registry executes each tool inline on the executor thread, appends the result to the LLM message list, and continues the ReAct cycle without a mailbox round-trip. No tool pool, no async REPLYs.

Pros: simpler control flow; lower per-turn token cost; one continuous LLM conversation.
Cons: blocks the executor thread; long tools serialize.

Canonical site: `src/lnl/object.py:_run_react_cycle` (look for `_tool_dispatch == "sync"` vs the async submit path).

---

## 7. Memory backends: `nested` vs `flat`

Configured by `SystemConfig.memory_backend` (also `--memory` on `evaluate.py`). Selects the action shape the executor emits AND the executor prompt that teaches it.

| Backend | Action shape | State shape | Executor prompt |
|---|---|---|---|
| `nested` (default) | `[{op, path, value}]` with `op ∈ {set, merge, delete, append}`, dotted paths (`tickets.T-042.status`) | Nested JSON object; immutable updates touch only the named path | `executor_nested.yaml` |
| `flat` | `{op, key, value}` with `op ∈ {set, delete, append}` | Flat top-level dict; nested entities re-emitted in full | `executor.yaml` |

The runtime exposes the same `state` getter regardless of backend (`object.py:state` property — serializes the backend's tree). Implementations live in `src/lnl/memory.py`.

Why the choice matters: nested mode emits **targeted updates** for entity-attribute changes (e.g., "set status of ticket T-042 to closed" → one delta of ~80 tokens). Flat mode re-emits the whole entity per change (~800 tokens). On multi-attribute mutations the difference compounds.

`--memory` selects the matching executor prompt automatically; don't override `--object-prompt` unless you know what you're doing.

---

## 8. Message bus and trace correlation

`src/lnl/bus.py:MessageBus` handles three patterns:

- **Peer-to-peer**: `recipient="<object-id>"` → direct mailbox delivery.
- **Pub/sub**: `topic="<name>"` → all subscribers except the sender.
- **Broadcast**: `recipient="__broadcast__"` → all objects except the sender.

### Trace correlation

Every message carries a `trace_id` (the cascade root). The runtime propagates it through:

- Outgoings (`reply_msg.trace_id = result.source_trace_id`).
- Tool REPLYs (the tool reply inherits the originating message's trace).
- Wait-matched events (the inbound event's `trace_id` is rebound onto the absorbing plan; original recorded in `plan.additional_trace_ids`).

The runtime also stamps `plan_step_index` on each outgoing in `_correlate_outgoing` (`src/lnl/object.py`):

- First `planned` step whose `kind` matches `expects_reply` (tell ↔ expects_reply=false; ask ↔ true) and whose `target` matches the recipient → stamp `out.plan_step_index = i`.
- Single-candidate fallback when no target match but exactly one `planned` step of that kind exists.
- Tell → step flips to `done` on dispatch. Ask → step flips to `dispatched`; flips to `done` when the reply arrives.

### Cascade depth

`depth_remaining` starts at `max_chain_depth` (default 10) and decrements by 1 on every hop. When it reaches 0 the chain is cut — guard against infinite loops.

### Wave commits

Each `Runtime.send()` (or equivalent) opens a `_Transaction` that ref-counts every scheduled drain. The call blocks until the count reaches 0 — i.e., until the entire cascade quiesces. This is what `evaluate.py` uses to know when a TC event is "done."

---

## 9. Wait steps (multi-stage async workflows)

When a workflow expects an external event later (e.g., "after the user replies to the email, file the ticket"), the executor adds a `kind="wait"` step via `plan_update.add_steps` with:

- `wait_predicate`: NL description of what to wait for ("a reply email from {user}").
- `wait_source`: optional channel hint.
- `wait_timeout_seconds`: per-step override on the default (24h).

The runtime registers the wait in `_pending_waits` and flips `plan.status = "waiting"`. On every subsequent inbound DOMAIN/EVENT message to this object, the **wait matcher brain** (`wait_matcher.yaml`) is consulted:

- Inputs: object_id, inbound message, candidate waits with predicate + originating context + prior step results.
- Output: `"trace_id:step_index"` or `None`.
- On match: rebind the inbound message's `trace_id` onto the absorbing plan, record the original trace in `plan.additional_trace_ids`, mark the wait step `done`, drop from `_pending_waits`, return `plan.status` to `"active"`.

The matcher is a small LLM call by default but can be disabled per-object (`enable_wait_correlation=False`) when the workflow has no wait steps.

Canonical sites: `src/lnl/object.py:_register_wait`, `_dispatch_pending_waits`, `_correlate_to_pending_wait`.

---

## 10. Heartbeats

When `heartbeat_enabled=True` and the runtime is in **live mode** (`Runtime.run` or `Runtime.start`), a background daemon thread broadcasts `MessageType.HEARTBEAT` messages every `heartbeat_interval_seconds` (default 30s). Each object's behavior block has the option to scan its state on heartbeats and emit proactive outgoings.

Heartbeats:

- Have depth=1 (do not cascade).
- Are tagged in the prompt with `[system time: <ts>]`.
- Trigger pending-timeout sweeps: asks pending longer than `pending_timeout_seconds` (default 90s) are abandoned.
- Are also used by the evaluator's self-correction feedback delivery — when the evaluator returns FAIL with feedback, the runtime synthesizes a heartbeat to re-engage the executor without polluting the trace history.

Synchronous `evaluate.py` runs do NOT use heartbeats (they're a live-mode mechanism).

---

## 11. Knowledge gaps

Two opt-in mechanisms (`auto_track_knowledge_gaps`, `auto_ask_peers_on_gap`, both default off in `config/lnl/system.yaml` but enabled by SystemConfig defaults — check `system.yaml` for the active value):

- **Auto-track**: when the executor emits `finish.knowledge_gap={question, context}`, the runtime appends a state delta recording the gap (`append "knowledge_gaps" {...}`).
- **Auto-ask peers**: with the above, the runtime also synthesizes outgoing Asks to every declared peer (except the sender that prompted the gap) with the question, expecting a reply.

Useful for objects that act as router/dispatcher and need to query peers for context they lack.

---

## 12. Sink completion shim

`enable_sink_completion_shim` (off in runtime defaults; on in `evaluate.py --sink-shim`).

Some objects are pure **sinks** — write services with no peers (`Storage`, `Email`, `Slack`, `Drive`, ...). In benchmark-mode evaluation, sink behaviors sometimes finish without producing a concrete artifact (URL, message_id, row_id) in the reply or state — the LLM "deferred" the action.

The shim detects these turns (plan has only tool/reason steps OR role text matches sink-keywords) and, when the reply lacks an artifact AND state lacks completion markers, synthesizes:

1. A role-appropriate artifact (Drive link, Slack ts, Gmail draft_id, Jira issue_key, ...).
2. A `set "auto_completion"` state delta.
3. An augmented reply with the artifact reference.

This is a **benchmark hint** — the synthesized artifact shapes match the Zapier judge rubric. Production-equivalent runs should keep the shim off.

Canonical site: `src/lnl/object.py` (search for `_apply_sink_completion_shim`).

---

## 13. Code tool & runtime-created objects

### `python` code tool (`enable_code_tool=True`)

Per-object stateful Python REPL. Variables, imports, and function definitions persist across calls for the lifetime of the object. Used for deterministic arithmetic, parsing, transforms — anything better solved by code than by NL reasoning.

Tool execution lives in `src/lnl/tools.py:CodeExecutor`. The namespace is held on the object (`_repl_namespace`) and is **not** serialized into the prompt.

### `create_object` tool

A core tool every object can call. Arguments: `object_id`, `class_id`, optional `params`. The runtime spawns a new live LLMObject from a registered class template (`Runtime.register_class` → `Runtime.spawn`), wires it into the bus, and injects an init event so it starts initialization behavior immediately.

Use this for workflows that need to dynamically materialize handlers (e.g., per-ticket object, per-customer dispatcher).

---

## 14. Runtime API

`src/lnl/runtime.py:Runtime`. The single entry point.

### Construction

```python
from src.lnl.runtime import Runtime, SystemConfig
from src.lnl.brain import OpenAIBrain   # or AnthropicBrain, GoogleBrain, MockBrain

cfg = SystemConfig.load()                  # or SystemConfig(planner_mode="dag", ...)
brain = OpenAIBrain(model="gpt-4o")
rt = Runtime(
    brain,
    system_config=cfg,
    planner_brain=brain,                   # optional separate planner model
    evaluator_brain=brain,                 # optional separate evaluator model
    tool_registry=tool_registry,           # required if you want create_object/python tools
)
```

### Loading objects

```python
rt.load_file("programs/hotel/objects/concierge.md")          # → LLMObject
rt.load_directory("programs/hotel/objects/")                  # → list[LLMObject]
rt.create_object(ObjectDefinition(object_id="x", ...))        # programmatic
rt.create_object_from_text("# x\n## Role\n...")               # from markdown string
rt.register_class("ticket-handler", definition)               # class template
rt.spawn(object_id="t1", class_id="ticket-handler", params={"ticket_id": "T-042"})
```

### Messaging

```python
results = rt.send("concierge", "Check in Alice")              # sync; blocks until cascade quiesces
results = rt.send_admin("concierge", "...")                   # admin message: definition-only, no ReAct
results = rt.send_many(items, on_result=callback)             # batch; true concurrent dispatch
rt.broadcast("morning kickoff")                                # all objects
rt.publish("incidents", "P0 fired")                            # pub/sub
rt.inject_event("ticket-router", payload, source="zendesk")   # external event injection
```

### Modification (runtime updates)

```python
rt.modify("concierge", role="Updated role", behavior="...")   # mutate definition live
rt.add_peer("concierge", "manager", "escalations")
rt.remove_peer("concierge", "manager")
```

### Querying

```python
rt.state("concierge")             # → str (the live state blob)
rt.snapshot("concierge")          # → dict with state, plans, history, definition
rt.topology()                     # → {object_id: [peer_ids, ...]}
```

### Live mode (long-running runtime)

```python
rt.start(poll_interval=0.5, on_result=callback)  # non-blocking; spawns run-loop thread
rt.submit("concierge", "...")                     # returns _WorkItem; non-blocking
rt.process_pending()                              # drain queue once
rt.stop(timeout=10)                                # shutdown
```

Live mode is what enables heartbeats and event-source polling. `evaluate.py` does NOT use live mode — it calls `rt.send()` synchronously per event.

---

## 15. Configuration: `SystemConfig` cheatsheet

`src/lnl/runtime.py:SystemConfig`. Loaded from `config/lnl/system.yaml` or constructed directly.

| Field | Default | Effect |
|---|---|---|
| `heartbeat_enabled` | `False` | Live-mode background heartbeat broadcast |
| `heartbeat_interval_seconds` | `30.0` | Heartbeat cadence |
| `pending_timeout_seconds` | `90.0` | Wall-clock before abandoning a pending ask |
| `max_tool_rounds` | `5` | Per-trace cap on tool dispatches in the ReAct loop |
| `max_chain_depth` | `10` | Hop budget per cascade |
| `max_history` | `6` | History window per object |
| `react_cross_objects` | `True` | Include the Peer Interaction Loop section in system prompts |
| `auto_track_knowledge_gaps` | `True` | Record `finish.knowledge_gap` in state |
| `auto_ask_peers_on_gap` | `True` | Synthesize peer Asks when a gap is recorded |
| `enable_code_tool` | `True` | Register the `python` REPL tool |
| `enable_sink_completion_shim` | `False` | Benchmark-mode sink artifact synthesis |
| `enable_planner` | `True` | Run the pre-execution planner |
| `enable_evaluator` | `True` | Run the post-execution evaluator with self-correction |
| `evaluator_max_cycles_per_trace` | `3` | Cap on FAIL → retry cycles |
| `stale_plan_seconds` | `180.0` | Idle time before plan → abandoned |
| `max_active_plans_per_object` | `32` | Cardinality cap; oldest force-retired |
| `memory_backend` | `"nested"` | `"nested"` or `"flat"` |
| `tool_dispatch` | `"async"` | `"async"` or `"sync"` |
| `planner_mode` | `"sequential"` | `"sequential"` or `"dag"` |

CLI flags on `evaluate.py` map to these (e.g., `--memory`, `--tool-dispatch`, `--planner-mode`, `--max-tool-rounds`, `--enable-planner` / `--no-enable-planner`, `--enable-evaluator` / `--no-enable-evaluator`, `--sink-shim` / `--no-sink-shim`).

---

## 16. Where to look next

- **`src/lnl/object.py`** — the heart of everything. Search for these landmarks:
  - `process_message` — top of the per-message flow.
  - `_run_react_cycle` — the executor ReAct loop.
  - `_correlate_outgoing` — the bridge between executor outgoings and plan steps.
  - `_auto_close_plan_if_complete` — plan retirement.
- **`src/lnl/runtime.py`** — `Runtime` (API), `SystemConfig` (config), `_Transaction` (wave commits), `_dispatch` (transactional send).
- **`src/lnl/brain.py`** — `LLMBrain` ABC, `build_system_prompt`, `build_planner_prompt`, `build_evaluator_prompt`, `_render_active_plan` (the DAG-mode ready-set logic lives here).
- **`src/lnl/bus.py`** — message routing.
- **`src/lnl/types.py`** — every dataclass: `Message`, `Plan`, `PlanStep`, `ObjectDefinition`, `ProcessingResult`, `InferenceMetrics`.
- **`src/lnl/memory.py`** — flat and nested memory backends.
- **`config/prompts/lnl/`** — every prompt template. Edit these to change behavior without touching code.
- **`config/lnl/system.yaml`** — runtime configuration defaults.

For docs on running benchmarks, see [EVALUATION.md](EVALUATION.md). For project-level commands and conventions, see [CLAUDE.md](CLAUDE.md).
