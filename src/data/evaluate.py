"""
Evaluation runner — Stage 3 of the data pipeline.

Executes Samples against the LNL runtime, judges outcomes with an LLM, and
reports correctness and cost metrics.

Usage:
    python -m src.data.evaluate \\
        -i outputs/data/zapier/20260322_120000/samples.jsonl \\
        --runs 3 \\
        --model gpt-4o --judge-model claude-sonnet-4-6
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import re
import statistics
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from tqdm import tqdm

# Maximum seconds the evaluator waits for the system to finish processing one event
# before injecting the next. Used by OpenClaw _wait_mock_quiescence as the fallback
# cap when the 3s-quiet heuristic never settles. LNL has an authoritative idle signal
# (rt.send blocks on the transaction barrier) so it relies on --timeout (default 180s)
# instead. Aligning OC's fallback to 180s keeps both systems on the same ceiling — OC
# slow cascades no longer get chopped at 30s while LNL gets 180s.
INTER_EVENT_TIMEOUT_S: float = 180.0

load_dotenv()

# ── Version ───────────────────────────────────────────────────────────────────

def _build_version() -> str:
    """Dynamic version string: git commit timestamp + short hash + '+dirty' if uncommitted changes."""
    import subprocess as _sp
    try:
        ts = _sp.check_output(
            ["git", "log", "-1", "--format=%cd", "--date=format:%Y%m%d_%H%M%S"],
            stderr=_sp.DEVNULL, text=True
        ).strip()
        sha = _sp.check_output(
            ["git", "log", "-1", "--format=%h"],
            stderr=_sp.DEVNULL, text=True
        ).strip()
        dirty = _sp.call(
            ["git", "diff", "--quiet", "HEAD"],
            stderr=_sp.DEVNULL
        ) != 0
        base = f"{ts}_{sha}" if ts and sha else "unknown"
        return f"{base}+dirty" if dirty else base
    except Exception:
        import os
        mtime = os.path.getmtime(__file__)
        from datetime import datetime
        return datetime.fromtimestamp(mtime).strftime("%Y%m%d_%H%M%S")

_VERSION: str = _build_version()  # bumped 2026-05-25 (v42): companion bump — mock_server.py injects generic-name aliases so baseline agents get TC-seeded responses.

from src.data.schema import (
    EvalSummary,
    EventResult,
    MockConfig,
    MockToolDef,
    ModificationResult,
    RunConfig,
    Sample,
    SampleResult,
    to_lnl_class_definition,
    to_lnl_definition,
)
from src.data.utils import (
    add_common_args,
    format_tc_event_detail,
    infer_provider,
    load_jsonl,
    print_run_info,
)


# ── Mock config loading ────────────────────────────────────────────────────────

def load_mock_config(path: Path) -> MockConfig:
    """Load a MockConfig from a YAML file."""
    data = yaml.safe_load(path.read_text())
    return MockConfig.model_validate(data)


def merge_mock_tools(
    global_tools: list[MockToolDef],
    tc_tools: list[MockToolDef],
) -> list[MockToolDef]:
    """Merge two tool lists. Right side wins on tool_name collision."""
    merged = {t.tool_name: t for t in global_tools}
    merged.update({t.tool_name: t for t in tc_tools})
    return list(merged.values())


def _derive_tools_from_skills(tc: "Sample") -> list[MockToolDef]:
    """Derive MockToolDef entries from skills declared on each object.

    Skills expose internal capabilities as callable tools so they appear in
    the {tools} system-prompt section. The LLM can invoke them via tool_calls;
    if no scripted mock overrides them, PassthroughExecutor handles the call
    and logs it for judge evidence.

    This is the lowest-priority layer — per-event triggered_by derivation,
    --mock-config files, and tc.mock_tools all override these entries.
    """
    seen: set[str] = set()
    result: list[MockToolDef] = []
    for obj in tc.objects:
        for skill in obj.skills:
            if skill in seen:
                continue
            seen.add(skill)
            label = skill.replace("-", " ").replace("_", " ")
            result.append(MockToolDef(
                tool_name=skill,
                description=f"Perform {label}.",
                arguments_schema={"type": "object", "additionalProperties": True},
                response_template=f"[mock] {skill} completed.",
            ))
    return result


@dataclass
class StepsSnapshot:
    """Captured runtime state after steps complete, for reuse across TC variants.

    Allows skipping redundant step re-execution when multiple test cases share
    the same base steps (same sample_id). Enable with --reuse-steps.
    """
    object_states: dict[str, str]           # obj_id → _state string
    object_histories: dict[str, list]       # obj_id → _history messages
    object_definitions: dict[str, object]   # obj_id → ObjectDefinition (shallow copy)
    tool_call_counts: dict[str, int]        # tool_name → _call_count after steps
    tool_call_logs: dict[str, list]         # tool_name → call_log after steps
    step_event_results: list               # EventResult list for steps (reused verbatim)
    prior_context: str                     # _format_prior_state after steps


def _take_snapshot(
    rt,
    mock_executors: list,
    step_event_results: list,
    prior_context: str,
) -> StepsSnapshot:
    """Capture runtime state after steps complete."""
    states: dict[str, str] = {}
    histories: dict[str, list] = {}
    definitions: dict[str, object] = {}
    for obj_id, obj in rt._bus._objects.items():
        states[obj_id] = obj._state
        histories[obj_id] = list(obj._history)
        definitions[obj_id] = copy.copy(obj._definition)

    tool_counts: dict[str, int] = {}
    tool_logs: dict[str, list] = {}
    for ex in mock_executors:
        name = getattr(getattr(ex, "_tool_def", None), "tool_name", None)
        if name is not None and hasattr(ex, "_call_count"):
            tool_counts[name] = ex._call_count
            tool_logs[name] = list(getattr(ex, "call_log", []))

    return StepsSnapshot(
        object_states=states,
        object_histories=histories,
        object_definitions=definitions,
        tool_call_counts=tool_counts,
        tool_call_logs=tool_logs,
        step_event_results=list(step_event_results),
        prior_context=prior_context,
    )


def _restore_snapshot(rt, mock_executors: list, snapshot: StepsSnapshot) -> None:
    """Restore runtime state from a snapshot (overwrites freshly-created objects).

    Also re-creates any objects that were dynamically spawned during the steps
    phase but do not exist in the freshly-initialized runtime.
    """
    # Re-create spawned objects missing from the current runtime
    for obj_id, defn in snapshot.object_definitions.items():
        if obj_id not in rt._bus._objects:
            rt.create_object(copy.deepcopy(defn))

    # Restore state, history, and definition for all snapshotted objects
    for obj_id, obj in rt._bus._objects.items():
        if obj_id not in snapshot.object_states:
            continue
        obj._state = snapshot.object_states[obj_id]
        obj._history = list(snapshot.object_histories[obj_id])
        src = snapshot.object_definitions[obj_id]
        for attr in ("role", "behavior", "peers", "skills", "subscriptions", "event_sources", "initial_state"):
            if hasattr(src, attr):
                setattr(obj._definition, attr, copy.copy(getattr(src, attr)))

    for ex in mock_executors:
        name = getattr(getattr(ex, "_tool_def", None), "tool_name", None)
        if name is not None and name in snapshot.tool_call_counts:
            ex._call_count = snapshot.tool_call_counts[name]
            ex.call_log = list(snapshot.tool_call_logs.get(name, []))


# ── Timestamp parsing ──────────────────────────────────────────────────────────

def parse_when(when: str) -> int:
    """Convert 'W02-1T10:30' → ordinal minutes for sorting."""
    week_part, time_part = when.split("T")
    w, d = week_part.lstrip("W").split("-")
    h, m = time_part.split(":")
    return (int(w) * 7 + int(d)) * 1440 + int(h) * 60 + int(m)


# ── Evidence gathering ─────────────────────────────────────────────────────────

def _format_tool_call_entries(flat: "list[dict]") -> "list[str]":
    """Render tool-call log entries as evidence lines for the judge.

    Each entry shows `[tool] call#N {args}` plus the `← {response}` it
    received, plus any chain-triggered dispatches. Returns a list of
    formatted lines (one per entry) — the caller decides whether to put
    them under "THIS EVENT" or "PRIOR TOOL EXECUTIONS".
    """
    lines = []
    for entry in flat:
        idx = entry.get("call_index", "?")
        line = f"  [{entry['tool']}] call#{idx} {json.dumps(entry['arguments'])}"
        if "response" in entry:
            line += f"\n    ← {entry['response']}"
        for t in entry.get("triggered", []):
            line += f"\n    → dispatched to [{t['target']}]: {t['message']}"
        lines.append(line)
    return lines


def gather_evidence(
    rt,
    results,
    recipient: str,
    bus_messages: "list | None" = None,
    tool_calls: "list[list[dict]] | None" = None,
    prior_tool_calls: "list[list[dict]] | None" = None,
) -> str:
    """Collect observable evidence after an event for the LLM judge.

    Evidence is structured in sections:
    - OBJECT REGISTRY: each object's role.
    - PRIOR TOOL EXECUTIONS (optional): tool calls + responses that ran in
      EARLIER events of this TC. The judge can cross-reference what the
      workflow did upstream when judging the current event.
    - THIS EVENT: everything that happened during the current invocation —
      tool calls made (primary), bus messages exchanged, and replies.
    - OBJECT STATES: each object's updated_state after the event
      (may be empty; use THIS EVENT and PRIOR TOOL EXECUTIONS as
      authoritative evidence).
    """
    this_event_parts: list[str] = []

    # Tool calls (primary evidence when mock tools are in use)
    if tool_calls:
        flat = [entry for per_ex in tool_calls for entry in per_ex]
        if flat:
            this_event_parts.append("Tool calls:\n" + "\n".join(_format_tool_call_entries(flat)))

    # Bus messages: the full message flow visible in --debug-messages
    if bus_messages:
        lines = []
        for ml in bus_messages:
            msg = ml.message
            arrow = "↩" if msg.type.value == "reply" else "→"
            sender = f"[{msg.sender}]" if msg.type.value == "event" else msg.sender
            lines.append(f"  {sender} {arrow} {msg.recipient} ({msg.type.value}): {str(msg.content)}")
        this_event_parts.append("Message bus activity:\n" + "\n".join(lines))

    # Replies from the chain triggered by this event
    replies = [r for r in results if r.reply and str(r.reply).strip()]
    if replies:
        this_event_parts.append("Replies:\n" + "\n".join(f"  [{r.object_id}]: {r.reply}" for r in replies))

    # Object states after this event (what each object recorded/knows)
    state_parts: list[str] = []
    for obj_id, obj in rt._bus.objects.items():
        state = obj.state
        if isinstance(state, dict):
            state_str = json.dumps(state, indent=2) if state else "(empty)"
        else:
            state_str = str(state).strip() or "(empty)"
        state_parts.append(f"  [{obj_id}]:\n{state_str}")

    # Object registry — helps the judge map object IDs to external systems
    registry_lines = []
    for obj_id, obj in rt._bus.objects.items():
        role = getattr(obj.definition, "role", "")
        if role:
            registry_lines.append(f"  [{obj_id}]: {role}")

    sections: list[str] = []
    if registry_lines:
        sections.append("=== OBJECT REGISTRY ===\n" + "\n".join(registry_lines))
    if prior_tool_calls:
        prior_flat = [entry for per_ex in prior_tool_calls for entry in per_ex]
        if prior_flat:
            sections.append(
                "=== PRIOR TOOL EXECUTIONS (calls made in earlier events of this test case; "
                "use these to cross-reference what the workflow did upstream) ===\n"
                + "\n".join(_format_tool_call_entries(prior_flat))
            )
    if this_event_parts:
        sections.append("=== THIS EVENT ===\n" + "\n\n".join(this_event_parts))
    if state_parts:
        sections.append("=== OBJECT STATES ===\n" + "\n\n".join(state_parts))

    return "\n\n".join(sections) if sections else "(no observable state)"


# ── Trace reconstruction ───────────────────────────────────────────────────────

def build_event_trace(bus_msgs) -> tuple[list[dict], "str | None"]:
    """Build a structured per-hop trace from the bus log slice for one event.

    Each span captures: msg_id, parent_id, trace_id, sender, recipient, type,
    t_offset_ms (vs. root timestamp), mailbox_wait_ms, processing_ms,
    llm_latency_ms, input_tokens, output_tokens, hop_depth.

    Returns (spans, root_trace_id). The root_trace_id is the trace_id of the
    earliest span (the original trigger); spans are sorted by creation time.
    """
    if not bus_msgs:
        return [], None

    # Sort by message creation timestamp for deterministic ordering.
    sorted_logs = sorted(bus_msgs, key=lambda ml: ml.message.timestamp)
    root = sorted_logs[0]
    root_ts = root.message.timestamp
    root_trace_id = root.message.trace_id or root.message.id

    spans: list[dict] = []
    for ml in sorted_logs:
        msg = ml.message
        t_offset_ms = (msg.timestamp - root_ts).total_seconds() * 1000.0
        mailbox_wait_ms = None
        if ml.received_at is not None and ml.processing_started_at is not None:
            mailbox_wait_ms = (ml.processing_started_at - ml.received_at).total_seconds() * 1000.0
        processing_ms = None
        if ml.processing_started_at is not None and ml.processing_completed_at is not None:
            processing_ms = (ml.processing_completed_at - ml.processing_started_at).total_seconds() * 1000.0
        llm_latency_ms = ml.metrics.latency_ms if ml.metrics else None
        in_tok = ml.metrics.input_tokens if ml.metrics else 0
        out_tok = ml.metrics.output_tokens if ml.metrics else 0
        spans.append({
            "msg_id": msg.id,
            "parent_id": msg.parent_id,
            "trace_id": msg.trace_id,
            "sender": msg.sender,
            "recipient": msg.recipient,
            "type": msg.type.value if hasattr(msg.type, "value") else str(msg.type),
            "t_offset_ms": round(t_offset_ms, 2),
            "mailbox_wait_ms": round(mailbox_wait_ms, 2) if mailbox_wait_ms is not None else None,
            "processing_ms": round(processing_ms, 2) if processing_ms is not None else None,
            "llm_latency_ms": round(llm_latency_ms, 2) if llm_latency_ms is not None else None,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "hop_depth": ml.hop_depth,
        })
    return spans, root_trace_id


# ── Core execution ─────────────────────────────────────────────────────────────

def _print_message(msg) -> None:
    """Print a message exchange between LLM-objects."""
    arrow = "↩" if msg.type.value == "reply" else "→"
    content = str(msg.content)[:120].replace("\n", " ")
    # Mark external event sources clearly to distinguish from LLM-object IDs
    sender = f"[{msg.sender}]" if msg.type.value == "event" else msg.sender
    tqdm.write(f"      {sender} {arrow} {msg.recipient} ({msg.type.value}): {content}")


def _execute_test_case_inner(
    tc: Sample,
    brain,
    harness,
    debug_messages: bool = False,
    timeout_s: Optional[float] = None,
    steps_only: bool = False,
    max_chain_depth: int = 20,
    max_tool_rounds: int = 10,
    global_mock_tools: "list[MockToolDef] | None" = None,
    progress_callback=None,
    on_event_result=None,
    on_mod_applied=None,
    on_raw_event=None,
    steps_snapshot: "Optional[StepsSnapshot]" = None,
    snapshot_out: "Optional[list]" = None,
    concurrency: int = 0,
    concurrency_seed: int = 42,
    max_modifications: Optional[int] = None,
    object_prompt: Optional[str] = None,
    planner_prompt: str = "planner_sequential.yaml",
    max_history: Optional[int] = None,
    tracked_harness=None,
    enable_code_tool: bool = True,
    enable_sink_completion_shim: bool = False,
    enable_planner: bool = True,
    enable_evaluator: bool = True,
    planner_brain=None,
    evaluator_brain=None,
    mock_server_url: "str | None" = None,
    mock_slot_id: str = "default",
    memory_backend: str = "nested",
    log_planner_output: bool = False,
    tool_dispatch: str = "sync",
    planner_mode: str = "dag",
    enable_replan_checkpoints: bool = False,
    replan_max_per_trace: int = 3,
    enable_step_retry_replan: bool = True,
    step_max_retries: int = 2,
    step_replan_max: int = 1,
    reactive_replan_max_per_trace: int = 4,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Run a single Sample and return event + modification results."""
    from src.lnl.gateway import EventGateway
    from src.lnl.runtime import Runtime
    from src.lnl.tools import CodeExecutor, MockInProcessExecutor, PassthroughExecutor, ToolRegistry

    # 1. Build ToolRegistry — two-layer priority merge (lowest → highest):
    #    --mock-config global < tc.mock_tools
    #
    # Mock tools are declared explicitly in the test case or via --mock-config.
    # Reference data (employee records, catalogs, etc.) lives in the mock tool
    # response_template — not in ObjectDef. LLM-objects are never seeded with data.
    #
    # Skills are intentionally NOT included here — they are internal computations
    # (the generate_workflows prompt marks them as "purely internal, no external system
    # calls"). Registering them as tools would tell the LLM to call them externally.
    # Triggered events are dispatched directly by the harness (no mock tool needed).
    final_mock_tools = merge_mock_tools(global_mock_tools or [], tc.tools)

    # When --mock-server is active, register this TC's mock tools with the HTTP
    # mock server under a per-(tc, run) slot, then route tool-call responses
    # through it (POST /tool/{method}) — the same path the OpenClaw baseline
    # uses. Triggers still fire in-process below (mirrors the baseline).
    if mock_server_url and final_mock_tools:
        import httpx
        from src.data.mock_server import merge_tc_mock_tools
        mock_script = merge_tc_mock_tools(None, list(final_mock_tools))
        httpx.post(
            f"{mock_server_url}/configure",
            json={
                "slot_id": mock_slot_id,
                "session_key": mock_slot_id,
                "mock_script": mock_script.model_dump(),
            },
            timeout=10.0,
        )

    # Always create a ToolRegistry so create_object is registered as a core tool
    # on every Runtime (Runtime.__init__ registers CreateObjectExecutor when registry present).
    # Mock tools for domain skills are layered on top.
    mock_executors: list = []
    passthrough = PassthroughExecutor(object_ids={o.object_id for o in tc.objects})
    tool_registry: "ToolRegistry" = ToolRegistry()
    tool_registry.register("execute_code", CodeExecutor())
    tool_registry.register_fallback(passthrough)
    for mock_tool in final_mock_tools:
        # remote_url=None → unchanged in-process behavior; set → responses come
        # from the HTTP mock server under this run's slot.
        executor = MockInProcessExecutor(
            mock_tool,
            remote_url=mock_server_url,
            slot_id=mock_slot_id,
        )
        tool_registry.register(mock_tool.tool_name, executor, spec=executor.spec)
        mock_executors.append(executor)

    # 2. Create Runtime and EventGateway — synchronous mode (no rt.start()).
    # The evaluator dispatches one event at a time and waits; the background
    # run-loop is not needed and would break _run_with_timeout (ThreadPoolExecutor
    # shutdown(wait=True) hangs when the foreground thread is blocked on item.done).
    from src.lnl.runtime import SystemConfig
    sys_cfg = SystemConfig(
        max_tool_rounds=max_tool_rounds,
        enable_code_tool=enable_code_tool,
        enable_sink_completion_shim=enable_sink_completion_shim,
        enable_planner=enable_planner,
        enable_evaluator=enable_evaluator,
        memory_backend=memory_backend,
        tool_dispatch=tool_dispatch,
        planner_mode=planner_mode,
        enable_replan_checkpoints=enable_replan_checkpoints,
        replan_max_per_trace=replan_max_per_trace,
        enable_step_retry_replan=enable_step_retry_replan,
        step_max_retries=step_max_retries,
        step_replan_max=step_replan_max,
        reactive_replan_max_per_trace=reactive_replan_max_per_trace,
    )
    rt = Runtime(
        brain,
        tool_registry=tool_registry,
        max_chain_depth=max_chain_depth,
        system_config=sys_cfg,
        planner_brain=planner_brain,
        evaluator_brain=evaluator_brain,
    )
    # Only override the runtime's prompt-file defaults when the caller passes
    # an explicit value — otherwise let the runtime pick its backend-derived
    # default (executor.yaml for flat, executor_nested.yaml for nested).
    if object_prompt is not None:
        rt.set_prompt_file(object_prompt)
    if planner_prompt != "planner_sequential.yaml":
        rt.set_planner_prompt_file(planner_prompt)
    if max_history is not None:
        rt.set_max_history(max_history)
    if debug_messages and progress_callback:
        rt.set_message_listener(lambda msg: (_print_message(msg), progress_callback(msg)))
    elif debug_messages:
        rt.set_message_listener(_print_message)
    elif progress_callback:
        rt.set_message_listener(progress_callback)
    gw = EventGateway(rt)

    for cls_def in tc.llm_classes:
        rt.register_class(cls_def.class_id, to_lnl_class_definition(cls_def))
    for obj_def in tc.objects:
        rt.create_object(to_lnl_definition(obj_def))

    try:
        return _run_test_case_timeline(
            tc, rt, gw, harness,
            timeout_s=timeout_s,
            steps_only=steps_only,
            mock_executors=mock_executors + [passthrough],
            steps_snapshot=steps_snapshot,
            snapshot_out=snapshot_out,
            on_event_result=on_event_result,
            on_mod_applied=on_mod_applied,
            concurrency=concurrency,
            concurrency_seed=concurrency_seed,
            max_modifications=max_modifications,
            tracked_harness=tracked_harness,
            log_planner_output=log_planner_output,
        )
    finally:
        rt._pool.shutdown(wait=False)


