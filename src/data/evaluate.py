"""
Evaluation runner — Stage 3 of the data pipeline.

Executes TestCases against the LNL runtime, judges outcomes with an LLM, and
reports correctness and cost metrics.

Usage:
    python -m src.data.evaluate \\
        -i outputs/data/zapier/20260322_120000/test_cases.jsonl \\
        --runs 3 \\
        --model gpt-4o --judge-model claude-sonnet-4-6
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
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

load_dotenv()

from src.data.schema import (
    EvalSummary,
    EventResult,
    MockConfig,
    MockToolDef,
    ModificationResult,
    RawEventData,
    RawTestCaseResult,
    RunConfig,
    TestCase,
    TestCaseResult,
    to_lnl_definition,
)
from src.data.utils import (
    add_common_args,
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


def _derive_tools_from_skills(tc: "TestCase") -> list[MockToolDef]:
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
    step_raw_events: list                  # RawEventData list for steps (replayed)
    prior_context: str                     # _format_prior_state after steps


def _take_snapshot(
    rt,
    mock_executors: list,
    step_event_results: list,
    step_raw_events: list,
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
        step_raw_events=list(step_raw_events),
        prior_context=prior_context,
    )


def _restore_snapshot(rt, mock_executors: list, snapshot: StepsSnapshot) -> None:
    """Restore runtime state from a snapshot (overwrites freshly-created objects)."""
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


def _build_trigger_map(tc: "TestCase") -> "dict[str, list]":
    """Build a map from event_id → list of events triggered by that event."""
    trigger_map: dict[str, list] = {}
    for event in tc.events:
        if event.triggered_by:
            trigger_map.setdefault(event.triggered_by, []).append(event)
    return trigger_map


# ── Timestamp parsing ──────────────────────────────────────────────────────────

def parse_when(when: str) -> int:
    """Convert 'W02-1T10:30' → ordinal minutes for sorting."""
    week_part, time_part = when.split("T")
    w, d = week_part.lstrip("W").split("-")
    h, m = time_part.split(":")
    return (int(w) * 7 + int(d)) * 1440 + int(h) * 60 + int(m)


# ── Evidence gathering ─────────────────────────────────────────────────────────

def gather_evidence(
    rt,
    results,
    recipient: str,
    bus_messages: "list | None" = None,
    tool_calls: "list[list[dict]] | None" = None,
) -> str:
    """Collect observable evidence after an event for the LLM judge.

    Evidence is structured in two sections:
    - THIS EVENT: everything that happened during this invocation —
      tool calls made (primary), bus messages exchanged, and replies.
    - OBJECT STATES: each object's updated_state after the event
      (may be empty; use THIS EVENT as authoritative evidence).
    """
    this_event_parts: list[str] = []

    # Tool calls (primary evidence when mock tools are in use)
    if tool_calls:
        flat = [entry for per_ex in tool_calls for entry in per_ex]
        if flat:
            lines = []
            for entry in flat:
                idx = entry.get("call_index", "?")
                line = f"  [{entry['tool']}] call#{idx} {json.dumps(entry['arguments'])}"
                if "response" in entry:
                    line += f"\n    ← {entry['response'][:100]}"
                for t in entry.get("triggered", []):
                    line += f"\n    → dispatched to [{t['target']}]: {t['message'][:120]}"
                lines.append(line)
            this_event_parts.append("Tool calls:\n" + "\n".join(lines))

    # Bus messages: the full message flow visible in --debug-messages
    if bus_messages:
        lines = []
        for ml in bus_messages:
            msg = ml.message
            arrow = "↩" if msg.type.value == "reply" else "→"
            sender = f"[{msg.sender}]" if msg.type.value == "event" else msg.sender
            lines.append(f"  {sender} {arrow} {msg.recipient} ({msg.type.value}): {msg.content[:200]}")
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
    if this_event_parts:
        sections.append("=== THIS EVENT ===\n" + "\n\n".join(this_event_parts))
    if state_parts:
        sections.append("=== OBJECT STATES ===\n" + "\n\n".join(state_parts))

    return "\n\n".join(sections) if sections else "(no observable state)"


# ── Core execution ─────────────────────────────────────────────────────────────

def _print_message(msg) -> None:
    """Print a message exchange between LLM-objects."""
    arrow = "↩" if msg.type.value == "reply" else "→"
    content = msg.content[:120].replace("\n", " ")
    # Mark external event sources clearly to distinguish from LLM-object IDs
    sender = f"[{msg.sender}]" if msg.type.value == "event" else msg.sender
    tqdm.write(f"      {sender} {arrow} {msg.recipient} ({msg.type.value}): {content}")


def _execute_test_case_inner(
    tc: TestCase,
    brain,
    harness,
    debug_messages: bool = False,
    timeout_s: Optional[float] = None,
    steps_only: bool = False,
    max_chain_depth: int = 20,
    global_mock_tools: "list[MockToolDef] | None" = None,
    progress_callback=None,
    on_event_result=None,
    on_mod_applied=None,
    on_raw_event=None,
    steps_snapshot: "Optional[StepsSnapshot]" = None,
    snapshot_out: "Optional[list]" = None,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Run a single TestCase and return event + modification results."""
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
    # (the generate_samples prompt marks them as "purely internal, no external system
    # calls"). Registering them as tools would tell the LLM to call them externally.
    # Triggered events are dispatched directly by the harness (no mock tool needed).
    final_mock_tools = merge_mock_tools(global_mock_tools or [], tc.mock_tools)

    # Only enable the tool machinery (and LLM_RESPONSE_SCHEMA_WITH_TOOLS) when there
    # are actual mock tools to wire up. Without mock tools, pass tool_registry=None so
    # objects use LLM_RESPONSE_SCHEMA (no tool_calls field) and the LLM won't try to
    # call peer objects as tools via the PassthroughExecutor fallback.
    mock_executors: list = []
    passthrough = PassthroughExecutor()
    tool_registry: "ToolRegistry | None" = None

    if final_mock_tools:
        tool_registry = ToolRegistry()
        tool_registry.register("execute_code", CodeExecutor())
        tool_registry.register_fallback(passthrough)
        for mock_tool in final_mock_tools:
            executor = MockInProcessExecutor(mock_tool)
            tool_registry.register(mock_tool.tool_name, executor, spec=executor.spec)
            mock_executors.append(executor)

    # 2. Create Runtime and EventGateway — synchronous mode (no rt.start()).
    # The evaluator dispatches one event at a time and waits; the background
    # run-loop is not needed and would break _run_with_timeout (ThreadPoolExecutor
    # shutdown(wait=True) hangs when the foreground thread is blocked on item.done).
    rt = Runtime(brain, tool_registry=tool_registry, max_chain_depth=max_chain_depth)
    if debug_messages and progress_callback:
        rt.set_message_listener(lambda msg: (_print_message(msg), progress_callback(msg)))
    elif debug_messages:
        rt.set_message_listener(_print_message)
    elif progress_callback:
        rt.set_message_listener(progress_callback)
    gw = EventGateway(rt)

    for obj_def in tc.objects:
        rt.create_object(to_lnl_definition(obj_def))

    trigger_map = _build_trigger_map(tc)
    return _run_test_case_timeline(
        tc, rt, gw, harness,
        timeout_s=timeout_s,
        steps_only=steps_only,
        mock_executors=mock_executors + [passthrough] if final_mock_tools else [],
        steps_snapshot=steps_snapshot,
        snapshot_out=snapshot_out,
        trigger_map=trigger_map,
        on_event_result=on_event_result,
        on_mod_applied=on_mod_applied,
        on_raw_event=on_raw_event,
    )


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