def _quiesce_after_timeout(rt, max_wait_s: float) -> None:
    """A timed-out event's wave is abandoned, NOT cancelled — its threads keep
    processing and would interleave with the next event, racing custodian
    versions and cross-contaminating leads (observed: SC001's commit landing
    with version 7 while SC003 was dispatching). Wait (bounded) for every
    object to go idle before the next event is released."""
    import time as _t
    deadline = _t.monotonic() + max_wait_s
    while _t.monotonic() < deadline:
        objs = list(rt._bus._objects.values())
        if all(not getattr(o, "_active", False) and not getattr(o, "_mailbox", None) for o in objs):
            return
        _t.sleep(2.0)
    logging.getLogger(__name__).warning(
        "quiesce: abandoned wave still running after %.0fs — state may bleed into the next event",
        max_wait_s)


def _run_with_timeout(fn, timeout_s: Optional[float]):
    """Run fn() with an optional per-step timeout. Returns (result, timed_out).

    Uses a daemon thread so an abandoned timed-out thread never blocks process exit.
    ThreadPoolExecutor is avoided because its shutdown(wait=True) hangs when the
    worker is blocked on I/O (e.g. waiting for an LLM API response).
    """
    if timeout_s is None:
        return fn(), False
    import threading
    result_box: list = [None]
    exc_box: list = [None]
    def worker():
        try:
            result_box[0] = fn()
        except Exception as e:
            exc_box[0] = e
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        return [], True
    if exc_box[0] is not None:
        raise exc_box[0]
    return result_box[0] or [], False


def _format_prior_state(rt) -> str:
    """Snapshot all object states as a context string for the judge."""
    lines = ["=== PRIOR STATE (resolved runtime values from previous steps) ==="]
    for obj_id, obj in rt._bus.objects.items():
        state = obj.state
        if state:
            state_str = json.dumps(state, indent=2) if isinstance(state, dict) else str(state)
            lines.append(f"[{obj_id}]:\n{state_str}")
    return "\n\n".join(lines)


def _format_active_mods(modifications) -> str:
    """Format active modifications as a context block for the judge.

    'Later modifications supersede earlier ones where they overlap' is stated
    explicitly so the judge reasons against the cumulative, ordered state.
    """
    if not modifications:
        return ""
    lines = ["=== ACTIVE MODIFICATIONS (applied in order; later ones supersede earlier where they overlap) ==="]
    for i, m in enumerate(modifications, 1):
        lines.append(f"[{i}] {m.id} → {m.target}: {m.intent}")
    return "\n".join(lines)


def _build_event_ctx(event, modifications, prior_context: str) -> str:
    """Build the full judge context string for one event.

    Uses event.after_mod_ids when populated; falls back to timestamp-based
    inference for old test cases that lack the field.
    """
    mod_by_id = {m.id: m for m in modifications}
    if event.after_mod_ids:
        active = [mod_by_id[mid] for mid in event.after_mod_ids if mid in mod_by_id]
    else:
        # Backward-compat: infer from timestamp ordering
        ek = parse_when(event.when)
        active = [m for m in modifications if parse_when(m.when) <= ek]
    mods_ctx = _format_active_mods(active)
    if mods_ctx and prior_context:
        return f"{mods_ctx}\n\n{prior_context}"
    return mods_ctx or prior_context


def _snapshot_logs(mock_executors: list) -> list[int]:
    return [len(ex.call_log) for ex in mock_executors]


def _new_tool_calls(mock_executors: list, snapshots: list[int]) -> list[list[dict]]:
    return [ex.call_log[s:] for ex, s in zip(mock_executors, snapshots)]


def _prior_tool_calls(mock_executors: list, snapshots: list[int]) -> list[list[dict]]:
    """Tool calls from BEFORE the current event's snapshot — i.e. every call
    the runtime has made in the prior events of this test case. Used to give
    the judge cross-event context."""
    return [ex.call_log[:s] for ex, s in zip(mock_executors, snapshots)]


# ── Diagnostic logging helpers — enabled by --verbose DEBUG ────────────────────
def _serialize_plan(obj_id: str, plan, source: str) -> dict:
    return {
        "object_id": obj_id,
        "trace_id": plan.trace_id,
        "source": source,  # "active" or "completed"
        "goal": plan.goal,
        "status": plan.status,
        "steps": [{
            "id": s.id, "kind": s.kind, "target": s.target,
            "description": s.description, "status": s.status,
            "depends_on": list(s.depends_on or []),
            "result_kind": getattr(s, "result_kind", None),
            "wait_predicate": getattr(s, "wait_predicate", None),
            "wait_source": getattr(s, "wait_source", None),
            "retry_count": getattr(s, "retry_count", 0),
            "reactive_replan_count": getattr(s, "reactive_replan_count", 0),
            "last_failure_reason": getattr(s, "last_failure_reason", None),
            "reactive_replan_for": getattr(s, "reactive_replan_for", None),
        } for s in (plan.steps or [])],
    }


def _collect_planner_plans(rt, processing_results) -> list[dict]:
    """Snapshot all plans (active + completed) for traces this event touched.

    Walks every object in the runtime — not just those in processing_results —
    and includes both `_active_plans` and `_completed_plans`. Filters to plans
    whose trace_id matches the source_trace_id of any processing_result in this
    event (so we capture each event's plans without polluting across events).

    Empty when no planner is enabled or the runtime exposes no objects.
    """
    if not hasattr(rt, "_bus"):
        return []
    # Trace ids touched in this event
    event_trace_ids: set = set()
    for pr in processing_results or []:
        tid = getattr(pr, "source_trace_id", None)
        if tid:
            event_trace_ids.add(tid)
    if not event_trace_ids:
        return []

    out: list[dict] = []
    seen: set = set()  # (object_id, trace_id, source) to dedupe
    try:
        objects = rt._bus.objects  # dict[str, LLMObject]
    except Exception:
        return []
    for oid, obj in objects.items():
        # Active plans
        try:
            active = dict(getattr(obj, "_active_plans", {}) or {})
        except Exception:
            active = {}
        for tid, plan in active.items():
            if tid not in event_trace_ids:
                continue
            key = (oid, tid, "active")
            if key in seen:
                continue
            seen.add(key)
            try:
                out.append(_serialize_plan(oid, plan, "active"))
            except Exception:
                pass
        # Completed plans (deque)
        try:
            completed = list(getattr(obj, "_completed_plans", []) or [])
        except Exception:
            completed = []
        for plan in completed:
            tid = getattr(plan, "trace_id", None)
            if not tid or tid not in event_trace_ids:
                continue
            key = (oid, tid, "completed")
            if key in seen:
                continue
            seen.add(key)
            try:
                out.append(_serialize_plan(oid, plan, "completed"))
            except Exception:
                pass
    return out


def _serialize_bus_messages(bus_msgs) -> list[dict]:
    """Convert bus_msgs (list of MessageLog) to dicts. Truncates content to 4000 chars/message."""
    out: list[dict] = []
    for ml in (bus_msgs or []):
        # bus_msgs are MessageLog objects; the actual Message is on .message
        m = getattr(ml, "message", ml)
        try:
            mt = m.type.value if hasattr(m.type, "value") else str(m.type)
        except Exception:
            mt = "?"
        out.append({
            "id":          getattr(m, "id", "") or "",
            "sender":      getattr(m, "sender", "") or "",
            "recipient":   getattr(m, "recipient", "") or "",
            "type":        mt,
            "trace_id":    getattr(m, "trace_id", None),
            "in_reply_to": getattr(m, "in_reply_to", None),
            "content":     ((getattr(m, "content", "") or "")[:4000]),
        })
    return out