def _snapshot_logs(mock_executors: list) -> list[int]:
    return [len(ex.call_log) for ex in mock_executors]


def _new_tool_calls(mock_executors: list, snapshots: list[int]) -> list[list[dict]]:
    return [ex.call_log[s:] for ex, s in zip(mock_executors, snapshots)]


def _run_test_case_timeline(
    tc: TestCase,
    rt,
    gw,
    harness,
    timeout_s: Optional[float] = None,
    steps_only: bool = False,
    mock_executors: "list | None" = None,
    trigger_map: "dict[str, list] | None" = None,
    on_event_result=None,   # callable(EventResult, is_step: bool)
    on_mod_applied=None,    # callable(Modification)
    on_raw_event=None,      # callable(RawEventData) — fired before judge, for artifact capture
    steps_snapshot: "Optional[StepsSnapshot]" = None,   # restore from this; skip step execution
    snapshot_out: "Optional[list]" = None,              # append StepsSnapshot here after steps
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Execute steps and timeline events against a live runtime."""
    event_results: list[EventResult] = []
    mod_results: list[ModificationResult] = []

    execs = mock_executors or []
    prior_context: str = ""

    # 2. Run steps — initialize state and assert default (no-modification) behavior.
    # When steps_snapshot is provided, skip execution and restore from snapshot instead.
    if steps_snapshot is not None:
        _restore_snapshot(rt, execs, steps_snapshot)
        event_results.extend(steps_snapshot.step_event_results)
        prior_context = steps_snapshot.prior_context
        if on_raw_event:
            for raw in steps_snapshot.step_raw_events:
                on_raw_event(raw)
        if on_event_result:
            for ev in steps_snapshot.step_event_results:
                on_event_result(ev, True)
    else:
        _step_raw_events: list = []

    for i, step in enumerate(tc.steps):
        if steps_snapshot is not None:
            break  # already restored above
        log_snapshot = len(rt.message_log)
        exec_snap = _snapshot_logs(execs)
        t0 = time.monotonic()
        step_payload = json.dumps({"system": step.source, "content": step.text})
        results, timed_out = _run_with_timeout(
            lambda s=step, p=step_payload: gw.dispatch(s.target, p, source=s.source), timeout_s,
        )
        latency_ms = (time.monotonic() - t0) * 1000

        if step.expect is not None:
            if timed_out:
                event_results.append(EventResult(
                    event_id=f"S{i+1:03d}",
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
                bus_msgs = rt.message_log[log_snapshot:]
                new_calls = _new_tool_calls(execs, exec_snap)
                evidence = gather_evidence(rt, results, step.target, bus_messages=bus_msgs, tool_calls=new_calls)
                condition = step.expect.action
                raw = RawEventData(
                    event_id=f"S{i+1:03d}",
                    expected=condition,
                    evidence=evidence,
                    prior_context=prior_context,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    latency_ms=latency_ms,
                )
                if snapshot_out is not None:
                    _step_raw_events.append(raw)
                if on_raw_event:
                    on_raw_event(raw)
                passed, reasoning, votes = harness.evaluate_assertion(condition, evidence, prior_context)
                event_results.append(EventResult(
                    event_id=f"S{i+1:03d}",
                    passed=passed,
                    reasoning=reasoning,
                    expected=condition,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    latency_ms=latency_ms,
                    judge_votes=votes if len(votes) > 1 else [],
                ))
                if on_event_result:
                    on_event_result(event_results[-1], True)
        prior_context = _format_prior_state(rt)

    # Capture snapshot after steps complete (first TC in a sample group)
    if snapshot_out is not None and steps_snapshot is None:
        step_results_so_far = [e for e in event_results]
        snapshot_out.append(_take_snapshot(rt, execs, step_results_so_far, _step_raw_events, prior_context))

    if steps_only:
        return event_results, mod_results

    tmap = trigger_map or {}

    # 3. Build sorted timeline: tag each item with its type and when-ordinal.
    # Triggered events are dispatched as reactions after their parent fires —
    # exclude them from the main timeline ordering.
    timeline: list[tuple[int, str, object]] = []
    for mod in tc.modifications:
        timeline.append((parse_when(mod.when), "mod", mod))
    for evt in tc.events:
        if evt.triggered_by is None:
            timeline.append((parse_when(evt.when), "event", evt))
    timeline.sort(key=lambda x: x[0])

    def _dispatch_event(evt) -> tuple[list, bool, float]:
        """Dispatch a single event; return (results, timed_out, latency_ms)."""
        log_snap = len(rt.message_log)
        exec_s = _snapshot_logs(execs)
        t0 = time.monotonic()
        if evt.call_type == "send_event":
            payload = json.dumps({"system": evt.source, "content": evt.input})
            res, tout = _run_with_timeout(
                lambda e=evt, p=payload: gw.dispatch(e.recipient, p, source=e.source),
                timeout_s,
            )
        else:
            res, tout = _run_with_timeout(
                lambda e=evt: rt.send(e.recipient, e.input, sender=e.source),
                timeout_s,
            )
        lat = (time.monotonic() - t0) * 1000
        return res, tout, lat, log_snap, exec_s

    def _record_event_result(evt, res, timed_out, lat_ms, log_snap, exec_s, ctx: str = ""):
        if timed_out:
            event_results.append(EventResult(
                event_id=evt.id,
                passed=False,
                reasoning=f"Timeout after {timeout_s}s",
                expected=evt.expect.action,
                latency_ms=lat_ms,
            ))
        else:
            in_tok = sum(r.metrics.input_tokens for r in res if r.metrics)
            out_tok = sum(r.metrics.output_tokens for r in res if r.metrics)
            bus_msgs = rt.message_log[log_snap:]
            new_calls = _new_tool_calls(execs, exec_s)
            evidence = gather_evidence(rt, res, evt.recipient, bus_messages=bus_msgs, tool_calls=new_calls)
            if on_raw_event:
                on_raw_event(RawEventData(
                    event_id=evt.id,
                    expected=evt.expect.action,
                    evidence=evidence,
                    prior_context=ctx,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    latency_ms=lat_ms,
                ))
            passed, reasoning, votes = harness.evaluate_assertion(evt.expect.action, evidence, ctx)
            event_results.append(EventResult(
                event_id=evt.id,
                passed=passed,
                reasoning=reasoning,
                expected=evt.expect.action,
                input_tokens=in_tok,
                output_tokens=out_tok,
                latency_ms=lat_ms,
                judge_votes=votes if len(votes) > 1 else [],
            ))

    for _, kind, item in timeline:
        if kind == "mod":
            if on_mod_applied:
                on_mod_applied(item)
            t0 = time.monotonic()
            results, timed_out = _run_with_timeout(
                lambda it=item: rt.send(it.target, it.intent, sender=it.source),
                timeout_s,
            )
            latency_ms = (time.monotonic() - t0) * 1000
            if timed_out:
                mod_results.append(ModificationResult(mod_id=item.id, latency_ms=latency_ms))
            else:
                in_tok = sum(r.metrics.input_tokens for r in results if r.metrics)
                out_tok = sum(r.metrics.output_tokens for r in results if r.metrics)
                mod_results.append(ModificationResult(
                    mod_id=item.id,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    latency_ms=latency_ms,
                ))
            prior_context = _format_prior_state(rt)

        else:  # event
            res, tout, lat, log_snap, exec_s = _dispatch_event(item)
            if item.expect is not None:
                _record_event_result(item, res, tout, lat, log_snap, exec_s, prior_context)
                if on_event_result:
                    on_event_result(event_results[-1], False)
            prior_context = _format_prior_state(rt)

            # Dispatch events triggered by this one (in declaration order)
            for triggered_evt in tmap.get(item.id, []):
                tr, tt, tl, tls, tes = _dispatch_event(triggered_evt)
                if triggered_evt.expect is not None:
                    _record_event_result(triggered_evt, tr, tt, tl, tls, tes, prior_context)
                    if on_event_result:
                        on_event_result(event_results[-1], False)
                prior_context = _format_prior_state(rt)

    return event_results, mod_results


def execute_test_case(
    tc: TestCase,
    brain,
    harness,
    timeout_s: Optional[float] = None,
    debug_messages: bool = False,
    steps_only: bool = False,
    max_chain_depth: int = 20,
    global_mock_tools: "list[MockToolDef] | None" = None,
    progress_callback=None,
    on_event_result=None,
    on_mod_applied=None,
    on_raw_event=None,
    steps_snapshot: "Optional[StepsSnapshot]" = None,
    snapshot_out: "Optional[list]" = None,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Run a single TestCase with a per-event timeout (seconds).

    Each step, modification, and event gets its own timeout. If a single
    step times out, it is marked as failed and execution continues.

    Args:
        global_mock_tools: Shared mock tool definitions loaded from --mock-config files.
            Merged with tc.mock_tools; per-TestCase entries override shared ones.
        progress_callback: Optional callable(msg) invoked on every bus message delivery.
        on_event_result: Optional callable(EventResult, is_step: bool) for real-time display.
        on_mod_applied: Optional callable(Modification) called before each modification runs.
        on_raw_event: Optional callable(RawEventData) fired before each judge call, for artifact capture.
    """
    return _execute_test_case_inner(
        tc, brain, harness,
        debug_messages=debug_messages,
        timeout_s=timeout_s,
        steps_only=steps_only,
        max_chain_depth=max_chain_depth,
        global_mock_tools=global_mock_tools,
        progress_callback=progress_callback,
        on_event_result=on_event_result,
        on_mod_applied=on_mod_applied,
        on_raw_event=on_raw_event,
        steps_snapshot=steps_snapshot,
        snapshot_out=snapshot_out,
    )


# ── Output path ────────────────────────────────────────────────────────────────

def default_output_path(input_path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return input_path.parent / "runs" / f"{input_path.stem}_eval_{ts}.jsonl"


def runs_path_from_eval_path(eval_path: Path) -> Path:
    """Derive the _runs.jsonl artifact path from an _eval.jsonl path."""
    return eval_path.parent / eval_path.name.replace("_eval_", "_runs_", 1)


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


def _print_verbose(tc_result: TestCaseResult, show_evidence: bool = False) -> None:
    """Print per-event breakdown to console."""
    for ev in tc_result.events:
        _print_event_result(ev, show_evidence=show_evidence)


# ── Main runner ────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> Path:
    """Run evaluation. Returns the output path."""
    logging.basicConfig(level=logging.WARNING)

    if args.output is None:
        args.output = default_output_path(args.input)

    print(f"Output: {args.output}")

    if args.provider is None:
        args.provider = infer_provider(args.model)

    if not args.input.exists():
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    test_cases = load_jsonl(args.input, TestCase)

    if getattr(args, "tc", None):
        # Select test cases by 1-based index, ID, or ID[mod_type] (e.g. TC001[temporal]).
        # Preserve order of appearance in file.
        def _tc_mod_type(tc: TestCase) -> str:
            return tc.modifications[0].mod_type.value if tc.modifications else "none"

        selected: list[TestCase] = []
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
                matched = [tc for tc in test_cases if tc.id == selector]
                if not matched:
                    print(f"Error: --tc {selector!r} not found. Available IDs: {[tc.id for tc in test_cases[:5]]}...", file=sys.stderr)
                    sys.exit(1)
                selected.extend(matched)
        test_cases = selected
    elif args.limit:
        test_cases = test_cases[: args.limit]

    # When running steps-only (no modifications), deduplicate by sample_id so
    # test cases sharing the same base steps are only executed once.
    if getattr(args, "steps_only", False):
        seen_samples: set[str] = set()
        deduped: list[TestCase] = []
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

    extra_info = {
        "Runs per test case": str(args.runs),
        "Timeout per event": f"{timeout_s}s" if timeout_s else "none",
        "Judge": judge_label,
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
        elif provider == "google":
            from src.lnl.brain import GeminiBrain
            return GeminiBrain(model=model)
        else:
            from src.lnl.brain import AnthropicBrain
            return AnthropicBrain(model=model)

    def _make_judge(provider, model):
        if provider == "openai":
            from src.lnl.judge import OpenAIJudge
            return OpenAIJudge(model=model)
        elif provider == "google":
            from src.lnl.judge import GeminiJudge
            return GeminiJudge(model=model)
        else:
            from src.lnl.judge import AnthropicJudge
            return AnthropicJudge(model=model)

    brain = _make_brain(args.provider, args.model)
    single_judges = [_make_judge(p, m) for p, m in parsed_judges]
    if len(single_judges) == 1:
        judge = single_judges[0]
    else:
        from src.lnl.judge import PanelJudge
        labels = [f"{p}/{m}" for p, m in parsed_judges]
        judge = PanelJudge(single_judges, judge_labels=labels)

    from src.lnl.benchmark import BenchmarkHarness
    harness = BenchmarkHarness(brain=brain, judge=judge)

    # Load shared mock tool configs from --mock-config files
    global_mock_tools: list[MockToolDef] = []
    for mc_path in getattr(args, "mock_config", None) or []:
        mc = load_mock_config(mc_path)
        global_mock_tools.extend(mc.tools)
        print(f"  Loaded mock config: {mc_path} ({len(mc.tools)} tools)")

    args.output.parent.mkdir(parents=True, exist_ok=True)

    # Continuation: if output file already exists, load completed runs and resume.
    # Key is (tc_index, run_index, seed). tc_index (0-based file position) is the
    # reliable unique identifier — tc_id is NOT unique (80 IDs × 6 mod-type variants).
    # Different seed values coexist in the same file; continuation is per-seed.
    # Legacy results (tc_index=-1) fall back to (tc_id, run_index) keying.
    completed: set[tuple[int, int, Optional[int]]] = set()  # (tc_index, run_index, seed)
    completed_legacy: set[tuple[str, int]] = set()
    all_tc_results: list[TestCaseResult] = []
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
                    all_tc_results.append(TestCaseResult.model_validate(data))
            except Exception:
                pass
        n_done = len(completed) + len(completed_legacy)
        if n_done:
            print(f"Resuming: {n_done} runs already complete, continuing from checkpoint.")
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

    runs_output = runs_path_from_eval_path(args.output)

    def _run_one(tc_idx: int, tc: TestCase, run_idx: int) -> Optional[TestCaseResult]:
        mod_type_str = tc.modifications[0].mod_type.value if tc.modifications else "none"
        label = f"{tc.id}[{mod_type_str}]"
        if args.runs > 1:
            label += f" run={run_idx+1}/{args.runs}"

        tqdm.write(f"\n{label}")
        msg_count = [0]
        tc_start = time.monotonic()
        raw_events: list[RawEventData] = []

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

        def _on_event_result(result: EventResult, is_step: bool, _args=args):
            status = "PASS" if result.passed else "FAIL"
            tag = " (baseline)" if is_step else ""
            tqdm.write(f"  [{status}] {result.event_id}{tag}: {result.expected[:70]}")
            if not result.passed:
                tqdm.write(f"         → {result.reasoning[:120]}")

        def _on_raw_event(raw: RawEventData):
            raw_events.append(raw)

        def _on_mod_applied(mod, _tc=tc):
            tqdm.write(
                f"  ── [{mod.mod_type.value}/{mod.ambiguity.value}] {mod.id}: "
                f"{mod.intent[:70]}"
            )
            pbar.set_description(f"Eval [{_tc.id}] {mod.mod_type.value}")

        def _on_message(_msg, _label=label, _count=msg_count, _start=tc_start):
            _count[0] += 1
            elapsed = time.monotonic() - _start
            pbar.set_postfix_str(
                f"{_label}  msgs={_count[0]}  {elapsed:.0f}s", refresh=True
            )

        event_results, mod_results = execute_test_case(
            tc, brain, harness, timeout_s,
            debug_messages=getattr(args, "debug_messages", False),
            steps_only=getattr(args, "steps_only", False),
            max_chain_depth=args.max_chain_depth,
            global_mock_tools=global_mock_tools or None,
            progress_callback=_on_message,
            on_event_result=_on_event_result,
            on_mod_applied=_on_mod_applied,
            on_raw_event=_on_raw_event,
            steps_snapshot=steps_snapshot,
            snapshot_out=snapshot_out,
        )

        # Store completed snapshot and signal waiting workers
        if reuse_steps and snapshot_out and tc.sample_id:
            key = (tc.sample_id, run_idx)
            with _snapshot_registry_lock:
                if snapshot_out:
                    _snapshots[key] = snapshot_out[0]
                _snapshot_events[key].set()
        pass_rate = (
            sum(1 for e in event_results if e.passed) / len(event_results)
            if event_results else None
        )
        result = TestCaseResult(
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
        )
        raw_result = RawTestCaseResult(
            tc_id=tc.id,
            sample_id=tc.sample_id,
            tc_index=tc_idx,
            seed=seed,
            name=tc.name,
            domain=tc.domain,
            run_index=run_idx,
            events=raw_events,
            modifications=mod_results,
        )
        passed_n = sum(1 for e in event_results if e.passed)
        total_n = len(event_results)
        rate_str = f"{pass_rate:.0%}" if pass_rate is not None else "N/A"
        tqdm.write(f"  → pass={passed_n}/{total_n} ({rate_str})")
        with write_lock:
            with open(runs_output, "a") as rf:
                rf.write(raw_result.model_dump_json() + "\n")
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
        timestamp=datetime.now().isoformat(),
        input_path=str(args.input),
        output_path=str(args.output),
        runs_path=str(runs_output),
        model=args.model,
        provider=args.provider,
        judge_model=judge_model,
        judge_provider=judge_provider,
        judge_specs=[f"{p}/{m}" for p, m in parsed_judges] if len(parsed_judges) > 1 else [],
        runs=args.runs,
        workers=workers,
        timeout_s=timeout_s,
        seed=seed,
        steps_only=getattr(args, "steps_only", False),
        max_chain_depth=args.max_chain_depth,
        mock_config_paths=[str(p) for p in (getattr(args, "mock_config", None) or [])],
        tc_filter=getattr(args, "tc", None),
        limit=getattr(args, "limit", None),
        is_continuation=is_continuation,
    )

    # Initialize runs artifact file (truncate on new run, append on continuation)
    if not is_continuation:
        runs_output.parent.mkdir(parents=True, exist_ok=True)
        runs_output.write_text("")  # create/truncate
    print(f"Runs:   {runs_output}")

    with open(args.output, file_mode) as f:
        f.write(run_config.model_dump_json() + "\n")
        f.flush()
        with tqdm(total=total_runs, initial=n_skipped, unit="run", desc="Evaluating") as pbar:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(_run_one, tc_idx, tc, run_idx): (tc_idx, tc, run_idx)
                    for tc_idx, tc, run_idx in pending_runs
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
                    except Exception as e:
                        tqdm.write(f"FAILED {label} run={run_idx}: {e}", file=sys.stderr)
                    pbar.update(1)

    # Write summary
    summary = _compute_summary(all_tc_results)
    with open(args.output, "a") as f:
        f.write(summary.model_dump_json() + "\n")

    print()
    print(f"Complete. Output: {args.output}")
    print(f"Mean pass rate: {summary.mean_pass_rate:.3f}  std: {summary.pass_rate_std:.3f}")
    return args.output


_STEP_EVENT_ID = re.compile(r"^S\d+$")


def _compute_summary(results: list[TestCaseResult]) -> EvalSummary:
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

    # Compute per-result effective events (step events only for the base TC per sample).
    all_events: list[EventResult] = []
    pass_rates: list[float] = []
    for r in results:
        is_base = r.tc_id in base_tc_ids
        effective = [
            e for e in r.events
            if is_base or not _STEP_EVENT_ID.match(e.event_id)
        ]
        all_events.extend(effective)
        if effective:
            pass_rates.append(sum(1 for e in effective if e.passed) / len(effective))
        # TCs with no evaluable events are excluded from pass rate — not counted as passing.

    mean_pass_rate = mean(pass_rates) if pass_rates else 0.0

    # Behavioral consistency: mean of per-TC std devs across runs.
    # Groups results by tc_id, computes std dev within each group, then averages.
    # Requires --runs > 1; returns 0.0 when each TC has only one run.
    by_tc: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.pass_rate is not None:
            by_tc[r.tc_id].append(r.pass_rate)
    per_tc_stds = [
        statistics.stdev(rates) for rates in by_tc.values() if len(rates) > 1
    ]
    pass_rate_std = mean(per_tc_stds)

    return EvalSummary(
        total_test_cases=total_test_cases,
        total_runs=total_runs,
        total_events=len(all_events),
        mean_pass_rate=mean_pass_rate,
        pass_rate_std=pass_rate_std,
        mean_event_input_tokens=mean([e.input_tokens for e in all_events]),
        mean_event_output_tokens=mean([e.output_tokens for e in all_events]),
        mean_event_latency_ms=mean([e.latency_ms for e in all_events]),
        mean_mod_input_tokens=mean([m.input_tokens for m in all_mods]),
        mean_mod_output_tokens=mean([m.output_tokens for m in all_mods]),
        mean_mod_latency_ms=mean([m.latency_ms for m in all_mods]),
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate test cases against the LNL runtime",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.data.evaluate -i outputs/data/zapier/20260322_120000/test_cases.jsonl
  python -m src.data.evaluate -i test_cases.jsonl --runs 3 --model claude-sonnet-4-6
  python -m src.data.evaluate -i test_cases.jsonl --model gpt-4o --judge-model claude-sonnet-4-6
""",
    )
    parser.add_argument(
        "--input", "-i",
        type=Path,
        required=True,
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
        help="Wall-clock timeout per step/event (not per test case); timed-out steps are marked failed and execution continues (default: 60)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Also print per-event evidence (expected/status/judge always shown)",
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
        help="Run specific test cases by 1-based index, ID, or ID[mod_type] (e.g. --tc 2 TC007 TC001[temporal]). Overrides --limit.",
    )
    parser.add_argument(
        "--steps-only",
        action="store_true",
        default=False,
        help="Run only the steps (baseline behavior); skip modifications and events",
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
        choices=["openai", "anthropic", "google"],
        default=None,
        help="Provider for judge model (inferred from judge-model if not specified). Ignored when --llm-judge is set.",
    )
    parser.add_argument(
        "--max-chain-depth",
        type=int,
        default=20,
        help="Max message chain depth per event (default: 20). Increase for workflows with many round-trips.",
    )
    parser.add_argument(
        "--mock-config",
        type=Path,
        action="append",
        default=None,
        metavar="YAML",
        help=(
            "YAML file with shared MockToolDef entries (can be specified multiple times). "
            "Loaded tools are merged with per-TestCase mock_tools; TestCase entries win on collision. "
            "Example: --mock-config config/mocks/lnl/email.yaml --mock-config config/mocks/lnl/slack.yaml"
        ),
    )
    add_common_args(parser)
    return parser


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