def _run_test_case_timeline(
    tc: Sample,
    rt,
    gw,
    harness,
    timeout_s: Optional[float] = None,
    steps_only: bool = False,
    mock_executors: "list | None" = None,
    on_event_result=None,   # callable(EventResult, is_step: bool)
    on_mod_applied=None,    # callable(Modification)
    steps_snapshot: "Optional[StepsSnapshot]" = None,   # restore from this; skip step execution
    snapshot_out: "Optional[list]" = None,              # append StepsSnapshot here after steps
    concurrency: int = 0,       # number of concurrent events per group (0 = sequential)
    concurrency_seed: int = 42, # seed for sampling concurrent events from each group
    max_modifications: Optional[int] = None,  # limit to first N modifications (None = all)
    tracked_harness=None,
    log_planner_output: bool = False,  # persist per-event planner plans + outgoing messages
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Execute steps and timeline events against a live runtime."""
    event_results: list[EventResult] = []
    mod_results: list[ModificationResult] = []

    # Once any event in this TC hits a technical failure (content filter,
    # runtime exception, network error captured as cf_errors), all subsequent
    # events in the same TC are treated as infra-error too — they depend on
    # the world state the failed event would have produced, so their pass/fail
    # signal isn't a clean measurement of model behavior. Excluded from scoring.
    tc_had_infra_error: bool = False

    execs = mock_executors or []
    prior_context: str = ""

    # 2. Run steps — initialize state and assert default (no-modification) behavior.
    # When steps_snapshot is provided, skip execution and restore from snapshot instead.
    if steps_snapshot is not None:
        _restore_snapshot(rt, execs, steps_snapshot)
        event_results.extend(steps_snapshot.step_event_results)
        prior_context = steps_snapshot.prior_context
        if on_event_result:
            for ev in steps_snapshot.step_event_results:
                on_event_result(ev, True)

    base_events = [e for e in tc.events if e.role == "base"]
    for step in base_events:
        if steps_snapshot is not None:
            break  # already restored above
        log_snapshot = len(rt.message_log)
        exec_snap = _snapshot_logs(execs)
        t0 = time.monotonic()
        step_payload = json.dumps({"system": step.source, "content": (
            f"[scenario time: {step.when}] {step.input}" if getattr(step, "when", None) else step.input),
            "timestamp": getattr(step, "when", None)})
        results, timed_out = _run_with_timeout(
            lambda s=step, p=step_payload: gw.dispatch(s.recipient, p, source=s.source), timeout_s,
        )
        latency_ms = (time.monotonic() - t0) * 1000

        if step.expect is not None:
            if timed_out:
                event_results.append(EventResult(
                    event_id=step.id,
                    passed=False,
                    reasoning=f"Timeout after {timeout_s}s",
                    expected=step.expect.action,
                    latency_ms=latency_ms,
                ))
                if on_event_result:
                    on_event_result(event_results[-1], True)
            else:
                in_tok = sum(r.metrics.input_tokens for r in results if r.metrics)
                out_tok = sum(r.metrics.output_tokens for r in results if r.metrics)
                planner_in_tok   = sum(r.planner_metrics.input_tokens   for r in results if r.planner_metrics)
                planner_out_tok  = sum(r.planner_metrics.output_tokens  for r in results if r.planner_metrics)
                executor_in_tok  = sum(r.executor_metrics.input_tokens  for r in results if r.executor_metrics)
                executor_out_tok = sum(r.executor_metrics.output_tokens for r in results if r.executor_metrics)
                executor_calls   = sum(r.executor_cycles for r in results if r.executor_cycles)
                executor_retries = sum(max(0, r.executor_cycles - 1) for r in results if r.executor_cycles)
                evaluator_in_tok  = sum(r.evaluator_metrics.input_tokens  for r in results if r.evaluator_metrics)
                evaluator_out_tok = sum(r.evaluator_metrics.output_tokens for r in results if r.evaluator_metrics)
                bus_msgs = rt.message_log[log_snapshot:]
                new_calls = _new_tool_calls(execs, exec_snap)
                cf_errors = rt.drain_infra_errors()
                # Mark infra-error if THIS event raised an error OR an earlier
                # event in the same TC already did (downstream events depend
                # on world state we can no longer trust).
                infra_error = bool(cf_errors) or tc_had_infra_error
                if infra_error:
                    tc_had_infra_error = True
                # NB: prior_tool_calls intentionally NOT threaded — adding a
                # cumulative PRIOR TOOL EXECUTIONS section to the judge
                # evidence regressed the random-30 mod eval (−10 base events
                # vs prior baseline). The cumulative log inflated the
                # judge's input enough to either confuse it or make it
                # stricter on later events. Helper retained in case a
                # different consumer wants the cross-event view.
                evidence = gather_evidence(rt, results, step.recipient, bus_messages=bus_msgs, tool_calls=new_calls)
                condition = step.expect.action
                passed, reasoning, votes, j_in_tok, j_out_tok = harness.evaluate_assertion(condition, evidence, prior_context)
                event_results.append(EventResult(
                    event_id=step.id,
                    passed=passed,
                    reasoning=reasoning,
                    expected=condition,
                    evidence=evidence,
                    prior_context=prior_context,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    planner_input_tokens=planner_in_tok,
                    planner_output_tokens=planner_out_tok,
                    executor_input_tokens=executor_in_tok,
                    executor_output_tokens=executor_out_tok,
                    executor_calls=executor_calls,
                    executor_retries=executor_retries,
                    evaluator_input_tokens=evaluator_in_tok,
                    evaluator_output_tokens=evaluator_out_tok,
                    latency_ms=latency_ms,
                    judge_input_tokens=j_in_tok,
                    judge_output_tokens=j_out_tok,
                    judge_votes=votes,
                    infra_error=infra_error,
                    mock_tool_calls=sum(len(per_ex) for per_ex in new_calls),
                    planner_plans=(_collect_planner_plans(rt, results) if log_planner_output else []),
                    outgoing_messages=(_serialize_bus_messages(bus_msgs) if log_planner_output else []),
                ))
                if on_event_result:
                    on_event_result(event_results[-1], True)
        prior_context = _format_prior_state(rt)
        if tc_had_infra_error:
            break

    # Capture snapshot after steps complete (first TC in a sample group)
    if snapshot_out is not None and steps_snapshot is None:
        snapshot_out.append(_take_snapshot(rt, execs, list(event_results), prior_context))

    if steps_only or tc_had_infra_error:
        return event_results, mod_results

    # Apply --modifications N: limit to first N modifications and filter events.
    active_mods = tc.modifications[:max_modifications] if max_modifications is not None else tc.modifications
    allowed_mod_ids: set[str] = {m.id for m in active_mods}

    def _event_in_scope(evt) -> bool:
        """Event is in scope if all its after_mod_ids refer to allowed mods."""
        return all(mid in allowed_mod_ids for mid in (evt.after_mod_ids or []))

    # Build concurrent group map: group_name → ordered list of events.
    # Concurrent events are excluded from the main timeline and fired as batches
    # around their associated modification (when concurrency > 0).
    group_map: dict[str, list] = {}
    concurrent_event_ids: set[str] = set()
    if concurrency > 0:
        for evt in tc.events:
            if evt.role == "base":
                continue
            if evt.concurrent_group and _event_in_scope(evt):
                group_map.setdefault(evt.concurrent_group, []).append(evt)
                concurrent_event_ids.add(evt.id)

    # 3. Build sorted timeline: tag each item with its type and when-ordinal.
    # Concurrent-group events are excluded from the main timeline — dispatched as batches.
    # All other events (including those with triggered_by) are placed in the static timeline.
    timeline: list[tuple[int, str, object]] = []
    for mod in active_mods:
        timeline.append((parse_when(mod.when), "mod", mod))
    for evt in tc.events:
        if evt.role == "base":
            continue
        if evt.id not in concurrent_event_ids and _event_in_scope(evt):
            timeline.append((parse_when(evt.when), "event", evt))
    timeline.sort(key=lambda x: x[0])

    def _dispatch_event(evt) -> tuple[list, bool, float]:
        """Dispatch a single event; return (results, timed_out, latency_ms)."""
        log_snap = len(rt.message_log)
        exec_s = _snapshot_logs(execs)
        t0 = time.monotonic()
        # The scenario clock travels WITH the event: agents otherwise have no way to
        # know the scenario date and stamp real wall-clock time, which breaks every
        # date-keyed decision (daily caps, rolling windows, mod expiry) — while the
        # judge grades against scenario timestamps. Same W-notation as mods/expects.
        stamped = f"[scenario time: {evt.when}] {evt.input}" if getattr(evt, "when", None) else evt.input
        if evt.call_type == "send_event":
            payload = json.dumps({"system": evt.source, "content": stamped, "timestamp": evt.when})
            res, tout = _run_with_timeout(
                lambda e=evt, p=payload: gw.dispatch(e.recipient, p, source=e.source),
                timeout_s,
            )
        else:
            res, tout = _run_with_timeout(
                lambda e=evt, m=stamped: rt.send(e.recipient, m, sender=e.source),
                timeout_s,
            )
        if tout:
            _quiesce_after_timeout(rt, max_wait_s=min(timeout_s or 120.0, 240.0))
        lat = (time.monotonic() - t0) * 1000
        return res, tout, lat, log_snap, exec_s

    def _record_event_result(evt, res, timed_out, lat_ms, log_snap, exec_s, ctx: str = "",
                             _in_tok: int = None, _out_tok: int = None,
                             _planner_in_tok: int = None, _planner_out_tok: int = None,
                             _executor_in_tok: int = None, _executor_out_tok: int = None,
                             _evaluator_in_tok: int = None, _evaluator_out_tok: int = None):
        nonlocal tc_had_infra_error
        if evt.expect is None:
            return  # background event — no judgment
        active_harness = (
            tracked_harness
            if tracked_harness is not None and getattr(evt, "role", None) == "irrelevant"
            else harness
        )
        if timed_out:
            bus_msgs_to = rt.message_log[log_snap:]
            trace_spans_to, trace_root_to = build_event_trace(bus_msgs_to)
            # A timeout is itself a technical failure — flag it as infra_error
            # and propagate to subsequent events in this TC. (Treating timeouts
            # as infra_error preserves the model's measured behavior on TCs
            # that don't hit infrastructure problems.)
            tc_had_infra_error = True
            event_results.append(EventResult(
                event_id=evt.id,
                passed=False,
                reasoning=f"Timeout after {timeout_s}s",
                expected=evt.expect.action,
                prior_context=ctx,
                role=getattr(evt, "role", None),
                latency_ms=lat_ms,
                trace=trace_spans_to,
                trace_root_id=trace_root_to,
                infra_error=True,
            ))
        else:
            cf_errors = rt.drain_infra_errors()
            # Propagate any prior infra error in this TC; record new ones.
            infra_error = bool(cf_errors) or tc_had_infra_error
            if infra_error:
                tc_had_infra_error = True
            in_tok  = _in_tok  if _in_tok  is not None else sum(r.metrics.input_tokens  for r in res if r.metrics)
            out_tok = _out_tok if _out_tok is not None else sum(r.metrics.output_tokens for r in res if r.metrics)
            planner_in_tok   = _planner_in_tok   if _planner_in_tok   is not None else sum(r.planner_metrics.input_tokens   for r in res if r.planner_metrics)
            planner_out_tok  = _planner_out_tok  if _planner_out_tok  is not None else sum(r.planner_metrics.output_tokens  for r in res if r.planner_metrics)
            executor_in_tok  = _executor_in_tok  if _executor_in_tok  is not None else sum(r.executor_metrics.input_tokens  for r in res if r.executor_metrics)
            executor_out_tok = _executor_out_tok if _executor_out_tok is not None else sum(r.executor_metrics.output_tokens for r in res if r.executor_metrics)
            executor_calls   = sum(r.executor_cycles for r in res if r.executor_cycles)
            executor_retries = sum(max(0, r.executor_cycles - 1) for r in res if r.executor_cycles)
            evaluator_in_tok  = _evaluator_in_tok  if _evaluator_in_tok  is not None else sum(r.evaluator_metrics.input_tokens  for r in res if r.evaluator_metrics)
            evaluator_out_tok = _evaluator_out_tok if _evaluator_out_tok is not None else sum(r.evaluator_metrics.output_tokens for r in res if r.evaluator_metrics)
            bus_msgs = rt.message_log[log_snap:]
            new_calls = _new_tool_calls(execs, exec_s)
            if active_harness is tracked_harness:
                # Memory-fidelity judge: only object states, no tool calls / bus traffic
                evidence = gather_evidence(rt, res, evt.recipient)
            else:
                # See companion comment in the steps loop — prior_tool_calls
                # not threaded; the cumulative section regressed the eval.
                evidence = gather_evidence(rt, res, evt.recipient, bus_messages=bus_msgs, tool_calls=new_calls)
            passed, reasoning, votes, j_in_tok, j_out_tok = active_harness.evaluate_assertion(evt.expect.action, evidence, ctx)
            trace_spans, trace_root = build_event_trace(bus_msgs)
            event_results.append(EventResult(
                event_id=evt.id,
                passed=passed,
                reasoning=reasoning,
                expected=evt.expect.action,
                evidence=evidence,
                prior_context=ctx,
                role=getattr(evt, "role", None),
                input_tokens=in_tok,
                output_tokens=out_tok,
                planner_input_tokens=planner_in_tok,
                planner_output_tokens=planner_out_tok,
                executor_input_tokens=executor_in_tok,
                executor_output_tokens=executor_out_tok,
                executor_calls=executor_calls,
                executor_retries=executor_retries,
                evaluator_input_tokens=evaluator_in_tok,
                evaluator_output_tokens=evaluator_out_tok,
                latency_ms=lat_ms,
                judge_input_tokens=j_in_tok,
                judge_output_tokens=j_out_tok,
                judge_votes=votes,
                infra_error=infra_error,
                mock_tool_calls=sum(len(per_ex) for per_ex in new_calls),
                trace=trace_spans,
                trace_root_id=trace_root,
                planner_plans=(_collect_planner_plans(rt, res) if log_planner_output else []),
                outgoing_messages=(_serialize_bus_messages(bus_msgs) if log_planner_output else []),
            ))

    def _dispatch_concurrent_group(group: list, ctx: str, group_key: str = "") -> None:
        """Dispatch a sampled subset of a concurrent group in one transaction.

        Workflows `concurrency` events from `group` using a deterministic seed
        derived from the base seed, TC id, and group name — so the same C events
        are always chosen for a given (TC, seed, group) triple.
        """
        if not group:
            return
        import random as _random
        if concurrency >= len(group):
            batch = list(group)
        else:
            rng = _random.Random(f"{concurrency_seed}:{tc.id}:{group_key}")
            batch = rng.sample(group, concurrency)
        log_snap = len(rt.message_log)
        exec_s = _snapshot_logs(execs)
        t0 = time.monotonic()

        # Per-event latency: on_result fires (in delivery order) once per input
        # message as each drain iteration completes. Cascades are filtered out by
        # source_message_id inside send_many/dispatch_many.
        evt_latencies: list[float] = []
        def _record_lat(_result) -> None:
            evt_latencies.append((time.monotonic() - t0) * 1000)

        # Scenario clock travels with the event (same stamping as _dispatch_event) —
        # this group path is the one actually taken for timeline events.
        def _stamp(e) -> str:
            return f"[scenario time: {e.when}] {e.input}" if getattr(e, "when", None) else e.input
        if all(e.call_type == "send_event" for e in batch):
            items = [
                (e.recipient, json.dumps({"system": e.source, "content": _stamp(e), "timestamp": e.when}), e.source)
                for e in batch
            ]
            res, tout = _run_with_timeout(
                lambda its=items: gw.dispatch_many(its, on_result=_record_lat), timeout_s
            )
        else:
            items = [(e.recipient, _stamp(e), e.source) for e in batch]
            res, tout = _run_with_timeout(
                lambda its=items: rt.send_many(its, on_result=_record_lat), timeout_s
            )

        lat_ms = (time.monotonic() - t0) * 1000
        new_calls = _new_tool_calls(execs, exec_s)

        # Divide batch token totals equally across events: the combined res list
        # contains results from all N events, so summing it per-event would
        # over-attribute by N×.
        n = len(batch)
        batch_in_tok  = sum(r.metrics.input_tokens  for r in (res or []) if r.metrics)
        batch_out_tok = sum(r.metrics.output_tokens for r in (res or []) if r.metrics)
        batch_planner_in_tok   = sum(r.planner_metrics.input_tokens   for r in (res or []) if r.planner_metrics)
        batch_planner_out_tok  = sum(r.planner_metrics.output_tokens  for r in (res or []) if r.planner_metrics)
        batch_executor_in_tok  = sum(r.executor_metrics.input_tokens  for r in (res or []) if r.executor_metrics)
        batch_executor_out_tok = sum(r.executor_metrics.output_tokens for r in (res or []) if r.executor_metrics)
        batch_evaluator_in_tok  = sum(r.evaluator_metrics.input_tokens  for r in (res or []) if r.evaluator_metrics)
        batch_evaluator_out_tok = sum(r.evaluator_metrics.output_tokens for r in (res or []) if r.evaluator_metrics)

        for i, evt in enumerate(batch):
            evt_lat = evt_latencies[i] if i < len(evt_latencies) else lat_ms
            evt_ctx = _build_event_ctx(evt, active_mods, ctx)
            _record_event_result(evt, res or [], tout, evt_lat, log_snap, exec_s, evt_ctx,
                                 _in_tok=batch_in_tok // n, _out_tok=batch_out_tok // n,
                                 _planner_in_tok=batch_planner_in_tok // n, _planner_out_tok=batch_planner_out_tok // n,
                                 _executor_in_tok=batch_executor_in_tok // n, _executor_out_tok=batch_executor_out_tok // n,
                                 _evaluator_in_tok=batch_evaluator_in_tok // n, _evaluator_out_tok=batch_evaluator_out_tok // n)
            if on_event_result:
                on_event_result(event_results[-1], False)

    for _, kind, item in timeline:
        if kind == "mod":
            if on_mod_applied:
                on_mod_applied(item)

            # pre-mod concurrent group fires before the modification
            if concurrency > 0:
                pre_key = f"cgroup_pre_{item.id}"
                pre_group = group_map.get(pre_key, [])
                _dispatch_concurrent_group(pre_group, prior_context, group_key=pre_key)
                prior_context = _format_prior_state(rt)

            t0 = time.monotonic()
            results, timed_out = _run_with_timeout(
                # Modifications now route through the dedicated admin path so
                # the LLM can actually patch the target's definition (role /
                # behavior / peers / skills). DOMAIN messages no longer carry
                # `updated_definition`, so a modification sent via rt.send
                # would only update state — never the definition itself.
                lambda it=item: rt.send_admin(
                    it.target,
                    (f"[scenario time: {it.when}] {it.intent}" if getattr(it, "when", None) else it.intent),
                    sender=it.source),
                timeout_s,
            )
            latency_ms = (time.monotonic() - t0) * 1000
            if timed_out:
                mod_results.append(ModificationResult(mod_id=item.id, latency_ms=latency_ms))
            else:
                in_tok = sum(r.metrics.input_tokens for r in results if r.metrics)
                out_tok = sum(r.metrics.output_tokens for r in results if r.metrics)
                planner_in_tok   = sum(r.planner_metrics.input_tokens   for r in results if r.planner_metrics)
                planner_out_tok  = sum(r.planner_metrics.output_tokens  for r in results if r.planner_metrics)
                executor_in_tok  = sum(r.executor_metrics.input_tokens  for r in results if r.executor_metrics)
                executor_out_tok = sum(r.executor_metrics.output_tokens for r in results if r.executor_metrics)
                executor_calls   = sum(r.executor_cycles for r in results if r.executor_cycles)
                executor_retries = sum(max(0, r.executor_cycles - 1) for r in results if r.executor_cycles)
                evaluator_in_tok  = sum(r.evaluator_metrics.input_tokens  for r in results if r.evaluator_metrics)
                evaluator_out_tok = sum(r.evaluator_metrics.output_tokens for r in results if r.evaluator_metrics)
                mod_results.append(ModificationResult(
                    mod_id=item.id,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    planner_input_tokens=planner_in_tok,
                    planner_output_tokens=planner_out_tok,
                    executor_input_tokens=executor_in_tok,
                    executor_output_tokens=executor_out_tok,
                    executor_calls=executor_calls,
                    executor_retries=executor_retries,
                    evaluator_input_tokens=evaluator_in_tok,
                    evaluator_output_tokens=evaluator_out_tok,
                    latency_ms=latency_ms,
                ))
            prior_context = _format_prior_state(rt)

            # post-mod concurrent group fires after the modification settles
            if concurrency > 0:
                post_key = f"cgroup_post_{item.id}"
                post_group = group_map.get(post_key, [])
                _dispatch_concurrent_group(post_group, prior_context, group_key=post_key)
                prior_context = _format_prior_state(rt)

        else:  # event
            res, tout, lat, log_snap, exec_s = _dispatch_event(item)
            if item.expect is not None:
                ctx = _build_event_ctx(item, active_mods, prior_context)
                _record_event_result(item, res, tout, lat, log_snap, exec_s, ctx)
                if on_event_result:
                    on_event_result(event_results[-1], False)
            prior_context = _format_prior_state(rt)
        if tc_had_infra_error:
            break

    return event_results, mod_results


def execute_test_case(
    tc: Sample,
    brain,
    harness,
    timeout_s: Optional[float] = INTER_EVENT_TIMEOUT_S,
    debug_messages: bool = False,
    steps_only: bool = False,
    max_chain_depth: int = 20,
    max_tool_rounds: int = 10,
    global_mock_tools: "list[MockToolDef] | None" = None,
    progress_callback=None,
    on_event_result=None,
    on_mod_applied=None,
    steps_snapshot: "Optional[StepsSnapshot]" = None,
    snapshot_out: "Optional[list]" = None,
    concurrency: int = 0,
    concurrency_seed: int = 42,
    max_modifications: Optional[int] = None,
    object_prompt: Optional[str] = None,
    planner_prompt: str = "planner_sequential.yaml",
    max_history: Optional[int] = None,
    tracked_harness=None,
    enable_code_tool: bool = True,
    enable_sink_completion_shim: bool = False,
    enable_planner: bool = True,
    enable_evaluator: bool = True,
    planner_brain=None,
    evaluator_brain=None,
    mock_server_url: "str | None" = None,
    mock_slot_id: str = "default",
    memory_backend: str = "nested",
    log_planner_output: bool = False,
    tool_dispatch: str = "sync",
    planner_mode: str = "dag",
    enable_replan_checkpoints: bool = False,
    replan_max_per_trace: int = 3,
    enable_step_retry_replan: bool = True,
    step_max_retries: int = 2,
    step_replan_max: int = 1,
    reactive_replan_max_per_trace: int = 4,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Run a single Sample with a per-event timeout (seconds).

    Each step, modification, and event gets its own timeout. If a single
    step times out, it is marked as failed and execution continues.

    Args:
        global_mock_tools: Shared mock tool definitions loaded from --mock-config files.
            Merged with tc.mock_tools; per-Sample entries override shared ones.
        progress_callback: Optional callable(msg) invoked on every bus message delivery.
        on_event_result: Optional callable(EventResult, is_step: bool) for real-time display.
        on_mod_applied: Optional callable(Modification) called before each modification runs.
        concurrency: Number of concurrent events to fire per group (0 = sequential).
        concurrency_seed: Seed for sampling concurrent events from each group (default 42).
        max_modifications: Limit evaluation to the first N modifications (None = all).
        object_prompt: Object system-prompt template filename (relative to config/prompts/lnl/).
        max_history: Override conversation history window per object (None = use default 6).
    """
    return _execute_test_case_inner(
        tc, brain, harness,
        debug_messages=debug_messages,
        timeout_s=timeout_s,
        steps_only=steps_only,
        max_chain_depth=max_chain_depth,
        max_tool_rounds=max_tool_rounds,
        global_mock_tools=global_mock_tools,
        progress_callback=progress_callback,
        on_event_result=on_event_result,
        on_mod_applied=on_mod_applied,
        steps_snapshot=steps_snapshot,
        snapshot_out=snapshot_out,
        concurrency=concurrency,
        concurrency_seed=concurrency_seed,
        max_modifications=max_modifications,
        object_prompt=object_prompt,
        planner_prompt=planner_prompt,
        max_history=max_history,
        tracked_harness=tracked_harness,
        enable_code_tool=enable_code_tool,
        enable_sink_completion_shim=enable_sink_completion_shim,
        enable_planner=enable_planner,
        enable_evaluator=enable_evaluator,
        planner_brain=planner_brain,
        evaluator_brain=evaluator_brain,
        mock_server_url=mock_server_url,
        mock_slot_id=mock_slot_id,
        memory_backend=memory_backend,
        log_planner_output=log_planner_output,
        tool_dispatch=tool_dispatch,
        planner_mode=planner_mode,
        enable_replan_checkpoints=enable_replan_checkpoints,
        replan_max_per_trace=replan_max_per_trace,
        enable_step_retry_replan=enable_step_retry_replan,
        step_max_retries=step_max_retries,
        step_replan_max=step_replan_max,
        reactive_replan_max_per_trace=reactive_replan_max_per_trace,
    )


# ── Output path ────────────────────────────────────────────────────────────────

def default_output_path(input_path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    repo_root = Path(__file__).parent.parent.parent
    outputs_root = repo_root / "outputs"
    # If input is already under outputs/, keep it there; otherwise mirror under outputs/
    try:
        rel = input_path.resolve().relative_to(outputs_root.resolve())
        base = outputs_root / rel.parent
    except ValueError:
        try:
            rel = input_path.resolve().relative_to(repo_root.resolve())
            base = outputs_root / rel.parent
        except ValueError:
            base = input_path.parent
    return base / "runs" / f"{input_path.stem}_eval_{ts}.jsonl"



def _role_elapsed_fields(events: list) -> dict:
    """Sum latency_ms per role from a list of EventResult objects."""
    def _sum(role):
        evts = [e for e in events if e.role == role]
        return sum(e.latency_ms for e in evts) if evts else None
    return dict(
        base_elapsed_ms=_sum(None),
        pre_mod_elapsed_ms=_sum("pre_mod"),
        post_mod_elapsed_ms=_sum("post_mod"),
        irrelevant_elapsed_ms=_sum("irrelevant"),
    )


# ── Verbose output ────────────────────────────────────────────────────────────

def _print_event_result(ev, show_evidence: bool = False) -> None:
    """Print a single event result. Evidence is optional (verbose mode only)."""
    status = "PASS" if ev.passed else "FAIL"
    tqdm.write(f"    [{status}] {ev.event_id}")
    tqdm.write(f"      Expected: {ev.expected}")
    if show_evidence and ev.evidence:
        indented = ev.evidence.replace("\n", "\n        ")
        tqdm.write(f"      Evidence: {indented}")
    tqdm.write(f"      Judge:    {ev.reasoning}")
    tqdm.write("")


def _print_verbose(tc_result: SampleResult, show_evidence: bool = False) -> None:
    """Print per-event breakdown to console."""
    for ev in tc_result.events:
        _print_event_result(ev, show_evidence=show_evidence)


# ── Pricing ────────────────────────────────────────────────────────────────────

# (input $/1M, output $/1M)
_MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gpt-5.4-mini": (0.75, 1.5),
    "gpt-5.4":      (2.5,  5.0),
}


def _compute_cost(in_tok: int, out_tok: int, model: str) -> Optional[float]:
    prices = _MODEL_PRICES.get(model)
    if prices is None:
        return None
    return in_tok / 1_000_000 * prices[0] + out_tok / 1_000_000 * prices[1]


# ── Main runner ────────────────────────────────────────────────────────────────

def _print_summary(summary, output_path=None, elapsed_s=None,
                   agent_model: str = None, judge_model: str = None) -> None:
    """Print a human-readable summary of evaluation results."""
    def _fmt(v) -> str:
        return f"{v:.4f}" if v is not None else "N/A"

    def _fmts(v, me) -> str:
        return f"{_fmt(v)}  ±ME: {_fmt(me)}"

    has_inconclusive = summary.inconclusive_tcs > 0

    def _fmt_mod(conclusive, conclusive_me, all_val, all_me) -> str:
        if not has_inconclusive:
            return _fmts(conclusive, conclusive_me)
        return f"{_fmts(all_val, all_me)}  (conclusive only: {_fmts(conclusive, conclusive_me)}, {summary.inconclusive_tcs} inconclusive excluded)"

    if output_path:
        print(f"Complete. Output: {output_path}")
    if elapsed_s is not None:
        h = int(elapsed_s) // 3600
        m = (int(elapsed_s) % 3600) // 60
        s = int(elapsed_s) % 60
        ms = int((elapsed_s % 1) * 1000)
        elapsed_str = f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}" if h else f"{m:02d}:{s:02d}.{ms:03d}"
        print(f"Elapsed:             {elapsed_str}")
    print(f"Mean pass rate:      {_fmts(summary.mean_pass_rate, summary.pass_rate_ci95)}")
    print(f"Steps pass rate:     {_fmts(summary.steps_pass_rate, summary.steps_pass_rate_ci95)}")
    print(f"Workflows completion:  {_fmts(summary.samples_completion, summary.samples_completion_ci95)}")
    print(f"Mod pass rate:       {_fmt_mod(summary.mod_pass_rate, summary.mod_pass_rate_ci95, summary.mod_pass_rate_all, summary.mod_pass_rate_all_ci95)}  (pre+post+irrelevant)")
    print(f"  Pre-mod:           {_fmt_mod(summary.pre_mod_pass_rate, summary.pre_mod_pass_rate_ci95, summary.pre_mod_pass_rate_all, summary.pre_mod_pass_rate_all_ci95)}")
    print(f"  Post-mod:          {_fmt_mod(summary.post_mod_pass_rate, summary.post_mod_pass_rate_ci95, summary.post_mod_pass_rate_all, summary.post_mod_pass_rate_all_ci95)}")
    print(f"  Irrelevant:        {_fmt_mod(summary.irrelevant_pass_rate, summary.irrelevant_pass_rate_ci95, summary.irrelevant_pass_rate_all, summary.irrelevant_pass_rate_all_ci95)}")
    print(f"Inconclusive TCs:    {summary.inconclusive_tcs}")
    if summary.infra_error_tcs:
        print(f"Infra-error TCs:     {summary.infra_error_tcs}  (excluded from scores — content filter or similar)")
    def _fmt_ms(v) -> str:
        return f"{v:.0f}ms" if v is not None else "N/A"
    print(f"Mean event latency:  {_fmt_ms(summary.mean_event_latency_ms)}"
          f"  (base: {_fmt_ms(summary.mean_base_event_latency_ms)}"
          f"  pre: {_fmt_ms(summary.mean_pre_mod_event_latency_ms)}"
          f"  post: {_fmt_ms(summary.mean_post_mod_event_latency_ms)}"
          f"  irrel: {_fmt_ms(summary.mean_irrelevant_event_latency_ms)})")
    n_events = summary.total_events or 1
    print(f"Agent tokens:        {summary.total_agent_input_tokens:,} in / {summary.total_agent_output_tokens:,} out"
          f"  (mean/event: {summary.mean_event_input_tokens:.0f} in / {summary.mean_event_output_tokens:.0f} out)")
    print(f"  executor:          {summary.total_executor_input_tokens:,} in / {summary.total_executor_output_tokens:,} out")
    if summary.total_executor_calls:
        print(
            f"  executor cycles:   {summary.total_executor_calls:,} calls / "
            f"{summary.total_executor_retries:,} retries "
            f"(mean/event: {summary.mean_executor_calls_per_event:.2f} calls / "
            f"{summary.mean_executor_retries_per_event:.2f} retries)"
        )
    if summary.total_planner_input_tokens or summary.total_planner_output_tokens:
        print(f"  planner:           {summary.total_planner_input_tokens:,} in / {summary.total_planner_output_tokens:,} out")
    if summary.total_evaluator_input_tokens or summary.total_evaluator_output_tokens:
        print(f"  evaluator:         {summary.total_evaluator_input_tokens:,} in / {summary.total_evaluator_output_tokens:,} out")
    print(f"Judge tokens:        {summary.total_judge_input_tokens:,} in / {summary.total_judge_output_tokens:,} out"
          f"  (mean/event: {summary.total_judge_input_tokens/n_events:.0f} in / {summary.total_judge_output_tokens/n_events:.0f} out)")
    agent_cost = _compute_cost(summary.total_agent_input_tokens, summary.total_agent_output_tokens, agent_model or "")
    judge_cost = _compute_cost(summary.total_judge_input_tokens, summary.total_judge_output_tokens, judge_model or "")
    if agent_cost is not None or judge_cost is not None:
        agent_str = f"${agent_cost:.2f}" if agent_cost is not None else "unknown model"
        judge_str = f"${judge_cost:.2f}" if judge_cost is not None else "unknown model"
        total = (agent_cost or 0) + (judge_cost or 0)
        print(f"Cost:                ${total:.2f} total  "
              f"(agent: {agent_str}  judge: {judge_str})")


def _warn_continuation_mismatch(output_path: Path, args: argparse.Namespace) -> None:
    """Read the original run_config from an existing results file and warn if key
    params differ from the current invocation. Runs only when continuing a run."""
    GUARDED = [
        ("model",         lambda a: getattr(a, "model", None)),
        ("judge_model",   lambda a: getattr(a, "judge_model", None) or getattr(a, "model", None)),
        ("runs",          lambda a: getattr(a, "runs", None)),
        ("concurrency",   lambda a: getattr(a, "concurrency", None)),
        ("modifications", lambda a: getattr(a, "modifications", None)),
        ("seed",          lambda a: getattr(a, "seed", None)),
        ("limit",         lambda a: getattr(a, "limit", None)),
        ("timeout_s",     lambda a: getattr(a, "timeout", None)),
    ]
    try:
        with open(output_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if d.get("record_type") == "run_config":
                    mismatches = []
                    for field, get_current in GUARDED:
                        orig = d.get(field)
                        curr = get_current(args)
                        if orig is not None and curr is not None and orig != curr:
                            mismatches.append(f"  {field}: original={orig!r}  current={curr!r}")
                    if mismatches:
                        print("\n⚠️  WARNING: continuation params differ from original run:")
                        for m in mismatches:
                            print(m)
                        ans = input("\nContinue anyway? [y/N] ").strip().lower()
                        if ans != "y":
                            raise SystemExit("Aborted by user.")
                        print()
                    break
    except SystemExit:
        raise
    except Exception:
        pass


def run(args: argparse.Namespace) -> Path:
    """Run evaluation. Returns the output path."""
    eval_start = time.monotonic()
    logging.basicConfig(level=logging.WARNING)

    if args.output is None:
        args.output = default_output_path(args.input)

    print(f"Output: {args.output}")

    if args.provider is None:
        args.provider = infer_provider(args.model)

    if not args.input.exists():
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    test_cases = load_jsonl(args.input, Sample)

    if getattr(args, "tc", None):
        # Select test cases by 1-based index, ID, or ID[mod_type] (e.g. TC001[temporal]).
        # Preserve order of appearance in file.
        def _tc_mod_type(tc: Sample) -> str:
            return tc.modifications[0].mod_type.value if tc.modifications else "none"

        selected: list[Sample] = []
        for selector in args.tc:
            if selector.isdigit():
                idx = int(selector) - 1
                if idx < 0 or idx >= len(test_cases):
                    print(f"Error: --tc {selector} out of range (file has {len(test_cases)} test cases)", file=sys.stderr)
                    sys.exit(1)
                selected.append(test_cases[idx])
            elif "[" in selector and selector.endswith("]"):
                tc_id, mod_type = selector[:-1].rsplit("[", 1)
                matched = [tc for tc in test_cases if tc.id == tc_id and _tc_mod_type(tc) == mod_type]
                if not matched:
                    print(f"Error: --tc {selector!r} not found. Use ID[mod_type] e.g. TC001[temporal].", file=sys.stderr)
                    sys.exit(1)
                selected.extend(matched)
            else:
                # Match by TC ID, or by sample_id (selects all TCs sharing that sample)
                matched = [tc for tc in test_cases if tc.id == selector or tc.sample_id == selector]
                if not matched:
                    print(f"Error: --tc {selector!r} not found. Available IDs: {[tc.id for tc in test_cases[:5]]}...", file=sys.stderr)
                    sys.exit(1)
                selected.extend(matched)
        test_cases = selected
    elif getattr(args, "sample", None):
        import random as _random
        _rng = _random.Random(getattr(args, "sample_seed", None))
        test_cases = _rng.sample(test_cases, min(args.sample, len(test_cases)))
    elif args.limit:
        test_cases = test_cases[: args.limit]

    # When running steps-only (no modifications), deduplicate by sample_id so
    # test cases sharing the same base steps are only executed once.
    if getattr(args, "steps_only", False):
        seen_samples: set[str] = set()
        deduped: list[Sample] = []
        for tc in test_cases:
            key = tc.sample_id or tc.id
            if key not in seen_samples:
                seen_samples.add(key)
                deduped.append(tc)
        if len(deduped) < len(test_cases):
            print(
                f"  Steps-only mode: deduplicating by sample_id "
                f"({len(test_cases)} → {len(deduped)} test cases)"
            )
        test_cases = deduped

    timeout_s: Optional[float] = getattr(args, "timeout", None)

    print(f"evaluate {_VERSION}")
    print(f"Loaded {len(test_cases)} test cases from {args.input}")
    # Build judge spec list — --llm-judge takes precedence; fall back to --judge-model / --model.
    # Each spec is "model" or "provider/model".
    def _parse_judge_spec(spec: str) -> tuple[str, str]:
        """Return (provider, model) for a judge spec."""
        if "/" in spec:
            provider, model = spec.split("/", 1)
        else:
            model = spec
            provider = infer_provider(model)
        return provider, model

    llm_judge_specs: list[str] = getattr(args, "llm_judge", None) or []
    if llm_judge_specs:
        parsed_judges = [_parse_judge_spec(s) for s in llm_judge_specs]
    elif getattr(args, "judge_model", None):
        jp = getattr(args, "judge_provider", None) or infer_provider(args.judge_model)
        parsed_judges = [(jp, args.judge_model)]
    else:
        parsed_judges = [(args.provider, args.model)]

    # Use first judge for backward-compat RunConfig fields
    judge_provider, judge_model = parsed_judges[0]

    if len(parsed_judges) == 1:
        judge_label = f"{judge_provider}/{judge_model}"
    else:
        judge_label = f"panel({len(parsed_judges)}): " + ", ".join(
            f"{p}/{m}" for p, m in parsed_judges
        )

    object_prompt = getattr(args, "object_prompt", None) or "(backend default)"
    # Planner line — only shown when planner is enabled; otherwise "(disabled)".
    if getattr(args, "enable_planner", True):
        planner_provider = getattr(args, "planner_provider", None) or args.provider
        planner_model = getattr(args, "planner_model", None) or args.model
        planner_label = f"{planner_provider}/{planner_model}"
    else:
        planner_label = "(disabled)"
    # Evaluator line — only shown when evaluator is enabled.
    if getattr(args, "enable_evaluator", True):
        evaluator_provider = getattr(args, "evaluator_provider", None) or args.provider
        evaluator_model = getattr(args, "evaluator_model", None) or args.model
        evaluator_label = f"{evaluator_provider}/{evaluator_model}"
    else:
        evaluator_label = "(disabled)"
    planner_mode = getattr(args, "planner_mode", "dag")
    planner_prompt_display = getattr(args, "planner_prompt", "planner_sequential.yaml")
    if planner_mode == "dag" and planner_prompt_display == "planner_sequential.yaml":
        # Mode auto-selects planner_dag.yaml unless --planner-prompt was explicit.
        planner_prompt_display = "planner_dag.yaml"
    replan_enabled = getattr(args, "enable_replan_checkpoints", False)
    replan_label = (
        f"on (max={getattr(args, 'replan_max', 3)}/trace)" if replan_enabled else "off"
    )
    extra_info = {
        "Planner": planner_label,
        "Planner mode": planner_mode,
        "Replan checkpoints": replan_label,
        "Evaluator": evaluator_label,
        "Judge": judge_label,
        "Object prompt": object_prompt,
        "Planner prompt": planner_prompt_display,
        "Runs per test case": str(args.runs),
        "Timeout per event": f"{timeout_s}s" if timeout_s else "none",
    }
    print_run_info(
        args.provider,
        args.model,
        getattr(args, "seed", None),
        extra_info,
    )

    # Build LNL brain (for objects) and judge(s) (for assertions)
    seed: Optional[int] = getattr(args, "seed", None)

    def _make_brain(provider, model):
        if provider == "openai":
            from src.lnl.brain import OpenAIBrain
            return OpenAIBrain(model=model, seed=seed)
        elif provider == "azure":
            from src.lnl.brain import AzureBrain
            return AzureBrain(model=model)
        elif provider == "google":
            from src.lnl.brain import GeminiBrain
            return GeminiBrain(model=model)
        else:
            from src.lnl.brain import AnthropicBrain
            return AnthropicBrain(model=model, thinking=getattr(args, "thinking", None))

    def _make_judge(provider, model):
        if provider == "openai":
            from src.lnl.judge import OpenAIJudge
            return OpenAIJudge(model=model)
        elif provider == "azure":
            from src.lnl.judge import AzureJudge
            return AzureJudge(model=model)
        elif provider == "google":
            from src.lnl.judge import GeminiJudge
            return GeminiJudge(model=model)
        else:
            from src.lnl.judge import AnthropicJudge
            return AnthropicJudge(model=model)

    brain = _make_brain(args.provider, args.model)
    # Planner brain — defaults to agent brain when --planner-model is not set.
    planner_model = getattr(args, "planner_model", None) or args.model
    planner_provider = getattr(args, "planner_provider", None) or args.provider
    if planner_model == args.model and planner_provider == args.provider:
        planner_brain = brain  # share instance — same model+provider
    else:
        planner_brain = _make_brain(planner_provider, planner_model)
    args.planner_model_resolved = f"{planner_provider}/{planner_model}"
    # Evaluator brain — defaults to agent brain when --evaluator-model is not set.
    evaluator_model = getattr(args, "evaluator_model", None) or args.model
    evaluator_provider = getattr(args, "evaluator_provider", None) or args.provider
    if evaluator_model == args.model and evaluator_provider == args.provider:
        evaluator_brain = brain
    elif evaluator_model == planner_model and evaluator_provider == planner_provider:
        evaluator_brain = planner_brain  # reuse planner instance if same model
    else:
        evaluator_brain = _make_brain(evaluator_provider, evaluator_model)
    args.evaluator_model_resolved = f"{evaluator_provider}/{evaluator_model}"
    single_judges = [_make_judge(p, m) for p, m in parsed_judges]
    if len(single_judges) == 1:
        judge = single_judges[0]
    else:
        from src.lnl.judge import PanelJudge
        labels = [f"{p}/{m}" for p, m in parsed_judges]
        judge = PanelJudge(single_judges, judge_labels=labels)

    from src.lnl.benchmark import BenchmarkHarness
    harness = BenchmarkHarness(brain=brain, judge=judge)

    # Optional memory-fidelity judge for tracked events (probe-dataset TCs).
    tracked_harness: Optional["BenchmarkHarness"] = None
    tracked_judge_path = getattr(args, "tracked_judge", None)
    if tracked_judge_path:
        import yaml as _yaml
        tracked_prompt = _yaml.safe_load(Path(tracked_judge_path).read_text())["system_prompt"].strip()
        tracked_judge_inst = _make_judge(judge_provider, judge_model)
        tracked_judge_inst._system_prompt = tracked_prompt
        tracked_harness = BenchmarkHarness(brain=brain, judge=tracked_judge_inst)
        print(f"Tracked judge: {tracked_judge_path}")

    # Load shared mock tool configs from --mock-config files
    global_mock_tools: list[MockToolDef] = []
    for mc_path in getattr(args, "mock_config", None) or []:
        mc = load_mock_config(mc_path)
        global_mock_tools.extend(mc.tools)
        print(f"  Loaded mock config: {mc_path} ({len(mc.tools)} tools)")

    # Optional: start the HTTP mock server. When enabled, mock tool calls are
    # routed through POST /tool/{method} — the same path the OpenClaw baseline
    # uses — instead of the in-process executor. Per-(tc, run) slots isolate
    # concurrent workers.
    mock_server = None
    mock_server_url: Optional[str] = None
    if getattr(args, "mock_server", False):
        from src.data.mock_server import MockServer
        mock_port = getattr(args, "mock_server_port", 18888)
        mock_server = MockServer(port=mock_port)
        mock_server.start()
        mock_server.wait_ready()
        mock_server_url = f"http://127.0.0.1:{mock_port}"
        print(f"Mock server: enabled (port {mock_port}, script mode) — tool calls routed via HTTP")

    args.output.parent.mkdir(parents=True, exist_ok=True)

    # Continuation: if output file already exists, load completed runs and resume.
    # Key is (tc_index, run_index, seed). tc_index (0-based file position) is the
    # reliable unique identifier — tc_id is NOT unique (80 IDs × 6 mod-type variants).
    # Different seed values coexist in the same file; continuation is per-seed.
    # Legacy results (tc_index=-1) fall back to (tc_id, run_index) keying.
    completed: set[tuple[int, int, Optional[int]]] = set()  # (tc_index, run_index, seed)
    completed_legacy: set[tuple[str, int]] = set()
    all_tc_results: list[SampleResult] = []
    if args.output.exists():
        for line in args.output.read_text().splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                if "tc_id" in data and "run_index" in data:
                    tc_index = data.get("tc_index", -1)
                    run_index = data["run_index"]
                    result_seed = data.get("seed")  # None for legacy/unseeded
                    if tc_index >= 0:
                        completed.add((tc_index, run_index, result_seed))
                    else:
                        completed_legacy.add((data["tc_id"], run_index))
                    all_tc_results.append(SampleResult.model_validate(data))
            except Exception:
                pass
        n_done = len(completed) + len(completed_legacy)
        if n_done:
            print(f"Resuming: {n_done} runs already complete, continuing from checkpoint.")
            _warn_continuation_mismatch(args.output, args)
    file_mode = "a" if (completed or completed_legacy) else "w"

    total_runs = len(test_cases) * args.runs
    # Count only runs that will actually be skipped in this invocation
    n_skipped = sum(
        1
        for tc_idx, tc in enumerate(test_cases)
        for run_idx in range(args.runs)
        if (tc_idx, run_idx, seed) in completed
        or (tc.id, run_idx) in completed_legacy
    )

    workers: int = getattr(args, "workers", 1)
    write_lock = threading.Lock()

    # --reuse-steps: share runtime snapshots across variants of the same sample.
    # Key: (sample_id, run_idx). First TC in each group runs steps and stores the
    # snapshot; subsequent TCs restore from it and skip step execution.
    reuse_steps: bool = getattr(args, "reuse_steps", False)
    _snapshots: dict[tuple, StepsSnapshot] = {}
    _snapshot_events: dict[tuple, threading.Event] = {}
    _snapshot_registry_lock = threading.Lock()

    # Shared counters updated across all concurrent workers. Approximate (no lock) — display-only.
    # Seeded from already-completed results so continuation runs accumulate correctly.
    _event_counter: list[int] = [sum(len(r.events) for r in all_tc_results)]
    _in_tok_counter: list[int] = [sum(e.input_tokens or 0 for r in all_tc_results for e in r.events)]
    _out_tok_counter: list[int] = [sum(e.output_tokens or 0 for r in all_tc_results for e in r.events)]
    _pbar_holder: list = [None]       # set to the tqdm pbar once the loop starts

    def _run_one(tc_idx: int, tc: Sample, run_idx: int) -> Optional[SampleResult]:
        mod_type_str = tc.modifications[0].mod_type.value if tc.modifications else "none"
        label = f"{tc.id}[{mod_type_str}]"
        if args.runs > 1:
            label += f" run={run_idx+1}/{args.runs}"

        tqdm.write(f"\n{label}")
        msg_count = [0]
        tc_start = time.monotonic()

        # Resolve snapshot for --reuse-steps
        steps_snapshot: Optional[StepsSnapshot] = None
        snapshot_out: Optional[list] = None
        if reuse_steps and tc.sample_id:
            key = (tc.sample_id, run_idx)
            with _snapshot_registry_lock:
                if key in _snapshots:
                    steps_snapshot = _snapshots[key]
                elif key in _snapshot_events:
                    wait_event = _snapshot_events[key]
                else:
                    wait_event = None
                    new_event = threading.Event()
                    _snapshot_events[key] = new_event
                    snapshot_out = []  # this TC will capture the snapshot

            if steps_snapshot is None and snapshot_out is None:
                # Another worker is computing it — wait
                wait_event.wait()
                with _snapshot_registry_lock:
                    steps_snapshot = _snapshots.get(key)

        tc_log: list[str] = []  # buffered per-TC output, flushed atomically at end
        # At INFO/DEBUG with a single worker, stream results live instead of buffering.
        _live = workers == 1 and getattr(args, "verbose", "ERROR") in ("INFO", "DEBUG")

        def _emit(line: str) -> None:
            if _live:
                tqdm.write(line)
            else:
                tc_log.append(line)

        def _on_event_result(result: EventResult, is_step: bool, _args=args):
            tag = " (baseline)" if is_step else ""
            lat = f"  {result.latency_ms/1000:.1f}s" if result.latency_ms else ""
            if not result.passed:
                _emit(f"  {'✗'} {result.event_id}{tag}{lat}: {result.reasoning[:120]}")
            else:
                _emit(f"  {'✓'} {result.event_id}{tag}{lat}: {result.reasoning[:80]}")
            _event_counter[0] += 1
            _in_tok_counter[0] += result.input_tokens or 0
            _out_tok_counter[0] += result.output_tokens or 0
            if _pbar_holder[0] is not None:
                _pbar_postfix(_pbar_holder[0], all_tc_results, _event_counter[0], _in_tok_counter[0], _out_tok_counter[0], agent_model=args.model)

        def _on_mod_applied(mod, _tc=tc):
            _emit(
                f"  ── [{mod.mod_type.value}/{mod.ambiguity.value}] {mod.id}: "
                f"{mod.intent[:70]}"
            )

        def _on_message(_msg, _label=label, _count=msg_count, _start=tc_start):
            _count[0] += 1

        try:
            event_results, mod_results = execute_test_case(
                tc, brain, harness, timeout_s,
                debug_messages=getattr(args, "debug_messages", False),
                steps_only=getattr(args, "steps_only", False),
                max_chain_depth=args.max_chain_depth,
                max_tool_rounds=args.max_tool_rounds,
                global_mock_tools=global_mock_tools or None,
                progress_callback=_on_message,
                on_event_result=_on_event_result,
                on_mod_applied=_on_mod_applied,
                steps_snapshot=steps_snapshot,
                snapshot_out=snapshot_out,
                concurrency=getattr(args, "concurrency", 0),
                concurrency_seed=getattr(args, "seed", None) or 42,
                max_modifications=getattr(args, "modifications", None),
                object_prompt=getattr(args, "object_prompt", None),
                planner_prompt=getattr(args, "planner_prompt", "planner_sequential.yaml"),
                memory_backend=getattr(args, "memory", "nested"),
                max_history=getattr(args, "max_history", None),
                tracked_harness=tracked_harness,
                enable_code_tool=getattr(args, "code_tool", True),
                enable_sink_completion_shim=getattr(args, "sink_shim", False),
                enable_planner=getattr(args, "enable_planner", True),
                enable_evaluator=getattr(args, "enable_evaluator", True),
                planner_brain=planner_brain,
                evaluator_brain=evaluator_brain,
                mock_server_url=mock_server_url,
                mock_slot_id=f"tc{tc_idx}-r{run_idx}",
                log_planner_output=(getattr(args, "verbose", None) == "DEBUG"),
                tool_dispatch=getattr(args, "tool_dispatch", "sync"),
                planner_mode=getattr(args, "planner_mode", "dag"),
                enable_replan_checkpoints=getattr(args, "enable_replan_checkpoints", False),
                replan_max_per_trace=getattr(args, "replan_max", 3),
                enable_step_retry_replan=getattr(args, "enable_step_retry_replan", True),
                step_max_retries=getattr(args, "step_max_retries", 2),
                step_replan_max=getattr(args, "step_replan_max", 1),
                reactive_replan_max_per_trace=getattr(args, "reactive_replan_max_per_trace", 4),
            )
        finally:
            # Always store snapshot and signal waiting workers — even on failure —
            # so consumers are never left waiting on a dead event.
            if reuse_steps and snapshot_out is not None and tc.sample_id:
                key = (tc.sample_id, run_idx)
                with _snapshot_registry_lock:
                    if snapshot_out:
                        _snapshots[key] = snapshot_out[0]
                    if key in _snapshot_events:
                        _snapshot_events[key].set()
        # Per-TC pass_rate is computed only over events that ran cleanly
        # (no technical failure on themselves or any prior event in the TC).
        # If every event is infra-error, pass_rate is None and the TC is
        # excluded from aggregate scoring downstream.
        clean_events = [e for e in event_results if not e.infra_error]
        pass_rate = (
            sum(1 for e in clean_events if e.passed) / len(clean_events)
            if clean_events else None
        )
        elapsed_s = time.monotonic() - tc_start
        result = SampleResult(
            tc_id=tc.id,
            sample_id=tc.sample_id,
            tc_index=tc_idx,
            seed=seed,
            name=tc.name,
            domain=tc.domain,
            run_index=run_idx,
            events=event_results,
            modifications=mod_results,
            pass_rate=pass_rate,
            elapsed_ms=elapsed_s * 1000.0,
            **_role_elapsed_fields(event_results),
        )
        passed_n = sum(1 for e in event_results if e.passed)
        total_n = len(event_results)
        rate_str = f"{pass_rate:.0%}" if pass_rate is not None else "N/A"
        elapsed_str = f"{int(elapsed_s) // 60:02d}:{int(elapsed_s) % 60:02d}.{int((elapsed_s % 1) * 1000):03d}"

        avg_evt_s = elapsed_s / total_n if total_n else 0.0
        parts = [f"\n  → pass={passed_n}/{total_n} ({rate_str})  elapsed={elapsed_str}  avg/evt={avg_evt_s:.1f}s"]
        detail = format_tc_event_detail(event_results)
        if detail:
            parts.append(f"     {detail}")
        tc_log.extend(parts)
        mod_label = tc.modifications[0].mod_type.value if tc.modifications else ""
        tqdm.write("\n".join([f"\n{tc.id}[{mod_label}]  msgs={msg_count[0]}  elapsed={elapsed_str}"] + tc_log))
        return result

    # Build list of pending (tc_idx, tc, run_idx) tuples
    pending_runs = [
        (tc_idx, tc, run_idx)
        for tc_idx, tc in enumerate(test_cases)
        for run_idx in range(args.runs)
        if (tc_idx, run_idx, seed) not in completed
        and (tc.id, run_idx) not in completed_legacy
    ]

    is_continuation = bool(completed or completed_legacy)
    run_config = RunConfig(
        version=_VERSION,
        timestamp=datetime.now().isoformat(),
        input_path=str(args.input),
        output_path=str(args.output),
        model=args.model,
        provider=args.provider,
        judge_model=judge_model,
        judge_provider=judge_provider,
        judge_specs=[f"{p}/{m}" for p, m in parsed_judges] if len(parsed_judges) > 1 else [],
        tracked_judge=str(tracked_judge_path) if tracked_judge_path else None,
        runs=args.runs,
        workers=workers,
        timeout_s=timeout_s,
        seed=seed,
        steps_only=getattr(args, "steps_only", False),
        max_chain_depth=args.max_chain_depth,
        mock_config_paths=[str(p) for p in (getattr(args, "mock_config", None) or [])],
        tc_filter=getattr(args, "tc", None),
        limit=getattr(args, "limit", None),
        concurrency=getattr(args, "concurrency", None),
        modifications=getattr(args, "modifications", None),
        is_continuation=is_continuation,
        enable_step_retry_replan=getattr(args, "enable_step_retry_replan", True),
        step_max_retries=getattr(args, "step_max_retries", 2),
        step_replan_max=getattr(args, "step_replan_max", 1),
        reactive_replan_max_per_trace=getattr(args, "reactive_replan_max_per_trace", 4),
    )

    # With --reuse-steps, split into two phases to avoid blocking worker threads:
    # Phase 1 — one TC per (sample_id, run_idx): runs steps, captures snapshot.
    # Phase 2 — remaining variants: snapshot already ready, skip steps, full parallelism.
    # Without --reuse-steps (or no sample_ids), a single phase covers all runs.
    if reuse_steps:
        seen_keys: set = set()
        producers: list = []
        consumers: list = []
        for item in pending_runs:
            tc_idx, tc, run_idx = item
            key = (tc.sample_id or tc.id, run_idx)
            if key not in seen_keys:
                seen_keys.add(key)
                producers.append(item)
            else:
                consumers.append(item)
        run_phases = [("Phase 1/2: steps", producers), ("Phase 2/2: variants", consumers)]
    else:
        run_phases = [("", pending_runs)]

    # tqdm creates a multiprocessing.RLock (a POSIX semaphore) for its write lock.
    # os._exit() below skips the resource tracker's cleanup, which would log a
    # "leaked semaphore" UserWarning. Pre-seeding the class attribute with a plain
    # threading.RLock prevents the POSIX semaphore from being created at all —
    # a threading lock is sufficient since evaluate.py runs in a single process.
    try:
        from tqdm.std import TqdmDefaultWriteLock as _TqdmLock
        if not hasattr(_TqdmLock, 'mp_lock'):
            _TqdmLock.mp_lock = threading.RLock()
    except Exception:
        pass

    # Track how many TC results we expect per run_idx so we can print a
    # mid-run summary the moment each run cycle completes.
    _expected_per_run: dict[int, int] = {}
    for _, _, _ri in pending_runs:
        _expected_per_run[_ri] = _expected_per_run.get(_ri, 0) + 1
    _done_per_run: dict[int, int] = {ri: 0 for ri in _expected_per_run}

    with open(args.output, file_mode) as f:
        f.write(run_config.model_dump_json() + "\n")
        f.flush()
        with tqdm(total=total_runs, initial=n_skipped, unit="run", desc="Evaluating") as pbar:
            _pbar_holder[0] = pbar
            if all_tc_results:  # continuation — show running metrics immediately
                _pbar_postfix(pbar, all_tc_results, _event_counter[0], _in_tok_counter[0], _out_tok_counter[0], agent_model=args.model)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                for phase_label, phase_runs in run_phases:
                    if not phase_runs:
                        continue
                    if phase_label and any(p[1] for p in run_phases if p[0] != phase_label):
                        tqdm.write(f"\n── {phase_label} ({len(phase_runs)} runs) ──")
                    futures = {
                        executor.submit(_run_one, tc_idx, tc, run_idx): (tc_idx, tc, run_idx)
                        for tc_idx, tc, run_idx in phase_runs
                    }
                    for future in as_completed(futures):
                        tc_idx, tc, run_idx = futures[future]
                        label = f"{tc.id}"
                        try:
                            tc_result = future.result()
                            with write_lock:
                                f.write(tc_result.model_dump_json() + "\n")
                                f.flush()
                                all_tc_results.append(tc_result)
                                _pbar_postfix(pbar, all_tc_results, _event_counter[0], _in_tok_counter[0], _out_tok_counter[0], agent_model=args.model)
                                if run_idx in _done_per_run:
                                    _done_per_run[run_idx] += 1
                                    if _done_per_run[run_idx] == _expected_per_run[run_idx]:
                                        run_results = [r for r in all_tc_results if r.run_index == run_idx]
                                        run_summary = _compute_summary(run_results)
                                        import contextlib, io as _io
                                        buf = _io.StringIO()
                                        with contextlib.redirect_stdout(buf):
                                            _print_summary(run_summary)
                                        tqdm.write(f"\n── Run {run_idx + 1}/{args.runs} ──\n" + buf.getvalue())
                        except Exception as e:
                            tqdm.write(f"FAILED {label} run={run_idx}: {e}", file=sys.stderr)
                        pbar.update(1)

    # Write summary
    summary = _compute_summary(all_tc_results)
    with open(args.output, "a") as f:
        f.write(summary.model_dump_json() + "\n")

    print()
    _print_summary(summary, output_path=args.output, elapsed_s=time.monotonic() - eval_start,
                   agent_model=args.model, judge_model=args.judge_model)
    if mock_server is not None:
        mock_server.stop()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


_STEP_EVENT_ID = re.compile(r"^S\d+$")


def _running_metrics(results: "list[SampleResult]") -> tuple[Optional[float], Optional[float]]:
    """Return (mean_pass_rate, sample_pass_rate) across accumulated results.

    sample_pass_rate: among base-TC runs (first tc_id per sample_id) that have
    step events, the fraction where ALL step events passed.
    """
    valid = [r.pass_rate for r in results if r.pass_rate is not None]
    mean_pr = sum(valid) / len(valid) if valid else None

    first_tc_per_sample: dict[str, str] = {}
    for r in results:
        sid = r.sample_id or r.tc_id
        if sid not in first_tc_per_sample:
            first_tc_per_sample[sid] = r.tc_id
    base_tc_ids = set(first_tc_per_sample.values())

    attempts = passes = 0
    for r in results:
        if r.tc_id not in base_tc_ids:
            continue
        step_evts = [e for e in r.events if _STEP_EVENT_ID.match(e.event_id)]
        if not step_evts:
            continue
        attempts += 1
        if all(e.passed for e in step_evts):
            passes += 1
    sample_pr = passes / attempts if attempts else None
    return mean_pr, sample_pr


def _pbar_postfix(pbar, results, events_done: int = 0, in_tok: int = 0, out_tok: int = 0,
                  agent_model: str = None) -> None:
    """Update pbar postfix with running mean + sample pass rates + live counters."""
    mean_pr, sample_pr = _running_metrics(results)
    fields: dict[str, str] = {}
    if mean_pr is not None:
        fields["mean"] = f"{mean_pr:.1%}"
    if sample_pr is not None:
        fields["sample"] = f"{sample_pr:.1%}"
    cost = _compute_cost(in_tok, out_tok, agent_model or "")
    cost_str = f" (${cost:.2f})" if cost is not None else ""
    fields["tok"] = f"{in_tok//1000}k↑{out_tok//1000}k↓{cost_str}"
    # refresh=True: postfix-only changes never trigger a redraw on their own — pbar.update()
    # only fires per completed RUN, so single-TC runs showed NO live counters at all
    pbar.set_postfix(refresh=True, **fields)


def _compute_summary(results: list[SampleResult]) -> EvalSummary:
    """Compute aggregate metrics across all test case results.

    Step events (id matching S\\d+) are deduplicated by sample_id: only the first
    TC variant per sample contributes step results to the summary. All TC variants
    contribute their modification and timeline event results. This avoids
    over-weighting baseline behavior when multiple variants share the same sample.
    """
    all_mods = [m for r in results for m in r.modifications]
    total_runs = len(results)
    total_test_cases = len({r.tc_id for r in results})

    def mean(vals):
        return sum(vals) / len(vals) if vals else 0.0

    # Identify the first TC seen per sample_id — that TC's step results are canonical.
    # TCs without a sample_id fall back to their own tc_id (treated as unique samples).
    first_tc_per_sample: dict[str, str] = {}
    for r in results:
        sid = r.sample_id or r.tc_id
        if sid not in first_tc_per_sample:
            first_tc_per_sample[sid] = r.tc_id
    base_tc_ids = set(first_tc_per_sample.values())

    # TCs where any event had an infra error (e.g. content filter) — excluded from all scoring.
    # A TC is "infra-error" (excluded from aggregate scoring) ONLY when EVERY
    # event hit a technical failure — there are no clean events to score.
    # Partial-infra TCs (some clean events, some failed-then-propagated)
    # contribute their clean events to aggregates; their per-TC pass_rate is
    # already computed over the clean events only.
    infra_error_tc_ids: set[str] = {
        r.tc_id for r in results
        if r.events and any(e.infra_error for e in r.events)
    }

    # Compute per-result effective events (step events only for the base TC per sample).
    # infra_error events are excluded from scoring (they're not clean measurements).
    all_events: list[EventResult] = []
    pass_rates: list[float] = []
    for r in results:
        if r.tc_id in infra_error_tc_ids:
            continue
        is_base = r.tc_id in base_tc_ids
        effective = [
            e for e in r.events
            if (is_base or not _STEP_EVENT_ID.match(e.event_id))
            and not e.infra_error
        ]
        all_events.extend(effective)
        if effective:
            pass_rates.append(sum(1 for e in effective if e.passed) / len(effective))
        # TCs with no evaluable events are excluded from pass rate — not counted as passing.

    mean_pass_rate = mean(pass_rates) if pass_rates else 0.0

    # 95% CI half-width on the mean pass rate, from across-TC variance (Student's t).
    by_tc: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.pass_rate is not None:
            by_tc[r.tc_id].append(r.pass_rate)
    per_tc_means_pr = [mean(v) for v in by_tc.values() if v]
    n_pr = len(per_tc_means_pr)
    if n_pr >= 2:
        try:
            from scipy import stats as _scipy_stats
            _t_crit_pr = float(_scipy_stats.t.ppf(0.975, df=n_pr - 1))
        except ImportError:
            _t_crit_pr = 1.96
        pass_rate_ci95 = _t_crit_pr * statistics.stdev(per_tc_means_pr) / (n_pr ** 0.5)
    else:
        pass_rate_ci95 = None

    def _per_tc_ci95(by_tc_rates: dict) -> Optional[float]:
        """95% CI half-width on the mean, from across-TC variance (Student's t)."""
        tc_means = [mean(v) for v in by_tc_rates.values() if v]
        n = len(tc_means)
        if n < 2:
            return None
        try:
            from scipy import stats as _scipy_stats
            t_crit = float(_scipy_stats.t.ppf(0.975, df=n - 1))
        except ImportError:
            t_crit = 1.96
        return t_crit * statistics.stdev(tc_means) / (n ** 0.5)

    # Steps pass rate + std (base TCs only, mean fraction of steps passed per TC)
    by_tc_step: dict[str, list[float]] = defaultdict(list)
    # Workflows completion + std (fraction of TCs where ALL step events passed)
    by_tc_completion: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.tc_id not in base_tc_ids or r.tc_id in infra_error_tc_ids:
            continue
        step_evts = [e for e in r.events if _STEP_EVENT_ID.match(e.event_id) and not e.infra_error]
        if step_evts:
            by_tc_step[r.tc_id].append(sum(1 for e in step_evts if e.passed) / len(step_evts))
            by_tc_completion[r.tc_id].append(1.0 if all(e.passed for e in step_evts) else 0.0)
    steps_pass_rate = mean([mean(v) for v in by_tc_step.values()]) if by_tc_step else None
    steps_pass_rate_ci95 = _per_tc_ci95(by_tc_step)
    samples_completion = mean([mean(v) for v in by_tc_completion.values()]) if by_tc_completion else None
    samples_completion_ci95 = _per_tc_ci95(by_tc_completion)

    # Inconclusive TCs: TCs where any run had at least one step failure.
    # Probe TCs (no pre_mod events) are exempt: step failures there are unrelated
    # to modification evaluation and should not suppress probe/tracked metrics.
    # Infra-error TCs are excluded separately and not also marked inconclusive.
    tcs_with_pre_mod = {r.tc_id for r in results if any(e.role == "pre_mod" for e in r.events)}
    inconclusive_tc_ids: set[str] = set()
    for r in results:
        if r.tc_id not in tcs_with_pre_mod or r.tc_id in infra_error_tc_ids:
            continue
        step_evts = [e for e in r.events if _STEP_EVENT_ID.match(e.event_id) and not e.infra_error]
        if step_evts and any(not e.passed for e in step_evts):
            inconclusive_tc_ids.add(r.tc_id)

    # Role-based pass rates + std: exclude inconclusive + infra-error TCs, grouped by TC across runs
    def _role_pass_rate_and_ci95(role_val, exclude_inconclusive=True) -> tuple[Optional[float], Optional[float]]:
        by_tc: dict[str, list[float]] = defaultdict(list)
        for r in results:
            if r.tc_id in infra_error_tc_ids:
                continue
            if exclude_inconclusive and r.tc_id in inconclusive_tc_ids:
                continue
            evts = [e for e in r.events if e.role == role_val and not e.infra_error]
            if evts:
                by_tc[r.tc_id].append(sum(1 for e in evts if e.passed) / len(evts))
        rate = mean([mean(v) for v in by_tc.values()]) if by_tc else None
        return rate, _per_tc_ci95(by_tc)

    conclusive_events = [
        e for r in results if r.tc_id not in inconclusive_tc_ids and r.tc_id not in infra_error_tc_ids
        for e in r.events
        if not e.infra_error
    ]
    mod_events = [e for e in conclusive_events if e.role in ("pre_mod", "post_mod", "irrelevant")]
    mod_pass_rate = (sum(1 for e in mod_events if e.passed) / len(mod_events)) if mod_events else None

    by_tc_mod: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.tc_id in inconclusive_tc_ids or r.tc_id in infra_error_tc_ids:
            continue
        evts = [e for e in r.events if e.role in ("pre_mod", "post_mod", "irrelevant") and not e.infra_error]
        if evts:
            by_tc_mod[r.tc_id].append(sum(1 for e in evts if e.passed) / len(evts))
    mod_pass_rate_ci95 = _per_tc_ci95(by_tc_mod)

    pre_mod_pass_rate, pre_mod_pass_rate_ci95 = _role_pass_rate_and_ci95("pre_mod")
    post_mod_pass_rate, post_mod_pass_rate_ci95 = _role_pass_rate_and_ci95("post_mod")
    irrelevant_pass_rate, irrelevant_pass_rate_ci95 = _role_pass_rate_and_ci95("irrelevant")

    # Role-based pass rates including inconclusive TCs but still excluding infra-error TCs (indicative)
    all_mod_events = [
        e for r in results if r.tc_id not in infra_error_tc_ids
        for e in r.events
        if e.role in ("pre_mod", "post_mod", "irrelevant") and not e.infra_error
    ]
    mod_pass_rate_all = (sum(1 for e in all_mod_events if e.passed) / len(all_mod_events)) if all_mod_events else None

    by_tc_mod_all: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.tc_id in infra_error_tc_ids:
            continue
        evts = [e for e in r.events if e.role in ("pre_mod", "post_mod", "irrelevant")]
        if evts:
            by_tc_mod_all[r.tc_id].append(sum(1 for e in evts if e.passed) / len(evts))
    mod_pass_rate_all_ci95 = _per_tc_ci95(by_tc_mod_all)

    pre_mod_pass_rate_all, pre_mod_pass_rate_all_ci95 = _role_pass_rate_and_ci95("pre_mod", exclude_inconclusive=False)
    post_mod_pass_rate_all, post_mod_pass_rate_all_ci95 = _role_pass_rate_and_ci95("post_mod", exclude_inconclusive=False)
    irrelevant_pass_rate_all, irrelevant_pass_rate_all_ci95 = _role_pass_rate_and_ci95("irrelevant", exclude_inconclusive=False)

    return EvalSummary(
        total_test_cases=total_test_cases,
        total_runs=total_runs,
        total_events=len(all_events),
        mean_pass_rate=mean_pass_rate,
        pass_rate_ci95=pass_rate_ci95,
        steps_pass_rate=steps_pass_rate,
        steps_pass_rate_ci95=steps_pass_rate_ci95,
        samples_completion=samples_completion,
        samples_completion_ci95=samples_completion_ci95,
        mod_pass_rate=mod_pass_rate,
        mod_pass_rate_ci95=mod_pass_rate_ci95,
        mod_pass_rate_all=mod_pass_rate_all,
        mod_pass_rate_all_ci95=mod_pass_rate_all_ci95,
        pre_mod_pass_rate=pre_mod_pass_rate,
        pre_mod_pass_rate_ci95=pre_mod_pass_rate_ci95,
        pre_mod_pass_rate_all=pre_mod_pass_rate_all,
        pre_mod_pass_rate_all_ci95=pre_mod_pass_rate_all_ci95,
        post_mod_pass_rate=post_mod_pass_rate,
        post_mod_pass_rate_ci95=post_mod_pass_rate_ci95,
        post_mod_pass_rate_all=post_mod_pass_rate_all,
        post_mod_pass_rate_all_ci95=post_mod_pass_rate_all_ci95,
        irrelevant_pass_rate=irrelevant_pass_rate,
        irrelevant_pass_rate_ci95=irrelevant_pass_rate_ci95,
        irrelevant_pass_rate_all=irrelevant_pass_rate_all,
        irrelevant_pass_rate_all_ci95=irrelevant_pass_rate_all_ci95,
        inconclusive_tcs=len(inconclusive_tc_ids),
        infra_error_tcs=len(infra_error_tc_ids),
        mean_event_input_tokens=mean([e.input_tokens for e in all_events]),
        mean_event_output_tokens=mean([e.output_tokens for e in all_events]),
        mean_event_latency_ms=mean([e.latency_ms for e in all_events]),
        mean_mod_input_tokens=mean([m.input_tokens for m in all_mods]),
        mean_mod_output_tokens=mean([m.output_tokens for m in all_mods]),
        mean_mod_latency_ms=mean([m.latency_ms for m in all_mods]),
        mean_base_event_latency_ms=mean([e.latency_ms for e in all_events if e.role is None]) or None,
        mean_pre_mod_event_latency_ms=mean([e.latency_ms for e in all_events if e.role == "pre_mod"]) or None,
        mean_post_mod_event_latency_ms=mean([e.latency_ms for e in all_events if e.role == "post_mod"]) or None,
        mean_irrelevant_event_latency_ms=mean([e.latency_ms for e in all_events if e.role == "irrelevant"]) or None,
        total_agent_input_tokens=sum(e.input_tokens for e in all_events) + sum(m.input_tokens for m in all_mods),
        total_agent_output_tokens=sum(e.output_tokens for e in all_events) + sum(m.output_tokens for m in all_mods),
        total_judge_input_tokens=sum(e.judge_input_tokens for e in all_events),
        total_judge_output_tokens=sum(e.judge_output_tokens for e in all_events),
        total_planner_input_tokens=sum(e.planner_input_tokens for e in all_events) + sum(m.planner_input_tokens for m in all_mods),
        total_planner_output_tokens=sum(e.planner_output_tokens for e in all_events) + sum(m.planner_output_tokens for m in all_mods),
        total_executor_input_tokens=sum(e.executor_input_tokens for e in all_events) + sum(m.executor_input_tokens for m in all_mods),
        total_executor_output_tokens=sum(e.executor_output_tokens for e in all_events) + sum(m.executor_output_tokens for m in all_mods),
        total_executor_calls=sum(e.executor_calls for e in all_events) + sum(m.executor_calls for m in all_mods),
        total_executor_retries=sum(e.executor_retries for e in all_events) + sum(m.executor_retries for m in all_mods),
        mean_executor_calls_per_event=(
            (sum(e.executor_calls for e in all_events) / len(all_events)) if all_events else 0.0
        ),
        mean_executor_retries_per_event=(
            (sum(e.executor_retries for e in all_events) / len(all_events)) if all_events else 0.0
        ),
        total_evaluator_input_tokens=sum(e.evaluator_input_tokens for e in all_events) + sum(m.evaluator_input_tokens for m in all_mods),
        total_evaluator_output_tokens=sum(e.evaluator_output_tokens for e in all_events) + sum(m.evaluator_output_tokens for m in all_mods),
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate test cases against the LNL runtime",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.data.evaluate -i outputs/data/zapier/20260322_120000/samples.jsonl
  python -m src.data.evaluate -i workflows-mods.jsonl --runs 3 --model claude-sonnet-4-6
  python -m src.data.evaluate -i workflows-mods.jsonl --model gpt-4o --judge-model claude-sonnet-4-6
""",
    )
    parser.add_argument(
        "--input", "-i",
        type=Path,
        default=None,
        help="Path to test cases JSONL file",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Output JSONL path (default: {stem}_eval.jsonl next to input)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Number of runs per test case for behavioral consistency (default: 1)",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=1,
        help="Number of parallel test case workers (default: 1). Each worker runs one TC at a time; LNL runtime uses its own thread pool per TC.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        metavar="SECONDS",
        help="Wall-clock timeout per step/event (not per test case); timed-out steps are marked failed and execution continues (default: 180)",
    )
    parser.add_argument(
        "--verbose", "-v",
        nargs="?",
        const="INFO",
        default="ERROR",
        choices=["ERROR", "INFO", "DEBUG"],
        help=(
            "Verbosity level (default: ERROR — failures only). "
            "-v / --verbose INFO: also stream passing TC events inline. "
            "--verbose DEBUG: INFO + capture per-event planner plans and outgoing "
            "bus messages on each EventResult (bloats output — use with --tc filter)."
        ),
    )
    parser.add_argument(
        "--debug-messages",
        action="store_true",
        default=False,
        help="Print messages exchanged between LLM-objects during evaluation",
    )
    parser.add_argument(
        "--tc",
        nargs="+",
        default=None,
        metavar="N_OR_ID",
        help="Run specific test cases by 1-based index, ID, sample_id, or ID[mod_type] (e.g. --tc 2 TC007 S001 TC001[temporal]). Passing a sample_id selects all TCs sharing that sample. Overrides --limit.",
    )
    parser.add_argument(
        "--steps-only",
        action="store_true",
        default=False,
        help="Run only the steps (baseline behavior); skip modifications and events",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Number of events to fire concurrently per modification window (default: 0 = sequential). "
            "When >0, each mod window fires a pre-mod and post-mod concurrent group: "
            "1 relevant event + up to N-1 irrelevant events dispatched in one transaction. "
            "Requires test cases generated with --concurrent-events."
        ),
    )
    parser.add_argument(
        "--modifications",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Limit evaluation to the first N modifications per test case (default: all). "
            "Events whose after_mod_ids reference mods beyond N are skipped. "
            "Useful for evaluating 3-mod test cases as if they were 1-mod or 2-mod."
        ),
    )
    parser.add_argument(
        "--reuse-steps",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Run base steps once per sample and reuse the resulting runtime state "
            "across all TC variants sharing the same sample_id. Saves ~5-6x step "
            "cost when each sample has multiple variants. Requires sample_id to be set. "
            "Use --no-reuse-steps to disable. (default: enabled)"
        ),
    )
    parser.add_argument(
        "--code-tool",
        action=argparse.BooleanOptionalAction,
        default=True,
        dest="code_tool",
        help=(
            "Register the built-in `python` REPL tool on every LLM-object so the "
            "LLM can run code (with per-object persistent namespace) instead of "
            "reasoning numerically in natural language. "
            "Use --no-code-tool to revert to the default agent configuration. (default: enabled)"
        ),
    )
    parser.add_argument(
        "--enable-planner",
        action=argparse.BooleanOptionalAction,
        dest="enable_planner",
        default=True,
        help=(
            "Pre-execution planner: separate LLM call that produces a structured "
            "plan BEFORE the executor's ReAct loop. The plan is stored in the "
            "object's active_plan and surfaces in the executor prompt as a "
            "checklist. Default: ENABLED (paired with executor.yaml object-prompt "
            "and --enable-evaluator). Use --no-enable-planner to disable."
        ),
    )
    parser.add_argument(
        "--enable-evaluator",
        action=argparse.BooleanOptionalAction,
        dest="enable_evaluator",
        default=True,
        help=(
            "Post-execution evaluator: separate LLM call that grades the "
            "executor's last turn against the active plan, returning structured "
            "criterion-level PASS/FAIL. On FAIL, the runtime delivers a feedback "
            "heartbeat so the executor can fix the gaps. Capped at N cycles per "
            "trace to bound cost. Requires --enable-planner for there to be a "
            "plan to evaluate against. Default: ENABLED. Use --no-enable-evaluator "
            "to disable."
        ),
    )
    parser.add_argument(
        "--evaluator-model",
        default=None,
        help=(
            "Model used by the post-execution evaluator. Only consulted when "
            "--enable-evaluator is set. Defaults to --model (the agent model)."
        ),
    )
    parser.add_argument(
        "--evaluator-provider",
        choices=["openai", "azure", "anthropic", "google"],
        default=None,
        help="Provider for the evaluator model. Defaults to --provider.",
    )
    parser.add_argument(
        "--sink-shim",
        action=argparse.BooleanOptionalAction,
        dest="sink_shim",
        default=False,
        help=(
            "Sink Completion Shim: for objects whose role identifies them as "
            "write/notify sinks (Write Service, Storage, Notifier, Publisher), "
            "if the LLM finishes without producing an artifact (URL/ID) in its "
            "reply AND without a completion marker (status: sent/stored/...) "
            "in state, the runtime synthesizes a plausible artifact and "
            "injects it into state + augments the reply. Default: DISABLED. "
            "Use --sink-shim to enable."
        ),
    )
    parser.add_argument(
        "--llm-judge",
        action="append",
        default=None,
        metavar="[PROVIDER/]MODEL",
        help=(
            "Judge model spec (can be repeated for a multi-judge panel). "
            "Format: 'model' (provider inferred) or 'provider/model'. "
            "With 2 judges both must agree; with 3+ a majority vote is used. "
            "Example: --llm-judge gpt-4o --llm-judge claude-sonnet-4-6"
        ),
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help="Model for LLM-as-judge (default: same as --model). Ignored when --llm-judge is set.",
    )
    parser.add_argument(
        "--judge-provider",
        choices=["openai", "azure", "anthropic", "google"],
        default=None,
        help="Provider for judge model (inferred from judge-model if not specified). Ignored when --llm-judge is set.",
    )
    parser.add_argument(
        "--planner-model",
        default=None,
        help=(
            "Model used by the planner LLM call (separate from the agent). "
            "Only consulted when --fan-out-decompose is set. "
            "Defaults to --model (the agent model)."
        ),
    )
    parser.add_argument(
        "--planner-provider",
        choices=["openai", "azure", "anthropic", "google"],
        default=None,
        help="Provider for the planner model. Defaults to --provider.",
    )
    parser.add_argument(
        "--tracked-judge",
        type=Path,
        default=None,
        metavar="YAML",
        help=(
            "Path to a judge YAML with a `system_prompt` key. When set, events with "
            "role='irrelevant' and expect set (tracked events in probe-dataset TCs) "
            "are judged using this prompt instead of the default judge prompt."
        ),
    )
    parser.add_argument(
        "--thinking",
        choices=["disabled", "enabled"],
        default=None,
        help="Set Anthropic extended thinking mode (disabled/enabled). Default: not set (model default).",
    )
    parser.add_argument(
        "--object-prompt",
        default=None,
        help=(
            "Object system-prompt template filename relative to config/prompts/lnl/. "
            "Default: chosen by --memory (executor.yaml for flat, executor_nested.yaml "
            "for nested). Pass object.yaml for the legacy self-planning agent."
        ),
    )
    parser.add_argument(
        "--memory",
        default="nested",
        choices=["flat", "nested"],
        help=(
            "Memory backend used by LLM-objects. nested (default): Redux-style "
            "{op, path, value} actions over a nested JSON tree (ops: "
            "set/merge/delete/append, dotted paths). flat: legacy {op, key, value} "
            "deltas at top-level keys — use for A/B comparison with historical runs."
        ),
    )
    parser.add_argument(
        "--planner-prompt",
        default="planner_sequential.yaml",
        help="Planner system-prompt template filename relative to config/prompts/lnl/ (default: planner_sequential.yaml; auto-replaced by planner_dag.yaml when --planner-mode=dag).",
    )
    parser.add_argument(
        "--planner-mode",
        choices=["sequential", "dag"],
        default="dag",
        help=(
            "Planner output shape (default: dag). "
            "'dag' — planner emits a dependency graph; independent steps (empty "
            "depends_on or all deps done) fan out in a single executor turn. "
            "'sequential' — planner emits steps the executor dispatches one per "
            "turn (use for reproducibility against pre-2026-05 historical runs). "
            "Selects planner_dag.yaml automatically when set to 'dag' unless "
            "--planner-prompt is also passed explicitly."
        ),
    )
    parser.add_argument(
        "--enable-replan-checkpoints",
        action=argparse.BooleanOptionalAction,
        dest="enable_replan_checkpoints",
        default=False,
        help=(
            "Replan checkpoints: planner re-entry when a `kind=replan` step is "
            "reached. The planner may insert replan steps that suspend "
            "execution until their `depends_on` results land, then re-invoke "
            "the planner with the prior plan + completed step results so it "
            "can emit continuation steps. Use for conditional branches the "
            "planner cannot decide up-front (stock level, authorization, "
            "returned id). Default: DISABLED — empirically the runtime flag "
            "causes a regression on the random-30 mod eval even when the "
            "planner emits 0 replan steps (mechanism unclear; may be "
            "stochastic plan-shape drift from the flag changing runtime "
            "behavior in subtle ways). Re-enable per-run with this flag "
            "when working on workflows with genuine conditional branches. "
            "Budget per trace controlled by --replan-max."
        ),
    )
    parser.add_argument(
        "--replan-max",
        type=int,
        default=3,
        help=(
            "Max number of replan re-entries per trace_id (default: 3). "
            "Mirrors --evaluator-max-cycles. Only consulted when "
            "--enable-replan-checkpoints is set."
        ),
    )
    parser.add_argument(
        "--enable-step-retry-replan",
        action=argparse.BooleanOptionalAction,
        dest="enable_step_retry_replan",
        default=True,
        help=(
            "Reactive step-retry escalation: when the post-execution evaluator "
            "invalidates the same plan step --step-max-retries times in a row, "
            "synthesize a kind=replan step targeting it so the planner can "
            "propose an alternative continuation. If the synthesized replan's "
            "planner call itself fails, the plan flips to status=failed and "
            "the trace concludes with a graceful 'couldn't complete' reply. "
            "Independent of --enable-replan-checkpoints; safe to enable "
            "either, both, or neither. Default: DISABLED."
        ),
    )
    parser.add_argument(
        "--step-max-retries",
        type=int,
        default=2,
        help=(
            "Per-step retry budget before a reactive replan triggers "
            "(default: 2). Only consulted when --enable-step-retry-replan "
            "is set."
        ),
    )
    parser.add_argument(
        "--step-replan-max",
        type=int,
        default=1,
        help=(
            "Per-step cap on synthesized reactive replans (default: 1). "
            "Step is only escalated this many times before further "
            "invalidations are ignored. Only consulted when "
            "--enable-step-retry-replan is set."
        ),
    )
    parser.add_argument(
        "--reactive-replan-max-per-trace",
        type=int,
        default=4,
        dest="reactive_replan_max_per_trace",
        help=(
            "Total reactive replan steps allowed per trace (default: 4). "
            "Prevents plan explosion when many steps fail simultaneously. "
            "Only consulted when --enable-step-retry-replan is set."
        ),
    )
    parser.add_argument(
        "--max-tool-rounds",
        type=int,
        default=10,
        help="Max ReAct tool calls per object invocation (default: 10). Increase for objects with many skills/data tools.",
    )
    parser.add_argument(
        "--tool-dispatch",
        choices=["async", "sync"],
        default="sync",
        help=(
            "Tool dispatch mode (default: sync). "
            "'sync' — tools execute inline in the ReAct loop (single multi-turn LLM call); "
            "the result is fed back as the next user message. Blocks the object thread "
            "until all tools complete, but the LLM keeps its own prior tool_call in context. "
            "'async' — tools submit to a per-object pool; the result arrives as a mailbox "
            "REPLY processed in a new process_message turn (non-blocking actor semantics; "
            "the object can service peer/heartbeat messages while a tool runs)."
        ),
    )
    parser.add_argument(
        "--max-chain-depth",
        type=int,
        default=20,
        help="Max message chain depth per event (default: 20). Increase for workflows with many round-trips.",
    )
    parser.add_argument(
        "--max-history",
        type=int,
        default=None,
        metavar="N",
        help="Override conversation history window per object (default: 6). Use 0 to disable history entirely.",
    )
    parser.add_argument(
        "--mock-server",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Route mock tool calls through the HTTP mock server (POST /tool/{method}) "
            "instead of the in-process executor — the same tool-call path the OpenClaw "
            "baseline uses. Triggers still fire in-process (mirrors the baseline). "
            "Enabled by default; use --no-mock-server to disable."
        ),
    )
    parser.add_argument(
        "--mock-server-port",
        type=int,
        default=18888,
        help="Port for the local mock server started by --mock-server (default: 18888).",
    )
    parser.add_argument(
        "--mock-config",
        type=Path,
        action="append",
        default=None,
        metavar="YAML",
        help=(
            "YAML file with shared MockToolDef entries (can be specified multiple times). "
            "Loaded tools are merged with per-Sample mock_tools; Sample entries win on collision. "
            "Example: --mock-config config/mocks/lnl/email.yaml --mock-config config/mocks/lnl/slack.yaml"
        ),
    )
    add_common_args(parser)
    parser.add_argument(
        "--stats",
        default=None,
        metavar="FILE",
        type=Path,
        help="Recompute and reprint summary stats from an existing results JSONL file without re-running evaluation.",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        metavar="N",
        help="Randomly sample N test cases from the input (use with --sample-seed for reproducibility).",
    )
    parser.add_argument(
        "--sample-seed",
        type=int,
        default=None,
        metavar="S",
        help="Random seed for --sample (default: None = non-reproducible).",
    )
    return parser


def _load_tc_results(path: Path) -> list[SampleResult]:
    """Load SampleResult lines from a results JSONL, skipping EvalSummary lines."""
    import json as _json
    results = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = _json.loads(line)
            if "tc_id" in data:
                results.append(SampleResult(**data))
    return results


def _install_hang_diagnostics(stall_after_s: float = 300.0, check_every_s: float = 60.0) -> None:
    """Self-diagnosing hangs: two deadlocked runs froze with all threads parked on
    Python locks and nothing observable from outside (py-spy needs root on macOS).

    1. `kill -USR1 <pid>` dumps every thread's Python stack to stderr on demand.
    2. A daemon watchdog dumps all stacks automatically when the cost tracker
       records no new tokens for `stall_after_s` — token flow is the run's pulse;
       a silent 5 minutes means a deadlock or a hung request, and the dump names
       the exact file/line each thread is blocked on.
    """
    import faulthandler
    import signal as _signal
    import threading as _threading
    import time as _time
    from src.lnl.brain import LIVENESS as _liveness

    # chain=False: chaining re-runs the DEFAULT SIGUSR1 disposition, which is
    # process termination — a USR1 "diagnostic" killed a 45-minute run.
    faulthandler.register(_signal.SIGUSR1, all_threads=True, chain=False)

    def _pulse() -> int:
        # Attempts tick even when responses hang; a frozen counter for 5 minutes
        # means nothing is even TRYING to call out — or one call is wedged and
        # nothing else can proceed. Either way: dump.
        return int(_liveness["attempts"]) + int(_liveness["completions"])

    def _loop() -> None:
        last_pulse, last_change, dumped = _pulse(), _time.monotonic(), False
        while True:
            _time.sleep(check_every_s)
            cur = _pulse()
            if cur != last_pulse:
                last_pulse, last_change, dumped = cur, _time.monotonic(), False
            elif not dumped and _time.monotonic() - last_change > stall_after_s:
                sys.stderr.write(
                    f"\n=== HANG WATCHDOG: no token movement for {stall_after_s:.0f}s — "
                    f"dumping all thread stacks ===\n")
                faulthandler.dump_traceback(all_threads=True)
                sys.stderr.flush()
                dumped = True  # one dump per stall; resets when tokens move again

    _threading.Thread(target=_loop, daemon=True, name="hang-watchdog").start()


def main():
    args = build_parser().parse_args()
    _install_hang_diagnostics()
    if args.stats:
        results = _load_tc_results(args.stats)
        summary = _compute_summary(results)
        elapsed_s = sum(r.elapsed_ms for r in results if r.elapsed_ms) / 1000 or None
        _print_summary(summary, elapsed_s=elapsed_s)
        return
    if args.input is None:
        build_parser().error("the following arguments are required: --input/-i")
    run(args)


if __name__ == "__main__":
    main()
