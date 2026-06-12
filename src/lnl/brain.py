"""LLM provider abstraction — Brain interface and implementations."""
from __future__ import annotations

import datetime
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# Liveness pulse for external watchdogs (evaluate.py hang watchdog): bumped on
# every chat-completion ATTEMPT and every completed response. A hung HTTP read
# shows up as a frozen counter; healthy runs tick every few seconds.
LIVENESS = {"attempts": 0, "completions": 0, "prompt_tokens": 0, "completion_tokens": 0}
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import yaml

from .memory import FlatKeyValueMemory, MemoryBackend
from .types import (
    PATCHABLE_FIELDS,
    HistoryEntry,
    InferenceMetrics,
    KnowledgeGap,
    LLMResponse,
    Message,
    MessageType,
    ObjectDefinition,
    OutgoingMessage,
    Plan,
    PlanStep,
    PlanUpdate,
    ReactFinish,
    ReactStep,
    StateDelta,
    ToolCall,
    ToolResult,
)


# Default backend used when a brain isn't configured with one. Construction
# pattern mirrors `make_backend("flat")` to avoid a runtime import cycle.
_DEFAULT_MEMORY_BACKEND: MemoryBackend = FlatKeyValueMemory()

# JSON schema for the LLM response format (no tools)
LLM_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "updated_state": {
            "type": "string",
            "description": "Your complete updated state serialized as a JSON string, e.g. '{\"key\": \"value\"}'. Use '{}' if no state to store.",
        },
        "reply": {
            "type": "string",
            "description": "Your reply to the sender of the message.",
        },
        "outgoing_messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "recipient": {
                        "type": "string",
                        "description": "The object_id of the recipient.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content of the message.",
                    },
                },
                "required": ["recipient", "content"],
                "additionalProperties": False,
            },
            "description": "Messages to send to other objects.",
        },
        "reasoning": {
            "type": "string",
            "description": "Brief internal reasoning about what you did and why.",
        },
    },
    "required": ["updated_state", "reply", "outgoing_messages", "reasoning"],
    "additionalProperties": False,
}

# Schema extended with tool_calls — used when tools are registered.
# The tool_calls items schema is intentionally open (additionalProperties: true on arguments)
# so any tool can be called. The system prompt describes the available tools and their arguments.
LLM_RESPONSE_SCHEMA_WITH_TOOLS: dict[str, Any] = {
    **LLM_RESPONSE_SCHEMA,
    "properties": {
        **LLM_RESPONSE_SCHEMA["properties"],
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Unique ID for this tool call."},
                    "tool": {"type": "string", "description": "Tool name."},
                    "arguments": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": "Arguments for the tool, as described in the Tools section.",
                    },
                },
                "required": ["id", "tool", "arguments"],
                "additionalProperties": False,
            },
            "description": "Tool calls to execute. When present, the LLM will be called again with results before producing a final response.",
        },
    },
}


# ReAct step schema — one thought + one action per LLM call.
# action="tool_call": execute a tool and observe the result, then call again.
# action="finish": commit reply, state, and any outgoing messages/actions.
LLM_REACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {
            "type": "string",
            "description": "Your explicit reasoning about what to do next.",
        },
        "action": {
            "type": "string",
            "enum": ["tool_call", "finish"],
            "description": "The single action to take this step.",
        },
        "tool_call": {
            "type": "object",
            "description": "Legacy singular form. Prefer tool_calls (list). Kept for backward compatibility.",
            "properties": {
                "id": {"type": "string", "description": "Unique ID for this call."},
                "tool": {"type": "string", "description": "Tool name."},
                "arguments": {"type": "object", "additionalProperties": True},
                "plan_step_index": {
                    "type": ["integer", "null"],
                    "description": (
                        "Optional: index of the active plan step this tool call satisfies. "
                        "When set, the runtime captures the tool's structured return value "
                        "onto plan.steps[plan_step_index].result so downstream steps can "
                        "reference it directly."
                    ),
                },
            },
            "required": ["id", "tool", "arguments"],
            "additionalProperties": False,
        },
        "tool_calls": {
            "type": "array",
            "description": (
                "Preferred form. Batch of tool calls executed IN PARALLEL on the object's "
                "tool pool. The runtime blocks the turn until ALL listed tools complete, then "
                "calls you again with each tool's result in the message history. Results are "
                "also captured on plan.steps[plan_step_index].result. Use a list when you "
                "want concurrent execution of multiple tools; you'll always get the results "
                "back in this same turn before producing your final finish."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Unique ID for this call."},
                    "tool": {"type": "string", "description": "Tool name."},
                    "arguments": {"type": "object", "additionalProperties": True},
                    "plan_step_index": {
                        "type": ["integer", "null"],
                        "description": (
                            "Optional: index of the active plan step this tool call satisfies. "
                            "When set, the runtime captures the tool's structured return value "
                            "onto plan.steps[plan_step_index].result."
                        ),
                    },
                },
                "required": ["id", "tool", "arguments"],
                "additionalProperties": False,
            },
        },
        "plan_update": {
            "type": "object",
            "description": (
                "Optional plan maintenance. step_updates set TERMINAL outcomes only — a step that "
                "is not finished simply stays as it is; never write status='planned' or "
                "'in_progress' (non-terminal statuses are invalid and ignored)."
            ),
            "properties": {
                "goal": {"type": ["string", "null"]},
                "step_updates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": ["string", "null"], "description": "Step id, e.g. 's2'."},
                            "index": {"type": ["integer", "null"]},
                            "status": {
                                "type": "string",
                                "enum": ["done", "failed", "skipped"],
                                "description": "TERMINAL outcome only.",
                            },
                            "result_summary": {"type": ["string", "null"]},
                        },
                        "required": ["status"],
                        "additionalProperties": False,
                    },
                },
                "add_steps": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "status": {"type": ["string", "null"], "enum": [None, "done", "failed"]},
            },
            "additionalProperties": False,
        },
        "state_update": {
            "type": "object",
            "description": "Optional. Emit ONLY when a value genuinely changed. Omit entirely if nothing changed — do not invent updates.",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": ["set", "delete", "append"],
                    "description": "set: add/update a key. delete: remove a key. append: add to a list.",
                },
                "key": {"type": "string", "description": "The state key to modify."},
                "value": {"description": "New value (set/append). Omit for delete."},
            },
            "required": ["op", "key"],
            "additionalProperties": False,
        },
        "finish": {
            "type": "object",
            "description": "Present only when action=finish.",
            "properties": {
                "reply": {"type": "string", "description": "Reply to the message sender."},
                "outgoing_messages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "recipient": {"type": "string"},
                            "content": {"type": "string"},
                            "expects_reply": {
                                "type": "boolean",
                                "description": "Set true for Ask messages — when you need information back before you can continue. Leave false (default) for Tell messages: notifications, writes, and one-way forwards.",
                            },
                            "status": {
                                "type": ["string", "null"],
                                "enum": [None, "ok", "failed"],
                                "description": "Outcome signalling for replies. Set 'failed' when this outgoing reports an error to the asker; their plan step will flip to status='failed' on receipt.",
                            },
                            "error": {
                                "type": ["string", "null"],
                                "description": "Optional structured error detail when status='failed'.",
                            },
                        },
                        "required": ["recipient", "content"],
                        "additionalProperties": False,
                    },
                },
                "knowledge_gap": {
                    "type": "object",
                    "description": (
                        "Optional. Include ONLY when you genuinely do not know and cannot determine "
                        "the answer from your state or tools. Omit when you can answer or infer. "
                        "When present, the runtime records the gap and asks your peers automatically."
                    ),
                    "properties": {
                        "question": {"type": "string", "description": "The specific question or topic you cannot answer."},
                        "context": {"type": "string", "description": "Optional: what you do know, or why you're uncertain."},
                    },
                    "required": ["question"],
                    "additionalProperties": False,
                },
                "status": {
                    "type": ["string", "null"],
                    "enum": [None, "ok", "failed"],
                    "description": "Optional turn outcome. Set 'failed' to signal that your work could not complete — the runtime will propagate a failure REPLY to any asker awaiting you, and their plan step flips to 'failed' rather than 'done'.",
                },
                "error": {
                    "type": ["string", "null"],
                    "description": "Optional structured error detail when status='failed'.",
                },
            },
            "required": ["reply"],
            "additionalProperties": False,
        },
    },
    "required": ["thought", "action"],
    "additionalProperties": False,
}


_PROMPT_CONFIG: Optional[dict] = None

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "config" / "prompts" / "lnl"


def _load_prompt_config(prompt_file: str = "object.yaml") -> dict:
    """Load the prompt config from config/prompts/lnl/<prompt_file>."""
    global _PROMPT_CONFIG
    if prompt_file == "object.yaml":
        if _PROMPT_CONFIG is None:
            with open(_PROMPTS_DIR / prompt_file) as f:
                _PROMPT_CONFIG = yaml.safe_load(f)
        return _PROMPT_CONFIG
    with open(_PROMPTS_DIR / prompt_file) as f:
        return yaml.safe_load(f)


def _message_label(msg: Message) -> str:
    """Return a human-readable label for a message, so the LLM knows its type."""
    if msg.type == MessageType.HEARTBEAT:
        return "Heartbeat"
    if msg.type == MessageType.EVENT:
        if msg.sender in ("__system__", "__external__"):
            return "External event"
        return f"Event from {msg.sender}"
    if msg.type == MessageType.ADMIN:
        return "Admin"
    if msg.type == MessageType.REPLY and isinstance(msg.sender, str) and msg.sender.startswith("__tool__:"):
        tool_name = msg.sender[len("__tool__:"):]
        call_id = msg.reference or ""
        id_part = f" (call {call_id})" if call_id else ""
        return f"Tool result{id_part} from {tool_name}"
    if msg.type == MessageType.REPLY:
        return f"Reply from {msg.sender}"
    if msg.sender == "__user__":
        return "User instruction"
    if msg.expects_reply:
        return f"Ask from peer: {msg.sender}"   # sender expects a reply
    return f"Tell from peer: {msg.sender}"       # fire-and-forget, no reply expected


def _peer_interaction_loop(pending_timeout_seconds: float, heartbeat_interval_seconds: float) -> str:
    return f"""
  ## Peer Sends: Ask vs. Tell

  Every outgoing message is either an **Ask** or a **Tell**.

  **Tell (`expects_reply: false`, the DEFAULT):** informing, writing,
  forwarding, notifying. No reply expected. Send and immediately finish or
  continue to the next action. Examples: forwarding an event, writing a
  record, posting a notification, triggering a downstream action.

  **Ask (`expects_reply: true`):** you need information back before you can
  continue. The reply arrives on a LATER turn as a separate message. Use
  ONLY when you cannot proceed without the peer's answer.

  **Rule:** If you already have all the data you need, send Tells to all
  relevant peers and finish in one step. Do not defer action you can do now.

  **Heartbeat** (every {heartbeat_interval_seconds:.0f}s, prefix `[system time: <ts>]`):
  Review your state for time-sensitive conditions or emit proactive outgoing_messages if warranted."""


def _active_plan_mode_note(mode: str) -> str:
    """Mode-specific addendum injected after the active_plan block.

    In DAG mode this instructs the executor to fan out every step listed in
    the rendered `ready:` set in the same finish, rather than dispatching one
    step per turn. Returns empty string in sequential mode so existing prompts
    render byte-identically.
    """
    if mode == "dag":
        return (
            "\n**DAG mode — fan out every UNCONDITIONAL ready step in ONE finish; honor conditions.**\n"
            "The `ready:` list above is the wavefront: every step whose "
            "dependencies are satisfied. For each READY step, decide its "
            "fate this turn — do not defer:\n"
            "1. **Conditional steps.** If the step description names a "
            "condition (words like `if`, `when`, `only when`, `unless`, "
            "`provided that`, or any clause that gates the action on a "
            "specific value), evaluate the condition against the captured "
            "results of its `deps` BEFORE dispatching:\n"
            "   - Condition TRUE → dispatch normally (rules below).\n"
            "   - Condition FALSE → DO NOT dispatch. Instead, mark the step "
            "`skipped` via `plan_update.step_updates` in THIS finish. "
            "Skipping is a positive outcome — it preserves correctness.\n"
            "   - Cannot tell yet (deps haven't returned the needed value) "
            "→ leave the step `planned`; it will become ready again later.\n"
            "2. **Unconditional steps.** Dispatch every one in this single "
            "finish. If 3 unconditional steps are ready, your finish MUST "
            "produce 3 entries across `outgoing_messages` and `tool_calls` "
            "combined. Do NOT hold them back across turns.\n"
            "   - kind=tell/ask → one `outgoing_messages` entry per step "
            "(addressed to `step.target`).\n"
            "   - kind=tool → one `tool_calls` entry per step (tool name = "
            "`step.target`, arguments from the step description).\n"
            "   - kind=reason → close via `state_update` and "
            "`plan_update.step_updates` in this same finish.\n"
            "A step stays unready ONLY when its `deps=[...]` lists an id "
            "whose `result` has not been captured yet.\n"
            "Step ids `s1`, `s2`, `s3` are identifiers, not an order. Two "
            "unconditional ready steps with `deps=[]` BOTH fire (or both "
            "get evaluated) now, regardless of which appears first in the "
            "plan listing."
        )
    return ""


def _render_active_plan(plan: Optional[Plan], mode: str = "sequential") -> str:
    """Render the active plan for the prompt.

    Each step is identified by its stable string id (e.g., 's1', 's2'). The LLM
    can reference any earlier step's captured result in NL — e.g., "use the
    URL from s2.result". Captured step results are rendered in their native
    shape (NL for peer replies, JSON for tool returns) so downstream steps can
    reference them directly without state-write hops.

    When `mode == "dag"` the rendering surfaces a `ready:` header listing every
    step whose `depends_on` are all in terminal status (done/skipped) and which
    is itself still `planned`. Each such step also gets a `READY` tag in its
    line so the executor can dispatch them all in the same turn.
    """
    if plan is None:
        return "(none)"
    ready_ids: set[str] = set()
    if mode == "dag":
        done_ids = {(s.id or f"s{i+1}") for i, s in enumerate(plan.steps)
                    if s.status in ("done", "skipped")}
        for i, s in enumerate(plan.steps):
            sid = s.id or f"s{i+1}"
            if s.status != "planned":
                continue
            if all(d in done_ids for d in (s.depends_on or [])):
                ready_ids.add(sid)
    # Sequential-mode: deterministically point at the first 'planned' step
    # (skipping anything already in 'dispatched' status — that step is
    # currently in flight and the LLM should NOT pick it back up). The LLM
    # should execute this one next; everything before it is done/dispatched.
    next_planned_id: Optional[str] = None
    if mode != "dag":
        for i, s in enumerate(plan.steps):
            if s.status == "planned" and s.kind != "final":
                next_planned_id = s.id or f"s{i+1}"
                break
    lines = [f"goal: {plan.goal}", f"status: {plan.status}"]
    if mode == "dag":
        ready_list = ", ".join(sorted(ready_ids, key=lambda x: (len(x), x))) if ready_ids else "(none)"
        lines.append(f"ready: [{ready_list}]")
    else:
        lines.append(f"next: {next_planned_id if next_planned_id else '(none — all steps done/dispatched)'}")
    lines.append("steps:")
    if not plan.steps:
        lines.append("  (empty)")
    for i, s in enumerate(plan.steps):
        # Fall back to position-based id when the planner didn't supply one.
        sid = s.id or f"s{i+1}"
        target = f" → {s.target}" if s.target else ""
        deps = f"  deps={s.depends_on}" if s.depends_on else ""
        ready_tag = "  READY" if (mode == "dag" and sid in ready_ids) else ""
        next_tag = "  ← NEXT" if (mode != "dag" and sid == next_planned_id) else ""
        lines.append(f"  {sid}: {s.kind}{target}  status={s.status}{deps}{ready_tag}{next_tag}")
        lines.append(f"      description: \"{s.description}\"")
        if s.kind == "wait" and s.wait_predicate:
            src = f" (source hint: {s.wait_source})" if s.wait_source else ""
            lines.append(f"      waiting_for: \"{s.wait_predicate}\"{src}")
        if s.result is not None:
            kind_tag = f" ({s.result_kind})" if s.result_kind else ""
            if isinstance(s.result, str):
                rendered = f'"{s.result}"' if len(s.result) <= 240 else f'"{s.result[:237]}..."'
            else:
                try:
                    rendered = json.dumps(s.result, ensure_ascii=False, default=str)
                    if len(rendered) > 480:
                        rendered = rendered[:477] + "..."
                except (TypeError, ValueError):
                    rendered = repr(s.result)[:480]
            lines.append(f"      result{kind_tag}: {rendered}")
        elif s.result_summary:
            lines.append(f"      result: {s.result_summary}")
    return "\n".join(lines)


def _render_prior_plan_context(prior_plan: "Optional[Plan]", replan_question: Optional[str]) -> str:  # type: ignore[name-defined]
    """Build the 'Prior Plan Execution' block injected into replan-mode planner prompts.

    Empty string when prior_plan is None (first-time plan call). When set, lists
    each step's id/kind/target/status and the captured result (if any) so the
    planner can decide the continuation. `replan_question` is the deferred
    decision the planner now has full data to resolve.
    """
    if prior_plan is None:
        return ""
    lines = ["", "## Prior Plan Execution", f"goal: {prior_plan.goal or '(none)'}"]
    if replan_question:
        lines.append(f"")
        lines.append(f"## Decision Required")
        lines.append(f"{replan_question}")
    lines.append("")
    lines.append("Completed steps so far (use these results to decide continuation):")
    any_done = False
    for i, s in enumerate(prior_plan.steps):
        sid = s.id or f"s{i+1}"
        if s.status not in ("done", "skipped"):
            continue
        any_done = True
        target = f" -> {s.target}" if s.target else ""
        line = f"  {sid}: {s.kind}{target}  status={s.status}"
        lines.append(line)
        if s.result is not None:
            kind_tag = f" ({s.result_kind})" if s.result_kind else ""
            if isinstance(s.result, str):
                rendered = s.result if len(s.result) <= 240 else s.result[:237] + "..."
            else:
                try:
                    rendered = json.dumps(s.result, ensure_ascii=False, default=str)
                    if len(rendered) > 480:
                        rendered = rendered[:477] + "..."
                except (TypeError, ValueError):
                    rendered = repr(s.result)[:480]
            lines.append(f"      result{kind_tag}: {rendered}")
        elif s.result_summary:
            lines.append(f"      result: {s.result_summary}")
    if not any_done:
        lines.append("  (no completed steps yet)")
    lines.append("")
    lines.append(
        "Emit ONLY the continuation steps that should follow. Do NOT repeat the "
        "completed steps above — they will be preserved automatically. Your output "
        "is appended to the existing plan via add_steps."
    )
    return "\n".join(lines)


_MODIFICATION_RULES_MARKER = "MODIFICATION RULES"

_MODIFICATION_RULES_PLANNER_HINT = """

  ## ⚠ Behavior contains MODIFICATION RULES — evaluate before planning

  This object's `behavior` section ends with a `MODIFICATION RULES` block
  that an administrator appended at runtime. For EACH rule in that block:

  1. Read the rule's trigger predicate and evaluate it against THIS
     event's content. State the result explicitly in your step's
     `reasoning` (e.g. "M001 trigger: company == 'NorthPeak Labs' →
     event.company == 'Harbor Analytics' → FALSE → rule ignored").
  2. If TRUE: apply the rule's action — INSERT, OMIT, or ADJUST the
     specific baseline steps the rule names. Every OTHER baseline step
     still runs.
  3. If FALSE: ignore the rule entirely and plan the FULL baseline.
     Never carry the rule's action into a plan for non-matching events.

  Common pitfalls to avoid:
  - Entity-scoped rule (e.g. "company == X") applied to events from
    other entities — only X-matching events trigger.
  - Time-window rule (e.g. "before 09:00 Tuesday") applied outside the
    window — only in-window events trigger.
  - Rule says "do NOT do step S" → omit S, keep every other baseline
    step. Do not drop unrelated work.
"""


def build_planner_prompt(
    definition: ObjectDefinition,
    current_state,  # str (from LLM) or dict (from mock scripts)
    message,  # Message
    prompt_file: str = "planner_sequential.yaml",
    tools: str = "",
    prior_plan: "Optional[Plan]" = None,  # type: ignore[name-defined]
    replan_question: Optional[str] = None,
    replan_enabled: bool = True,
) -> str:
    """Build the planner system prompt from `planner_sequential.yaml` (the sequential default).

    The planner is a separate LLM call (Pre-Act Appendix D-inspired) that
    produces a multi-step plan BEFORE the executor starts dispatching. The
    plan persists in the object's active_plan and drives continuations.

    When `prior_plan` is set, this is a replan invocation: the prompt is
    enriched with the prior plan's completed-step results plus the deferred
    `replan_question`. The planner is expected to emit only the continuation
    steps (which the runtime appends via `add_steps`).

    `replan` is documented as a first-class step kind directly in the
    planner prompt (see planner_dag.yaml / planner_sequential.yaml). Whether
    a `kind=replan` step actually fires is controlled at the runtime layer
    by `SystemConfig.enable_replan_checkpoints`, not here.

    Conditionally appends a "MODIFICATION RULES" planner hint when, and ONLY
    when, the rendered behavior contains the marker — added by the admin
    path (see object_admin.yaml). Avoids polluting the planner prompt on
    events without modifications.
    """
    config = _load_prompt_config(prompt_file)
    template = config["system_prompt"]

    peers = "\n".join(f"- {p.object_id}: {p.relationship}" for p in definition.peers) or "(none)"
    if isinstance(current_state, dict):
        state_str = json.dumps(current_state, indent=2)
    elif current_state:
        state_str = str(current_state).strip()
    else:
        state_str = "(empty)"

    behavior_text = definition.behavior or "(none)"
    prior_plan_context = _render_prior_plan_context(prior_plan, replan_question)

    rendered = template.format(
        object_id=definition.object_id,
        role=definition.role,
        behavior=behavior_text,
        peers=peers,
        tools=tools or "(none)",
        current_state=state_str,
        sender=getattr(message, "sender", "(unknown)"),
        message_type=getattr(message, "type", "(unknown)").value if hasattr(getattr(message, "type", None), "value") else str(getattr(message, "type", "")),
        message_content=str(getattr(message, "content", "")),
        prior_plan_context=prior_plan_context,
    )

    # Gate the mod-aware hint on the behavior actually containing the marker
    # — keeps the planner prompt lean for the 95% of events that aren't
    # post-modification.
    if _MODIFICATION_RULES_MARKER in behavior_text:
        rendered = rendered + _MODIFICATION_RULES_PLANNER_HINT
    if not replan_enabled:
        rendered = rendered + (
            "\n\n## REPLAN IS DISABLED IN THIS RUNTIME\n"
            "`kind=replan` steps will NOT fire — a plan that defers its decision to a "
            "replan checkpoint stalls after the reads and the work never happens. Never "
            "emit `kind=replan`. Encode every conditional branch as a normal action step "
            "whose description states the condition and both outcomes, e.g. `tell "
            "<writer>: commit the update for the first eligible candidate per the step-s1 "
            "read; if none qualifies, send the hold/exception write instead`. The "
            "executor resolves the condition at execution time from the earlier step's "
            "result.\n"
        )
    return rendered


def build_evaluator_prompt(
    definition: ObjectDefinition,
    current_state,
    plan: "Optional[Plan]",  # type: ignore[name-defined]
    outgoing_messages: list,
    reply: str,
    message=None,  # the incoming Message this turn processed (context for evaluator)
    prompt_file: str = "evaluator.yaml",
    tool_calls_this_turn: "Optional[list[str]]" = None,
) -> str:
    """Build the evaluator system prompt from `evaluator.yaml`.

    Grades the executor's dispatches and state changes against each plan step.
    The incoming `message` is rendered for context so the evaluator knows what
    the executor was responding to.
    """
    config = _load_prompt_config(prompt_file)
    template = config["system_prompt"]

    peers = "\n".join(f"- {p.object_id}: {p.relationship}" for p in definition.peers) or "(none)"
    if isinstance(current_state, dict):
        state_str = json.dumps(current_state, indent=2)
    elif current_state:
        state_str = str(current_state).strip()
    else:
        state_str = "(empty)"

    # Incoming message block — rendered as context.
    if message is not None:
        mtype = getattr(message, "type", None)
        mtype_str = getattr(mtype, "value", str(mtype)) if mtype is not None else "?"
        incoming_message = (
            f"- from: {getattr(message, 'sender', '?')}\n"
            f"- type: {mtype_str}\n"
            f"- content: {str(getattr(message, 'content', ''))[:600]}"
        )
    else:
        incoming_message = "(incoming message not available)"

    # Plan section — renders the active plan with step ids, kinds and statuses.
    if plan is not None and getattr(plan, "steps", None):
        plan_steps_lines = []
        for i, s in enumerate(plan.steps):
            sid = s.id or f"s{i+1}"
            tgt = f" → {s.target}" if s.target else ""
            plan_steps_lines.append(f"  {sid}: {s.kind}{tgt}: {s.description}  status={s.status}")
        plan_section = (
            f"Goal: {plan.goal}\n\nPlanned steps (in order):\n"
            + "\n".join(plan_steps_lines)
        )
    else:
        plan_section = "(no plan)"

    if outgoing_messages:
        out_lines = []
        for m in outgoing_messages:
            recip = getattr(m, "recipient", "?")
            content = str(getattr(m, "content", ""))[:300]
            out_lines.append(f"  → {recip}: {content}")
        out_str = "\n".join(out_lines)
    else:
        out_str = "  (none — executor emitted no outgoings this turn)"

    # Tools-called-this-turn section — surfaces the actual tool execution
    # log so the evaluator can verify that plan `tool` steps fired.
    if tool_calls_this_turn:
        # Render as a count-per-tool list so the evaluator can see if a
        # tool was called multiple times (batched) vs once vs not at all.
        from collections import Counter as _Counter
        tc_counter = _Counter(tool_calls_this_turn)
        tools_lines = [f"  - {name} (×{n})" for name, n in tc_counter.items()]
        tools_str = "\n".join(tools_lines)
    else:
        tools_str = "  (none — no tools executed this turn)"

    return template.format(
        object_id=definition.object_id,
        role=definition.role,
        behavior=definition.behavior or "(none)",
        peers=peers,
        incoming_message=incoming_message,
        plan_section=plan_section,
        current_state=state_str,
        outgoing_messages=out_str,
        reply=str(reply).strip() or "(empty)",
        tool_calls_this_turn=tools_str,
    )


def build_wait_matcher_prompt(
    object_id: str,
    message,                   # the inbound EVENT message being matched
    candidates: list[dict],    # see _register_wait for full entry schema
    prompt_file: str = "wait_matcher.yaml",
) -> str:
    """Build the wait-matcher system prompt.

    The matcher receives the inbound event and the list of pending waits on
    this object. It must return either the id (`<trace_id>:<step_index>`) of
    the single concretely-matching wait, or null. Ambiguous matches MUST
    return null — silent misrouting is worse than starting a fresh plan.

    Each candidate is rendered with:
    - plan_goal / step_description / wait_predicate / expected_source —
      what the planner said it was waiting for.
    - originating_event — the message that triggered this plan (its
      identifying tokens are usually what the awaited event will reference).
    - prior_step_results — captured results from earlier plan steps (tool
      returns, peer replies) — the richest source of correlation tokens
      that weren't known at plan time (e.g. an order_id from a tool return).
    """
    config = _load_prompt_config(prompt_file)
    template = config["system_prompt"]
    sender = getattr(message, "sender", "(unknown)")
    content = str(getattr(message, "content", ""))
    if not candidates:
        candidates_str = "(none)"
    else:
        lines = []
        for c in candidates:
            cid = f"{c.get('trace_id','')}:{c.get('step_index','')}"
            src = c.get("wait_source") or "(any)"
            orig_sender = c.get("originating_sender") or "(unknown)"
            orig_content = (c.get("originating_content") or "").strip()
            if len(orig_content) > 320:
                orig_content = orig_content[:317] + "..."
            prior = c.get("prior_step_results") or []
            if prior:
                prior_lines = []
                for r in prior:
                    sid = r.get("step_id", "?") or "?"
                    kind = r.get("kind") or "?"
                    summary = (r.get("summary") or "").strip()
                    prior_lines.append(f"      - {sid} ({kind}): {summary}")
                prior_block = "\n".join(prior_lines)
            else:
                prior_block = "      (no completed steps yet)"
            lines.append(
                f"- id: {cid}\n"
                f"  plan_goal: {c.get('plan_goal','')}\n"
                f"  step_description: {c.get('step_description','')}\n"
                f"  wait_predicate: {c.get('wait_predicate','')}\n"
                f"  expected_source: {src}\n"
                f"  originating_event:\n"
                f"      from: {orig_sender}\n"
                f"      content: {orig_content}\n"
                f"  prior_step_results:\n{prior_block}"
            )
        candidates_str = "\n".join(lines)
    return template.format(
        object_id=object_id,
        event_source=sender,
        event_content=content,
        candidates=candidates_str,
    )


def _type_hint(json_schema: dict) -> str:
    """Human-readable shorthand for a JSON schema fragment, for the prompt."""
    t = json_schema.get("type")
    if t == "string":
        return "string"
    if t == "array":
        items = json_schema.get("items", {})
        if items.get("type") == "string":
            return "list of strings"
        if items.get("type") == "object":
            props = items.get("properties") or {}
            return "list of {" + ", ".join(props.keys()) + "}"
        return "list"
    return t or "value"


def _render_current_definition(definition: ObjectDefinition) -> str:
    """Render every patchable field's CURRENT value as a section block."""
    blocks: list[str] = []
    for spec in PATCHABLE_FIELDS:
        body = spec.renderer(getattr(definition, spec.name))
        blocks.append(f"## {spec.title}\n{body}")
    return "\n\n".join(blocks)


def _render_patchable_fields_spec() -> str:
    """Render the 'Patchable Fields' bulleted list from PATCHABLE_FIELDS."""
    lines: list[str] = []
    for spec in PATCHABLE_FIELDS:
        hint = _type_hint(spec.json_schema)
        lines.append(f"- `{spec.name}` ({hint}): {spec.description}")
        if spec.list_semantics_note:
            for note_line in spec.list_semantics_note.split("\n"):
                lines.append(f"  {note_line}")
    return "\n".join(lines)


def _render_response_format_fields(indent: str = "        ") -> str:
    """Render the JSON example body for `updated_definition` from the spec.

    Returns lines pre-prefixed with `indent` so they sit cleanly under the
    `updated_definition` block in the prompt's response-format example.
    Leading newline lets callers place the placeholder at end-of-line in
    the YAML template (avoids column-0 issues with the block scalar).
    """
    lines: list[str] = []
    for spec in PATCHABLE_FIELDS:
        lines.append(f'{indent}"{spec.name}": {spec.example_literal},  // optional')
    return "\n" + "\n".join(lines)


def build_admin_prompt(
    definition: ObjectDefinition,
    prompt_file: str = "object_admin.yaml",
) -> str:
    """Build the admin system prompt from `object_admin.yaml`.

    The admin prompt is a single-shot transform: the LLM sees the current
    definition and the administrator's NL instruction (passed separately as
    the inbound user message), and returns a patch with only the changed
    fields. No history, no tools, no React loop.

    The prompt template carries placeholders ({current_definition},
    {patchable_fields_spec}, {response_format_fields}) that are rendered
    from PATCHABLE_FIELDS (types.py) — so the YAML never names a specific
    patchable field.
    """
    config = _load_prompt_config(prompt_file)
    template = config["system_prompt"]

    return template.format(
        object_id=definition.object_id,
        current_definition=_render_current_definition(definition),
        patchable_fields_spec=_render_patchable_fields_spec(),
        response_format_fields=_render_response_format_fields(),
    )


VALID_STEP_KINDS = ("ask", "tell", "tool", "reason", "wait", "replan")


def _normalize_step_kind(raw: str) -> str:
    """Normalize step kind, accepting 'effect' as a back-compat alias for 'reason'."""
    k = (raw or "").strip().lower()
    if k == "effect":
        return "reason"
    return k


def plan_dict_to_plan(plan_dict: dict, trace_id: Optional[str] = None) -> "Plan":  # type: ignore[name-defined]
    """Convert a raw plan dict (matching PLANNER_RESPONSE_SCHEMA) into a Plan
    object the runtime can use. Filters out the terminal `final` step — it's
    a planning marker, not an executable step.

    Each executable step gets a stable string id (from the planner, or
    auto-assigned as 's{n}' if missing). Ids are stable across plan updates
    and referenced by later steps and by the evaluator's per-step criteria.
    """
    goal = plan_dict.get("goal", "")
    steps_in = plan_dict.get("steps", []) or []
    steps_out: list[PlanStep] = []
    auto_idx = 0
    used_ids: set[str] = set()
    for s in steps_in:
        if not isinstance(s, dict):
            continue
        kind = _normalize_step_kind(s.get("kind") or "")
        if kind not in VALID_STEP_KINDS:
            # `final` marker step — drop. Auto-close handles plan completion.
            continue
        # ask/tell target a peer; tool targets a tool name; reason/wait/replan have no peer target
        target = s.get("target") if kind in ("ask", "tell", "tool") else None
        # Step id: prefer planner-supplied; fall back to auto-assigned 's{n}'.
        # Deduplicate if the planner emits collisions.
        auto_idx += 1
        raw_id = (s.get("id") or "").strip()
        step_id = raw_id or f"s{auto_idx}"
        if step_id in used_ids:
            step_id = f"{step_id}_{auto_idx}"
        used_ids.add(step_id)
        depends_on_raw = s.get("depends_on") or []
        depends_on = [d for d in depends_on_raw if isinstance(d, str) and d]
        # Wait-step fields: only attached when kind=="wait". A wait with no
        # predicate is unmatchable, so the runtime would never close it; we
        # tolerate that here and let the runtime's stale-sweep recover.
        wait_predicate = None
        wait_source = None
        wait_timeout_seconds: Optional[float] = None
        if kind == "wait":
            wp = s.get("wait_predicate")
            wait_predicate = wp.strip() if isinstance(wp, str) and wp.strip() else None
            ws = s.get("wait_source")
            wait_source = ws.strip() if isinstance(ws, str) and ws.strip() else None
            wt = s.get("wait_timeout_seconds")
            if isinstance(wt, (int, float)) and wt > 0:
                wait_timeout_seconds = float(wt)
        replan_question = None
        if kind == "replan":
            rq = s.get("replan_question")
            replan_question = rq.strip() if isinstance(rq, str) and rq.strip() else None
        steps_out.append(PlanStep(
            id=step_id,
            kind=kind,
            description=s.get("description", "") or "",
            target=target,
            depends_on=depends_on,
            status="planned",
            result_summary=None,
            wait_predicate=wait_predicate,
            wait_source=wait_source,
            wait_timeout_seconds=wait_timeout_seconds,
            replan_question=replan_question,
        ))
    return Plan(goal=goal, steps=steps_out, status="active", trace_id=trace_id)


def build_system_prompt(
    definition: ObjectDefinition,
    current_state,  # str (from LLM) or dict (from mock scripts)
    tools: str = "",
    react_cross_objects: bool = True,
    pending_timeout_seconds: float = 90.0,
    heartbeat_interval_seconds: float = 30.0,
    active_plan: Optional["Plan"] = None,  # type: ignore[name-defined]
    prompt_file: str = "object.yaml",
    planner_mode: str = "dag",
) -> str:
    """Build the system prompt from the YAML template and an ObjectDefinition."""
    config = _load_prompt_config(prompt_file)
    template = config["system_prompt"]

    peers = ""
    if definition.peers:
        peers = "\n".join(f"- {p.object_id}: {p.relationship}" for p in definition.peers)

    skills_str = ""
    if definition.skills:
        skills_str = "\n".join(f"- {s}" for s in definition.skills)

    event_sources = ""
    if definition.event_sources:
        event_sources = "\n".join(f"- {s}" for s in definition.event_sources)

    substitutions = {
        "object_id": definition.object_id,
        "role": definition.role,
        "behavior": definition.behavior or "(none)",
        "skills": skills_str or "(none)",
        "peers": peers or "(none)",
        "event_sources": event_sources or "(none)",
        "current_state": (json.dumps(current_state, indent=2) if isinstance(current_state, dict) else current_state.strip()) if current_state else "(empty)",
        "active_plan": _render_active_plan(active_plan, mode=planner_mode),
        "active_plan_mode_note": _active_plan_mode_note(planner_mode),
        "tools": tools or "(none)",
        "peer_interaction_loop": _peer_interaction_loop(pending_timeout_seconds, heartbeat_interval_seconds) if react_cross_objects else "",
    }
    result = template
    for key, value in substitutions.items():
        result = result.replace("{" + key + "}", value)
    return result


def _is_tool_reply(msg: Message) -> bool:
    return (
        msg.type == MessageType.REPLY
        and isinstance(msg.sender, str)
        and msg.sender.startswith("__tool__:")
    )


def _render_tool_reply(msg: Message) -> str:
    """Render an async tool REPLY using the same framing sync mode produces
    inline in the ReAct loop: '[Tool result (call X) from Y] (status=ok|failed): <content>'.

    Sync builds this string directly in object._run_react_cycle and appends it
    as the next user message. Async wraps the same content in a Message and
    routes it via the mailbox; without this special-cased render the prompt
    would prefix it with '[system time:...] [msg-id:...] [in-reply-to:...]'
    making it visually identical to a peer reply — which empirically caused
    the LLM to misread tool results as peer chatter.
    """
    tool_name = msg.sender[len("__tool__:"):]
    call_id = msg.reference or ""
    id_part = f" (call {call_id})" if call_id else ""
    status_str = "failed" if (msg.status == "failed" or msg.error) else "ok"
    return f"[Tool result{id_part} from {tool_name}] (status={status_str}): {msg.content}"


def _build_chat_messages(
    sys_prompt: str,
    history: Sequence[HistoryEntry],
    message: Message,
) -> list[dict[str, str]]:
    """Build the initial chat message list with labeled history and new message.

    History entries are grouped by task_id (Plan.id) so the LLM can reason
    about which past exchanges belong together. Group order follows first-
    occurrence order in history; entries with task_id=None (admin / no-plan
    / broadcast) go under an "Other" header.

    Returns a list starting with {"role": "system", ...}. Anthropic implementations
    should strip this entry and pass it separately.
    """
    msgs: list[dict[str, str]] = [{"role": "system", "content": sys_prompt}]
    if history:
        # Two-level grouping — history[task][plan]. Outer by task_id, inner
        # by plan_id (the plan generation). Both groupings preserve first-
        # occurrence order. None task_id renders under "-- Other --"; None
        # plan_id (only the orphan bucket today) skips the inner header.
        task_groups: "dict[Optional[str], dict[Optional[str], list[Message]]]" = {}
        for entry in history:
            inner = task_groups.setdefault(entry.task_id, {})
            inner.setdefault(entry.plan_id, []).append(entry.message)
        history_lines: list[str] = []
        for task_id, plan_buckets in task_groups.items():
            if task_id is None:
                history_lines.append("  -- Other --")
            else:
                history_lines.append(f"  -- Task {task_id[:8]} --")
            for plan_id, msgs_in_group in plan_buckets.items():
                if plan_id is not None:
                    history_lines.append(f"    -- Plan {plan_id[:8]} --")
                for msg in msgs_in_group:
                    if _is_tool_reply(msg):
                        history_lines.append(f"      {_render_tool_reply(msg)}")
                    else:
                        history_lines.append(f"      [{_message_label(msg)}]: {msg.content}")
        msgs.append({"role": "user", "content": "[Past messages — already reflected in your state]\n" + "\n".join(history_lines)})
        msgs.append({"role": "assistant", "content": "Understood. What is the new message?"})
    if _is_tool_reply(message):
        msgs.append({"role": "user", "content": _render_tool_reply(message)})
    else:
        ts = message.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        id_tag = f" [msg-id: {message.id}]" if message.id else ""
        reply_tag = f" [in-reply-to: {message.in_reply_to}]" if message.in_reply_to else ""
        msgs.append({"role": "user", "content": f"[system time: {ts}]{id_tag}{reply_tag} [{_message_label(message)}]: {message.content}"})
    return msgs


EVALUATOR_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "criteria": {
            "type": "array",
            "description": (
                "Per-sub-item grades. ONE criterion per required sub-item "
                "(per field, per destination, per audit-log entry, per content "
                "detail) — NOT one per plan step. Multiple criteria MAY share "
                "the same step_index when the step requires several sub-items."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "step_index": {"type": "integer", "minimum": 0},
                    "step_id": {
                        "type": "string",
                        "description": "The plan step id (e.g. 's1'). Matches the rendered plan.",
                    },
                    "sub_item": {
                        "type": "string",
                        "description": (
                            "Short NL label naming the specific thing being checked, "
                            "e.g. 'slack notification contains AI summary', "
                            "'airtable row has role=Designer for Jordan Mitchell', "
                            "'audit log entry for LinkedIn platform exists'. "
                            "Use this to disambiguate when a step has multiple sub-items."
                        ),
                    },
                    "status": {"type": "string", "enum": ["PASS", "FAIL", "SKIP"]},
                    "diagnostic": {
                        "type": "string",
                        "description": (
                            "Failing diagnostics must be specific enough for the executor "
                            "to fix: name the field, the missing value, the omitted destination, "
                            "the audit-log entry not present, etc."
                        ),
                    },
                },
                "required": ["step_index", "step_id", "sub_item", "status", "diagnostic"],
                "additionalProperties": False,
            },
        },
        "feedback": {"type": "string"},
    },
    "required": ["verdict", "criteria", "feedback"],
    "additionalProperties": False,
}


PLANNER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": "One short sentence summarizing what the plan accomplishes.",
        },
        "steps": {
            "type": "array",
            "description": "Numbered steps in execution order. Last step must be kind=final.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": (
                            "Stable short id for this step, e.g. 's1', 's2', 's3'. "
                            "Used to reference this step's result from later steps' "
                            "descriptions (e.g. 'post the URL from s2.result to Slack')."
                        ),
                    },
                    "step_number": {"type": "integer", "minimum": 1},
                    "kind": {"type": "string", "enum": ["tell", "ask", "tool", "reason", "wait", "replan", "effect", "final"]},
                    "target": {"type": "string", "description": "Declared peer id (for ask/tell), tool name (for tool), 'self' (for reason/wait/replan), or 'final'."},
                    "description": {"type": "string"},
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of step ids whose results this step uses. "
                            "Make explicit any data flow between steps so the executor "
                            "and evaluator can reason about it."
                        ),
                    },
                    "reasoning": {"type": "string"},
                    "wait_predicate": {
                        "type": ["string", "null"],
                        "description": (
                            "Required when kind=='wait'. Natural-language description of the "
                            "external event being awaited, including identifying tokens "
                            "(order id, ticket id, customer email) derivable from prior step "
                            "results so the runtime can correlate the inbound event back to "
                            "this plan. Omitted for non-wait steps."
                        ),
                    },
                    "wait_source": {
                        "type": ["string", "null"],
                        "description": (
                            "Optional hint when kind=='wait'. The event source/sender id you "
                            "expect the event to arrive from (e.g. 'email-gateway'). Used as "
                            "a soft prefilter; the matcher may still consider other sources."
                        ),
                    },
                    "wait_timeout_seconds": {
                        "type": ["number", "null"],
                        "description": (
                            "Optional when kind=='wait'. Maximum seconds to wait before the "
                            "step fails and the plan closes. Defaults to a long window "
                            "(e.g. 24h) when omitted."
                        ),
                    },
                    "replan_question": {
                        "type": ["string", "null"],
                        "description": (
                            "Required when kind=='replan'. One short sentence describing the "
                            "decision the planner has deferred and will resolve on re-entry "
                            "once `depends_on` step results land (e.g. 'decide whether to "
                            "send a reorder based on s1.result.quantity vs reorder_threshold')."
                        ),
                    },
                },
                "required": ["id", "step_number", "kind", "target", "description", "reasoning"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["goal", "steps"],
    "additionalProperties": False,
}


def _build_admin_response_schema() -> dict[str, Any]:
    """Build the admin-response JSON schema from PATCHABLE_FIELDS (types.py).

    The patchable set is the single source of truth — adding a new patchable
    field there updates the schema, the apply step, and the prompt together.
    """
    return {
        "type": "object",
        "properties": {
            "thought": {
                "type": "string",
                "description": "Brief reasoning about which fields the admin's instruction changes.",
            },
            "finish": {
                "type": "object",
                "properties": {
                    "reply": {
                        "type": "string",
                        "description": "Short confirmation or clarifying question for the administrator.",
                    },
                    "updated_definition": {
                        "type": "object",
                        "description": "Patch with ONLY the changed fields. Omit entirely when asking for clarification.",
                        "properties": {f.name: f.json_schema for f in PATCHABLE_FIELDS},
                        "additionalProperties": False,
                    },
                },
                "required": ["reply"],
                "additionalProperties": False,
            },
        },
        "required": ["thought", "finish"],
        "additionalProperties": False,
    }


ADMIN_RESPONSE_SCHEMA: dict[str, Any] = _build_admin_response_schema()


WAIT_MATCHER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "match": {
            "type": ["string", "null"],
            "description": (
                "The candidate id (formatted '<trace_id>:<step_index>') of the "
                "pending wait that this inbound event satisfies, or null when "
                "no candidate is a concrete match. Ambiguous candidates MUST "
                "return null rather than guess — a wrong match silently "
                "hijacks an unrelated workflow."
            ),
        },
        "reasoning": {
            "type": "string",
            "description": "One short sentence explaining the decision.",
        },
    },
    "required": ["match", "reasoning"],
    "additionalProperties": False,
}


class LLMBrain(ABC):
    """Abstract interface for LLM processing backends."""

    # Memory backend the runtime is using. Brains use it to (a) inject the
    # right `state_update` schema fragment into ReAct requests and (b) parse
    # the response back into typed deltas. Defaults to the flat backend so
    # tests and ad-hoc usage Just Work.
    memory_backend: MemoryBackend = _DEFAULT_MEMORY_BACKEND

    def set_memory_backend(self, backend: MemoryBackend) -> None:
        self.memory_backend = backend

    @abstractmethod
    def call(
        self,
        messages: list[dict],
        schema: dict,
        *,
        object_id: str | None = None,
    ) -> tuple[LLMResponse, InferenceMetrics]:
        """Single LLM call. messages is the fully-assembled conversation (system + user turns).
        schema is the JSON schema for structured output.
        object_id is optional context used by MockBrain for script lookup.
        """
        ...

    @abstractmethod
    def react_call(
        self,
        messages: list[dict],
        *,
        object_id: str | None = None,
    ) -> tuple[ReactStep, InferenceMetrics]:
        """One ReAct step: returns a single thought + action and its metrics.

        The caller appends the step and its observation to `messages` and calls
        again until action == "finish".
        """
        ...

    def plan_call(
        self,
        system_prompt: str,
        *,
        object_id: str | None = None,
    ) -> tuple[dict, InferenceMetrics]:
        """One planning call: returns a raw plan dict (matching PLANNER_RESPONSE_SCHEMA)
        and its metrics. Subclasses override for efficiency."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement plan_call. "
            "Use OpenAIBrain or AzureBrain, or override plan_call."
        )

    def evaluate_call(
        self,
        system_prompt: str,
        *,
        object_id: str | None = None,
    ) -> tuple[dict, InferenceMetrics]:
        """One evaluation call: returns a raw evaluator-result dict (matching
        EVALUATOR_RESPONSE_SCHEMA) and its metrics. Subclasses override for
        efficiency. Caller interprets the criterion list / verdict / feedback.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement evaluate_call. "
            "Use OpenAIBrain or AzureBrain, or override evaluate_call."
        )

    def admin_call(
        self,
        system_prompt: str,
        admin_message: str,
        *,
        object_id: str | None = None,
    ) -> tuple[dict, InferenceMetrics]:
        """One admin call: a single LLM transform that parses an admin
        instruction and returns a definition patch.

        Returns the raw dict matching ADMIN_RESPONSE_SCHEMA — caller extracts
        `finish.reply` and the optional `finish.updated_definition` patch.
        Subclasses override for efficiency.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement admin_call. "
            "Use OpenAIBrain, AzureBrain, AnthropicBrain, or MockBrain, "
            "or override admin_call."
        )

    def match_wait_call(
        self,
        system_prompt: str,
        *,
        object_id: str | None = None,
    ) -> tuple[dict, InferenceMetrics]:
        """One wait-matcher call: returns {'match': '<trace_id>:<step_index>' | None,
        'reasoning': '...'} matching WAIT_MATCHER_RESPONSE_SCHEMA, plus its metrics.

        Used by the runtime to decide whether an inbound EVENT should be
        absorbed into an existing plan's pending `wait` step (rather than
        starting a fresh plan). Subclasses override for efficiency.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement match_wait_call. "
            "Use OpenAIBrain or AzureBrain, or override match_wait_call."
        )



class OpenAIBrain(LLMBrain):
    """Brain backed by the OpenAI API."""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: Optional[str] = None,
        temperature: float = 0.0,
        seed: Optional[int] = 42,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai package required. Install with: pip install openai")

        self.model = model
        self._temperature = temperature
        self._seed = seed
        # Same fail-fast timeout as the Azure client — see comment there.
        self._client = OpenAI(api_key=api_key or os.environ["OPENAI_API_KEY"],
                              timeout=120.0, max_retries=2)

    def call(
        self,
        messages: list[dict],
        schema: dict,
        *,
        object_id: str | None = None,
    ) -> tuple[LLMResponse, InferenceMetrics]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self._temperature,
            "max_completion_tokens": 32000,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "llm_response",
                    "schema": schema,
                },
            },
        }
        if self._seed is not None:
            kwargs["seed"] = self._seed

        t0 = time.time()
        resp = self._client.chat.completions.create(**kwargs)
        latency_ms = (time.time() - t0) * 1000

        metrics = InferenceMetrics(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )

        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                f"OpenAI response truncated (finish_reason=length) for object {object_id}. "
                "The output exceeded the model's max_tokens limit."
            )
        raw = _safe_json_loads(choice.message.content or "{}")
        return _parse_llm_result(raw), metrics

    def react_call(
        self,
        messages: list[dict],
        *,
        object_id: str | None = None,
    ) -> tuple[ReactStep, InferenceMetrics]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self._temperature,
            "max_completion_tokens": 32000,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "react_step", "schema": _build_react_schema(self.memory_backend)},
            },
        }
        if self._seed is not None:
            kwargs["seed"] = self._seed

        t0 = time.time()
        resp = self._client.chat.completions.create(**kwargs)
        latency_ms = (time.time() - t0) * 1000

        metrics = InferenceMetrics(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                f"OpenAI response truncated (finish_reason=length) for object {object_id}. "
                "The output exceeded the model's max_tokens limit."
            )
        raw = _safe_json_loads(choice.message.content or "{}")
        return _parse_react_step(raw, self.memory_backend), metrics

    def plan_call(
        self,
        system_prompt: str,
        *,
        object_id: str | None = None,
    ) -> tuple[dict, InferenceMetrics]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}],
            "temperature": self._temperature,
            "max_completion_tokens": 4000,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "plan", "schema": PLANNER_RESPONSE_SCHEMA},
            },
        }
        if self._seed is not None:
            kwargs["seed"] = self._seed
        t0 = time.time()
        resp = self._client.chat.completions.create(**kwargs)
        latency_ms = (time.time() - t0) * 1000
        metrics = InferenceMetrics(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                f"OpenAI plan response truncated for object {object_id}."
            )
        raw = _safe_json_loads(choice.message.content or "{}")
        return raw, metrics

    def evaluate_call(
        self,
        system_prompt: str,
        *,
        object_id: str | None = None,
    ) -> tuple[dict, InferenceMetrics]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}],
            "temperature": self._temperature,
            "max_completion_tokens": 4000,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "evaluator_result", "schema": EVALUATOR_RESPONSE_SCHEMA},
            },
        }
        if self._seed is not None:
            kwargs["seed"] = self._seed
        t0 = time.time()
        resp = self._client.chat.completions.create(**kwargs)
        latency_ms = (time.time() - t0) * 1000
        metrics = InferenceMetrics(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                f"OpenAI evaluator response truncated for object {object_id}."
            )
        raw = _safe_json_loads(choice.message.content or "{}")
        return raw, metrics

    def admin_call(
        self,
        system_prompt: str,
        admin_message: str,
        *,
        object_id: str | None = None,
    ) -> tuple[dict, InferenceMetrics]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"[Admin]: {admin_message}"},
            ],
            "temperature": self._temperature,
            "max_completion_tokens": 4000,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "admin_response", "schema": ADMIN_RESPONSE_SCHEMA},
            },
        }
        if self._seed is not None:
            kwargs["seed"] = self._seed
        t0 = time.time()
        resp = self._client.chat.completions.create(**kwargs)
        latency_ms = (time.time() - t0) * 1000
        metrics = InferenceMetrics(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                f"OpenAI admin response truncated for object {object_id}."
            )
        raw = _safe_json_loads(choice.message.content or "{}")
        return raw, metrics

    def match_wait_call(
        self,
        system_prompt: str,
        *,
        object_id: str | None = None,
    ) -> tuple[dict, InferenceMetrics]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}],
            "temperature": self._temperature,
            "max_completion_tokens": 512,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "wait_match", "schema": WAIT_MATCHER_RESPONSE_SCHEMA},
            },
        }
        if self._seed is not None:
            kwargs["seed"] = self._seed
        t0 = time.time()
        resp = self._client.chat.completions.create(**kwargs)
        latency_ms = (time.time() - t0) * 1000
        metrics = InferenceMetrics(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                f"OpenAI wait-matcher response truncated for object {object_id}."
            )
        raw = _safe_json_loads(choice.message.content or "{}")
        return raw, metrics


class AzureBrain(LLMBrain):
    """Brain backed by Azure OpenAI."""

    def __init__(
        self,
        model: str = "gpt-5.4-mini",
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        api_version: Optional[str] = None,
        temperature: float = 0.0,
        seed: Optional[int] = 42,
    ) -> None:
        try:
            from openai import AzureOpenAI
        except ImportError:
            raise ImportError("openai package required. Install with: pip install openai")

        self.model = model
        self._temperature = temperature
        self._seed = seed
        resolved_endpoint = endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
        if not resolved_endpoint:
            raise ValueError("Azure endpoint required. Set AZURE_OPENAI_ENDPOINT or pass endpoint=.")
        resolved_version = api_version or os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
        resolved_key = api_key or os.environ.get("AZURE_OPENAI_API_KEY")
        if not resolved_key:
            raise ValueError("Azure API key required. Set AZURE_OPENAI_API_KEY or pass api_key=.")
        # Explicit per-request timeout: the SDK default (600s, plus its own retries)
        # lets one dead connection hang an eval for tens of minutes with zero CPU —
        # observed as a frozen token counter on an ESTABLISHED-but-silent TLS socket.
        # 120s fails fast; our _create_with_filter_retry layer handles the retry.
        self._client = AzureOpenAI(
            api_key=resolved_key,
            azure_endpoint=resolved_endpoint,
            api_version=resolved_version,
            timeout=120.0,
            max_retries=2,
        )

    def call(
        self,
        messages: list[dict],
        schema: dict,
        *,
        object_id: str | None = None,
    ) -> tuple[LLMResponse, InferenceMetrics]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self._temperature,
            "max_completion_tokens": 32000,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "llm_response",
                    "schema": schema,
                },
            },
        }
        if self._seed is not None:
            kwargs["seed"] = self._seed

        t0 = time.time()
        try:
            resp = self._create_with_filter_retry(kwargs, object_id)
        except Exception as e:
            self._raise_if_content_filter(e, object_id)
            raise
        latency_ms = (time.time() - t0) * 1000

        metrics = InferenceMetrics(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )

        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                f"Azure OpenAI response truncated (finish_reason=length) for object {object_id}. "
                "The output exceeded the model's max_tokens limit."
            )
        raw = _safe_json_loads(choice.message.content or "{}")
        return _parse_llm_result(raw), metrics

    def react_call(
        self,
        messages: list[dict],
        *,
        object_id: str | None = None,
    ) -> tuple[ReactStep, InferenceMetrics]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self._temperature,
            "max_completion_tokens": 32000,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "react_step", "schema": _build_react_schema(self.memory_backend)},
            },
        }
        if self._seed is not None:
            kwargs["seed"] = self._seed

        t0 = time.time()
        try:
            resp = self._create_with_filter_retry(kwargs, object_id)
        except Exception as e:
            self._raise_if_content_filter(e, object_id)
            raise
        latency_ms = (time.time() - t0) * 1000

        metrics = InferenceMetrics(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                f"Azure OpenAI response truncated (finish_reason=length) for object {object_id}. "
                "The output exceeded the model's max_tokens limit."
            )
        raw = _safe_json_loads(choice.message.content or "{}")
        return _parse_react_step(raw, self.memory_backend), metrics

    def plan_call(
        self,
        system_prompt: str,
        *,
        object_id: str | None = None,
    ) -> tuple[dict, InferenceMetrics]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}],
            "temperature": self._temperature,
            "max_completion_tokens": 4000,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "plan", "schema": PLANNER_RESPONSE_SCHEMA},
            },
        }
        if self._seed is not None:
            kwargs["seed"] = self._seed
        try:
            t0 = time.time()
            resp = self._create_with_filter_retry(kwargs, object_id)
            latency_ms = (time.time() - t0) * 1000
        except Exception as exc:
            self._raise_if_content_filter(exc, object_id)
            raise
        metrics = InferenceMetrics(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                f"Azure plan response truncated for object {object_id}."
            )
        raw = _safe_json_loads(choice.message.content or "{}")
        return raw, metrics

    def evaluate_call(
        self,
        system_prompt: str,
        *,
        object_id: str | None = None,
    ) -> tuple[dict, InferenceMetrics]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}],
            "temperature": self._temperature,
            "max_completion_tokens": 4000,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "evaluator_result", "schema": EVALUATOR_RESPONSE_SCHEMA},
            },
        }
        if self._seed is not None:
            kwargs["seed"] = self._seed
        try:
            t0 = time.time()
            resp = self._create_with_filter_retry(kwargs, object_id)
            latency_ms = (time.time() - t0) * 1000
        except Exception as exc:
            self._raise_if_content_filter(exc, object_id)
            raise
        metrics = InferenceMetrics(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                f"Azure evaluator response truncated for object {object_id}."
            )
        raw = _safe_json_loads(choice.message.content or "{}")
        return raw, metrics

    def admin_call(
        self,
        system_prompt: str,
        admin_message: str,
        *,
        object_id: str | None = None,
    ) -> tuple[dict, InferenceMetrics]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"[Admin]: {admin_message}"},
            ],
            "temperature": self._temperature,
            "max_completion_tokens": 4000,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "admin_response", "schema": ADMIN_RESPONSE_SCHEMA},
            },
        }
        if self._seed is not None:
            kwargs["seed"] = self._seed
        try:
            t0 = time.time()
            resp = self._create_with_filter_retry(kwargs, object_id)
            latency_ms = (time.time() - t0) * 1000
        except Exception as exc:
            self._raise_if_content_filter(exc, object_id)
            raise
        metrics = InferenceMetrics(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                f"Azure admin response truncated for object {object_id}."
            )
        raw = _safe_json_loads(choice.message.content or "{}")
        return raw, metrics

    def match_wait_call(
        self,
        system_prompt: str,
        *,
        object_id: str | None = None,
    ) -> tuple[dict, InferenceMetrics]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}],
            "temperature": self._temperature,
            "max_completion_tokens": 512,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "wait_match", "schema": WAIT_MATCHER_RESPONSE_SCHEMA},
            },
        }
        if self._seed is not None:
            kwargs["seed"] = self._seed
        try:
            t0 = time.time()
            resp = self._create_with_filter_retry(kwargs, object_id)
            latency_ms = (time.time() - t0) * 1000
        except Exception as exc:
            self._raise_if_content_filter(exc, object_id)
            raise
        metrics = InferenceMetrics(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                f"Azure wait-matcher response truncated for object {object_id}."
            )
        raw = _safe_json_loads(choice.message.content or "{}")
        return raw, metrics

    def _create_with_filter_retry(self, kwargs: dict, object_id: str | None):
        """Azure's content filter / reasoning invalid_prompt 400s are PROBABILISTIC — the same
        prompt usually passes on retry. Retry up to 3 times with backoff before treating the
        flag as real."""
        import time as _time
        last = None
        for attempt in range(4):
            try:
                LIVENESS["attempts"] += 1
                resp = self._client.chat.completions.create(**kwargs)
                LIVENESS["completions"] += 1
                if getattr(resp, "usage", None):
                    LIVENESS["prompt_tokens"] += resp.usage.prompt_tokens or 0
                    LIVENESS["completion_tokens"] += resp.usage.completion_tokens or 0
                return resp
            except Exception as e:
                body = getattr(e, "body", None) or {}
                err = (body.get("error") or {}) if isinstance(body, dict) else {}
                msg = (err.get("message") or "").lower()
                flagged = err.get("code") in ("content_filter", "invalid_prompt") \
                    or "flagged" in msg or "invalid prompt" in msg
                if not flagged or attempt == 3:
                    raise
                last = e
                logger.warning("Content filter flagged %s (attempt %d/4) — retrying",
                               object_id, attempt + 1)
                _time.sleep(2 ** attempt)
        raise last  # unreachable

    @staticmethod
    def _raise_if_content_filter(exc: Exception, object_id: str | None) -> None:
        body = getattr(exc, "body", None) or {}
        err  = (body.get("error") or {}) if isinstance(body, dict) else {}
        code = err.get("code")
        msg  = (err.get("message") or "").lower()
        if code == "content_filter" or "flagged" in msg or "invalid prompt" in msg:
            raise RuntimeError(
                f"Azure content filter triggered for object {object_id} (prompt flagged). Skipping."
            ) from None


class AnthropicBrain(LLMBrain):
    """Brain backed by the Anthropic API."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        api_key: Optional[str] = None,
        temperature: float = 0.0,
        thinking: str | None = None,
    ) -> None:
        try:
            import anthropic as _anthropic
        except ImportError:
            raise ImportError("anthropic package required. Install with: pip install anthropic")

        self.model = model
        self._temperature = temperature
        self._thinking = thinking
        self._client = _anthropic.Anthropic(
            api_key=api_key or os.environ["ANTHROPIC_API_KEY"],
            timeout=600.0,  # 10 min HTTP timeout — prevents httpx.ReadTimeout on slow responses
        )

    def _thinking_kwargs(self) -> dict:
        if self._thinking is not None:
            return {"thinking": {"type": self._thinking}}
        return {}

    @staticmethod
    def _enforce_strict_schema(schema: dict) -> None:
        """Recursively set additionalProperties: false on all object types."""
        if schema.get("type") == "object":
            schema["additionalProperties"] = False
        for key in ("properties", "$defs"):
            if key in schema:
                for sub in schema[key].values():
                    if isinstance(sub, dict):
                        AnthropicBrain._enforce_strict_schema(sub)
        for key in ("items", "anyOf", "oneOf", "allOf"):
            if key in schema:
                target = schema[key]
                if isinstance(target, dict):
                    AnthropicBrain._enforce_strict_schema(target)
                elif isinstance(target, list):
                    for item in target:
                        if isinstance(item, dict):
                            AnthropicBrain._enforce_strict_schema(item)

    def call(
        self,
        messages: list[dict],
        schema: dict,
        *,
        object_id: str | None = None,
    ) -> tuple[LLMResponse, InferenceMetrics]:
        # Anthropic requires system prompt as a separate parameter
        sys_prompt = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        user_messages = [m for m in messages if m["role"] != "system"]

        strict_schema = json.loads(json.dumps(schema))
        self._enforce_strict_schema(strict_schema)

        t0 = time.time()
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=16000,
            temperature=self._temperature,
            system=sys_prompt,
            messages=user_messages,
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": strict_schema,
                },
            },
            **self._thinking_kwargs(),
        )
        latency_ms = (time.time() - t0) * 1000

        metrics = InferenceMetrics(
            input_tokens=getattr(resp.usage, "input_tokens", 0) if resp.usage else 0,
            output_tokens=getattr(resp.usage, "output_tokens", 0) if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )

        if resp.stop_reason == "max_tokens":
            raise RuntimeError(
                f"Anthropic response truncated (stop_reason=max_tokens) for object {object_id}. "
                "The output exceeded the max_tokens limit."
            )
        content_str = ""
        for block in resp.content:
            if hasattr(block, "text"):
                content_str += block.text

        raw = _safe_json_loads(content_str or "{}")
        return _parse_llm_result(raw), metrics

    def react_call(
        self,
        messages: list[dict],
        *,
        object_id: str | None = None,
    ) -> tuple[ReactStep, InferenceMetrics]:
        sys_prompt = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        user_messages = [m for m in messages if m["role"] != "system"]

        strict_schema = _build_react_schema(self.memory_backend)
        self._enforce_strict_schema(strict_schema)
        # Patch AFTER enforce_strict: give `state_update[.items].value` an explicit
        # wildcard schema (empty schema = any value in JSON Schema) so Anthropic's
        # validator accepts it. Walks both shapes — flat (single object) and
        # nested (array of items) — since enforce_strict may have overwritten the
        # wildcard with a stricter form.
        try:
            su = strict_schema["properties"]["state_update"]
            target_props = (
                su["items"]["properties"]
                if su.get("type") == "array" and "items" in su
                else su.get("properties")
            )
            if target_props and "value" in target_props:
                target_props["value"] = {"description": "New value. Omit for delete."}
        except (KeyError, TypeError):
            pass

        t0 = time.time()
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=16000,
                temperature=self._temperature,
                system=sys_prompt,
                messages=user_messages,
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": strict_schema,
                    },
                },
                **self._thinking_kwargs(),
            )
        except Exception as e:
            # output_config may be unsupported for this model/version — fall back to
            # unstructured output and rely on _safe_json_loads to parse the response.
            if "output_config" in str(e) or "json_schema" in str(e) or "400" in str(e):
                logger.debug(
                    "AnthropicBrain: output_config rejected (%s), falling back to unstructured call.",
                    e,
                )
                resp = self._client.messages.create(
                    model=self.model,
                    max_tokens=16000,
                    temperature=self._temperature,
                    system=sys_prompt,
                    messages=user_messages,
                    **self._thinking_kwargs(),
                )
            else:
                raise

        latency_ms = (time.time() - t0) * 1000

        metrics = InferenceMetrics(
            input_tokens=getattr(resp.usage, "input_tokens", 0) if resp.usage else 0,
            output_tokens=getattr(resp.usage, "output_tokens", 0) if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        if resp.stop_reason == "max_tokens":
            raise RuntimeError(
                f"Anthropic response truncated (stop_reason=max_tokens) for object {object_id}. "
                "The output exceeded the max_tokens limit."
            )
        content_str = "".join(block.text for block in resp.content if hasattr(block, "text"))
        try:
            raw = _safe_json_loads(content_str or "{}")
        except json.JSONDecodeError:
            logger.warning(
                "AnthropicBrain: JSON parse failed for object %s. "
                "Response preview: %r",
                object_id,
                (content_str or "")[:200],
            )
            raw = {}
        return _parse_react_step(raw, self.memory_backend), metrics

    def admin_call(
        self,
        system_prompt: str,
        admin_message: str,
        *,
        object_id: str | None = None,
    ) -> tuple[dict, InferenceMetrics]:
        strict_schema = json.loads(json.dumps(ADMIN_RESPONSE_SCHEMA))
        self._enforce_strict_schema(strict_schema)

        t0 = time.time()
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=4000,
            temperature=self._temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": f"[Admin]: {admin_message}"}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": strict_schema,
                },
            },
            **self._thinking_kwargs(),
        )
        latency_ms = (time.time() - t0) * 1000

        metrics = InferenceMetrics(
            input_tokens=getattr(resp.usage, "input_tokens", 0) if resp.usage else 0,
            output_tokens=getattr(resp.usage, "output_tokens", 0) if resp.usage else 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        if resp.stop_reason == "max_tokens":
            raise RuntimeError(
                f"Anthropic admin response truncated for object {object_id}."
            )
        content_str = "".join(block.text for block in resp.content if hasattr(block, "text"))
        raw = _safe_json_loads(content_str or "{}")
        return raw, metrics


class GeminiBrain(LLMBrain):
    """Brain backed by the Google Gemini API."""

    def __init__(
        self,
        model: str = "gemini-2.5-pro",
        api_key: Optional[str] = None,
        temperature: float = 0.0,
    ) -> None:
        try:
            from google import genai
            from google.genai import types as genai_types
        except ImportError:
            raise ImportError("google-genai package required. Install with: pip install google-genai")

        self.model = model
        self._temperature = temperature
        resolved_key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Google API key required. Set GOOGLE_API_KEY in your environment or .env file, "
                "or pass api_key to GeminiBrain."
            )
        self._client = genai.Client(api_key=resolved_key)
        self._types = genai_types

    def _to_gemini_contents(self, messages: list[dict]) -> tuple[str, list]:
        """Split system prompt and convert messages to Gemini contents format."""
        system_parts = []
        contents = []
        for m in messages:
            if m["role"] == "system":
                system_parts.append(m["content"])
            else:
                role = "model" if m["role"] == "assistant" else m["role"]
                contents.append(
                    self._types.Content(
                        role=role,
                        parts=[self._types.Part(text=m["content"])],
                    )
                )
        return "\n".join(system_parts), contents

    def _generate_json(self, messages: list[dict], schema: dict) -> tuple[str, Any]:
        system_instruction, contents = self._to_gemini_contents(messages)
        t0 = time.time()
        config = self._types.GenerateContentConfig(
            temperature=self._temperature,
            max_output_tokens=8192,
            response_mime_type="application/json",
            response_schema=schema,
        )
        if system_instruction:
            config.system_instruction = system_instruction
        resp = self._client.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )
        latency_ms = (time.time() - t0) * 1000
        metrics = InferenceMetrics(
            input_tokens=getattr(getattr(resp, "usage_metadata", None), "prompt_token_count", 0) or 0,
            output_tokens=getattr(getattr(resp, "usage_metadata", None), "candidates_token_count", 0) or 0,
            latency_ms=latency_ms,
            model=self.model,
        )
        return resp.text or "{}", metrics

    def call(
        self,
        messages: list[dict],
        schema: dict,
        *,
        object_id: str | None = None,
    ) -> tuple[LLMResponse, InferenceMetrics]:
        text, metrics = self._generate_json(messages, schema)
        raw = _safe_json_loads(text)
        return _parse_llm_result(raw), metrics

    def react_call(
        self,
        messages: list[dict],
        *,
        object_id: str | None = None,
    ) -> tuple[ReactStep, InferenceMetrics]:
        text, metrics = self._generate_json(messages, _build_react_schema(self.memory_backend))
        raw = _safe_json_loads(text)
        return _parse_react_step(raw, self.memory_backend), metrics


@dataclass
class _ScriptEntry:
    response: LLMResponse
    metrics: InferenceMetrics = field(
        default_factory=lambda: InferenceMetrics(model="mock")
    )


@dataclass
class CallRecord:
    """Record of a call made to MockBrain."""
    object_id: str | None
    messages: list[dict]


class MockBrain(LLMBrain):
    """Deterministic scripted brain for testing."""

    def __init__(self) -> None:
        self._scripts: dict[str, list[_ScriptEntry]] = {}
        self._default_response: Optional[LLMResponse] = None
        self.call_log: list[CallRecord] = []
        self._react_queue: list[tuple[ReactStep, InferenceMetrics]] = []
        # Plan-call queue: dicts shaped per PLANNER_RESPONSE_SCHEMA.
        # Per-object scripts take precedence over the global queue.
        self._plan_scripts: dict[str, list[dict]] = {}
        self._plan_queue: list[dict] = []
        # Wait-matcher queue: list of {'match': '<trace_id>:<step_index>' | None,
        # 'reasoning': '...'} payloads consumed in FIFO order.
        self._wait_match_queue: list[dict] = []
        # Admin-call queue: dicts shaped per ADMIN_RESPONSE_SCHEMA, consumed FIFO.
        # Per-object scripts take precedence over the global queue.
        self._admin_scripts: dict[str, list[dict]] = {}
        self._admin_queue: list[dict] = []

    def script(
        self,
        object_id: str,
        response: LLMResponse,
        metrics: Optional[InferenceMetrics] = None,
    ) -> None:
        """Add a scripted response for an object. Responses are consumed in order."""
        entry = _ScriptEntry(
            response=response,
            metrics=metrics or InferenceMetrics(model="mock"),
        )
        self._scripts.setdefault(object_id, []).append(entry)

    def set_default(self, response: LLMResponse) -> None:
        """Set a default response for any unscripted calls."""
        self._default_response = response

    def script_react(
        self,
        step: ReactStep,
        metrics: Optional[InferenceMetrics] = None,
    ) -> None:
        """Enqueue a pre-built ReactStep directly (bypasses LLMResponse conversion).

        Useful for testing state_update deltas and other ReAct-specific fields
        without polluting LLMResponse.
        """
        self._react_queue.append((step, metrics or InferenceMetrics(model="mock")))

    def call(
        self,
        messages: list[dict],
        schema: dict,
        *,
        object_id: str | None = None,
    ) -> tuple[LLMResponse, InferenceMetrics]:
        self.call_log.append(CallRecord(object_id=object_id, messages=messages))

        if object_id is not None:
            entries = self._scripts.get(object_id, [])
            if entries:
                entry = entries.pop(0)
                return entry.response, entry.metrics

        if self._default_response is not None:
            return self._default_response, InferenceMetrics(model="mock")

        # Fallback: echo the last user message with no state change
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        return (
            LLMResponse(
                updated_state="",
                reply=f"Echo: {last_user}",
                outgoing_messages=[],
                reasoning="No script configured",
            ),
            InferenceMetrics(model="mock"),
        )

    def script_plan(self, plan_dict: dict, object_id: Optional[str] = None) -> None:
        """Enqueue a scripted plan-call response (raw dict shaped per
        PLANNER_RESPONSE_SCHEMA). When object_id is provided the script is
        per-object FIFO; otherwise the dict goes onto the global queue."""
        if object_id:
            self._plan_scripts.setdefault(object_id, []).append(plan_dict)
        else:
            self._plan_queue.append(plan_dict)

    def script_wait_match(self, match: Optional[str], reasoning: str = "") -> None:
        """Enqueue a scripted wait-matcher response. `match` is either
        '<trace_id>:<step_index>' or None."""
        self._wait_match_queue.append({"match": match, "reasoning": reasoning})

    def script_admin(
        self,
        reply: str,
        updated_definition: Optional[dict] = None,
        object_id: Optional[str] = None,
    ) -> None:
        """Enqueue a scripted admin-call response shaped per ADMIN_RESPONSE_SCHEMA.

        `updated_definition` is included only when non-None — None signals an
        ambiguous/clarification turn where no patch is applied.
        """
        finish: dict[str, Any] = {"reply": reply}
        if updated_definition is not None:
            finish["updated_definition"] = updated_definition
        payload = {"thought": "mock admin", "finish": finish}
        if object_id:
            self._admin_scripts.setdefault(object_id, []).append(payload)
        else:
            self._admin_queue.append(payload)

    def plan_call(
        self,
        system_prompt: str,
        *,
        object_id: str | None = None,
    ) -> tuple[dict, InferenceMetrics]:
        if object_id is not None:
            entries = self._plan_scripts.get(object_id, [])
            if entries:
                return entries.pop(0), InferenceMetrics(model="mock")
        if self._plan_queue:
            return self._plan_queue.pop(0), InferenceMetrics(model="mock")
        # No script — let the runtime treat the planner as a no-op.
        raise NotImplementedError("MockBrain.plan_call: no scripted plan available")

    def match_wait_call(
        self,
        system_prompt: str,
        *,
        object_id: str | None = None,
    ) -> tuple[dict, InferenceMetrics]:
        if self._wait_match_queue:
            return self._wait_match_queue.pop(0), InferenceMetrics(model="mock")
        # No script: behave like "no match" — safe default that exercises
        # the fall-through path without forcing every test to script it.
        return {"match": None, "reasoning": "mock: no script"}, InferenceMetrics(model="mock")

    def admin_call(
        self,
        system_prompt: str,
        admin_message: str,
        *,
        object_id: str | None = None,
    ) -> tuple[dict, InferenceMetrics]:
        if object_id is not None:
            entries = self._admin_scripts.get(object_id, [])
            if entries:
                return entries.pop(0), InferenceMetrics(model="mock")
        if self._admin_queue:
            return self._admin_queue.pop(0), InferenceMetrics(model="mock")
        raise NotImplementedError(
            "MockBrain.admin_call: no scripted admin response available "
            f"(object_id={object_id})"
        )

    def react_call(
        self,
        messages: list[dict],
        *,
        object_id: str | None = None,
    ) -> tuple[ReactStep, InferenceMetrics]:
        # Return pre-converted steps before fetching a new scripted response.
        if self._react_queue:
            return self._react_queue.pop(0)

        # Fetch the next scripted LLMResponse and convert to ReactStep(s).
        response, metrics = self.call(messages, {}, object_id=object_id)

        if response.tool_calls:
            # One ReactStep per tool call — no finish yet (comes from next script).
            for tc in response.tool_calls:
                step = ReactStep(
                    thought=response.reasoning or "Calling tool.",
                    action="tool_call",
                    tool_call=tc,
                )
                self._react_queue.append((step, metrics))
        else:
            finish = ReactFinish(
                reply=response.reply,
                updated_state=response.updated_state,
                outgoing_messages=response.outgoing_messages,
            )
            step = ReactStep(
                thought=response.reasoning or "Done.",
                action="finish",
                finish=finish,
            )
            self._react_queue.append((step, metrics))

        return self._react_queue.pop(0)


def _sanitize_json_control_chars(text: str) -> str:
    """Escape literal control characters (newlines, tabs, carriage returns) inside JSON
    string values.  The LLM sometimes emits unescaped newlines in long 'thought' or
    'reply' fields, which are valid in prose but illegal in JSON strings.

    Uses a simple character-level state machine that tracks whether we are inside a
    JSON string so we can escape only the characters that need it.
    """
    out: list[str] = []
    in_string = False
    skip_next = False
    for ch in text:
        if skip_next:
            skip_next = False
            out.append(ch)
            continue
        if ch == "\\" and in_string:
            skip_next = True   # next char is an escape sequence — pass through as-is
            out.append(ch)
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string:
            if ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def _safe_json_loads(text: str) -> dict:
    """Parse JSON from LLM output, tolerating markdown fences, preamble text,
    and literal control characters inside string values."""
    text = text.strip()
    if not text:
        return {}
    # Strip optional markdown code fences (```json ... ``` or ``` ... ```)
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
        text = text.strip()
    if not text:
        return {}

    def _try_parse(s: str) -> dict:
        """Try json.loads, then Extra-data fallback, then brace-search fallback."""
        try:
            return json.loads(s)
        except json.JSONDecodeError as e:
            if "Extra data" in str(e):
                decoder = json.JSONDecoder()
                result, _ = decoder.raw_decode(s)
                return result
            # Fallback: find first '{' and try to parse from there
            # (handles preamble text like "Here is the response:\n{...}")
            brace = s.find("{")
            if brace > 0:
                try:
                    decoder = json.JSONDecoder()
                    result, _ = decoder.raw_decode(s, brace)
                    return result
                except json.JSONDecodeError:
                    pass
            raise

    try:
        return _try_parse(text)
    except json.JSONDecodeError:
        # Last resort: escape literal control characters inside strings and retry
        sanitized = _sanitize_json_control_chars(text)
        return _try_parse(sanitized)


def _ensure_str(value: Any) -> str:
    """Coerce a value to string — handles cases where the LLM returns a dict instead of a string."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value)


def _parse_state(raw_state: Any) -> str:
    """Normalize updated_state to a plain string."""
    if isinstance(raw_state, str):
        return raw_state.strip()
    if isinstance(raw_state, dict):
        # Fallback: model returned an object despite string schema — serialize it.
        return json.dumps(raw_state)
    return ""


def _parse_state_delta(raw: dict, backend: Optional[MemoryBackend] = None) -> Optional[Any]:
    """Parse an optional state_update dict into a delta object, or None if absent/invalid.

    The backend supplies the concrete delta type — flat returns StateDelta;
    nested returns NestedDelta.
    """
    b = backend or _DEFAULT_MEMORY_BACKEND
    return b.parse_delta(raw)


def _parse_state_deltas(raw: Any, backend: Optional[MemoryBackend] = None) -> list:
    """Parse a state_update payload into a list of delta objects.

    Accepts either a single dict (flat backend) or a list of dicts (nested
    backend). Returns the flattened list with invalid entries dropped.
    """
    b = backend or _DEFAULT_MEMORY_BACKEND
    if raw is None:
        return []
    if isinstance(raw, list):
        parsed = [b.parse_delta(d) for d in raw if isinstance(d, dict)]
        return [d for d in parsed if d is not None]
    if isinstance(raw, dict):
        d = b.parse_delta(raw)
        return [d] if d is not None else []
    return []


def _build_react_schema(backend: Optional[MemoryBackend]) -> dict:
    """Return a per-request copy of LLM_REACT_SCHEMA with the backend's
    `state_update` fragment swapped in."""
    schema = json.loads(json.dumps(LLM_REACT_SCHEMA))
    if backend is not None:
        schema["properties"]["state_update"] = backend.state_update_schema()
    return schema


def _parse_plan_update(raw: dict) -> Optional[PlanUpdate]:
    """Parse an optional plan_update dict. Accepts any of the three shapes.
    Returns None only if the dict is entirely empty / unrelated."""
    if not isinstance(raw, dict) or not raw:
        return None
    steps = raw.get("steps")
    step_updates = raw.get("step_updates")
    if isinstance(step_updates, list):
        # non-terminal statuses are not actionable — drop them here so the runtime never has
        # to warn-and-ignore ("status='planned' not allowed"); the step simply keeps its state
        for su in step_updates:
            if isinstance(su, dict) and su.get("status") not in ("done", "failed", "skipped"):
                su.pop("status", None)
    add_steps = raw.get("add_steps")
    return PlanUpdate(
        goal=raw.get("goal"),
        steps=steps if isinstance(steps, list) else None,
        step_updates=step_updates if isinstance(step_updates, list) else None,
        add_steps=add_steps if isinstance(add_steps, list) else None,
        status=raw.get("status"),
    )


def _parse_tool_call_dict(tc_data: dict) -> ToolCall:
    """Convert a raw tool_call dict into a ToolCall dataclass."""
    psi = tc_data.get("plan_step_index")
    return ToolCall(
        id=tc_data.get("id", ""),
        tool=tc_data.get("tool", ""),
        arguments=tc_data.get("arguments", {}),
        plan_step_index=psi if isinstance(psi, int) else None,
    )


def _parse_react_step(raw: dict, backend: Optional[MemoryBackend] = None) -> ReactStep:
    """Parse a raw LLM dict into a ReactStep.

    Accepts both legacy singular `tool_call` and the new `tool_calls` list.
    A `finish` action may carry `tool_calls` to dispatch async alongside the
    commitment — those are read here and the runtime dispatches them on the
    per-object tool pool without blocking the turn.

    The backend determines how `state_update` is parsed: flat → single
    StateDelta; nested → list of NestedDelta. ReactStep stores the parsed
    list on `state_updates` (and exposes the first entry as `state_update`
    for backward compatibility).
    """
    thought = raw.get("thought", "")
    action = raw.get("action", "finish")

    # state_update is either a single dict (flat backend) or a list (nested
    # backend). plan_update is unchanged.
    state_updates = _parse_state_deltas(raw.get("state_update"), backend)
    state_update = state_updates[0] if state_updates else None
    plan_update = _parse_plan_update(raw.get("plan_update") or {})

    # Collect tool_calls from either the legacy singular form or the new list.
    # Both may be present in unusual cases — the list wins, singular is appended
    # only if the list is empty.
    tool_calls_raw = raw.get("tool_calls") or []
    tool_calls: list[ToolCall] = [
        _parse_tool_call_dict(tc) for tc in tool_calls_raw if isinstance(tc, dict)
    ]
    if not tool_calls:
        legacy_tc = raw.get("tool_call")
        if isinstance(legacy_tc, dict) and legacy_tc.get("tool"):
            tool_calls = [_parse_tool_call_dict(legacy_tc)]

    if action == "tool_call":
        # Legacy ReAct path: action is tool_call, no finish payload.
        first = tool_calls[0] if tool_calls else None
        return ReactStep(
            thought=thought, action="tool_call",
            state_update=state_update, state_updates=state_updates,
            plan_update=plan_update,
            tool_call=first, tool_calls=tool_calls,
        )

    # action == "finish" — may also carry tool_calls dispatched async
    f_data = raw.get("finish") or {}
    updated_state = _parse_state(f_data.get("updated_state"))

    raw_msgs = f_data.get("outgoing_messages", []) or []
    outgoing = [
        OutgoingMessage(
            recipient=m["recipient"],
            content=m["content"],
            expects_reply=bool(m.get("expects_reply", False)),
            status=m.get("status") if m.get("status") in ("ok", "failed") else None,
            error=m.get("error") if isinstance(m.get("error"), str) else None,
        )
        for m in raw_msgs
        if isinstance(m, dict)
    ]
    updated_def = f_data.get("updated_definition") or None
    if updated_def == {}:
        updated_def = None
    raw_gap = f_data.get("knowledge_gap") or None
    knowledge_gap = None
    if isinstance(raw_gap, dict) and raw_gap.get("question"):
        knowledge_gap = KnowledgeGap(
            question=raw_gap["question"],
            context=raw_gap.get("context", ""),
        )
    finish_status = f_data.get("status") if f_data.get("status") in ("ok", "failed") else None
    finish_error = f_data.get("error") if isinstance(f_data.get("error"), str) else None
    finish = ReactFinish(
        reply=f_data.get("reply", ""),
        updated_state=updated_state,
        outgoing_messages=outgoing,
        updated_definition=updated_def,
        knowledge_gap=knowledge_gap,
        status=finish_status,
        error=finish_error,
    )
    return ReactStep(
        thought=thought, action="finish",
        state_update=state_update, state_updates=state_updates,
        plan_update=plan_update,
        finish=finish, tool_calls=tool_calls,
    )


def _parse_llm_result(result: Any) -> LLMResponse:
    """Parse the raw LLM result dict into LLMResponse."""
    if isinstance(result, dict):
        data = result
    else:
        data = {
            "updated_state": getattr(result, "state", "") or "",
            "reply": getattr(result, "response", "") or "",
            "outgoing_messages": getattr(result, "messages", []) or [],
            "reasoning": "",
        }

    outgoing = []
    for m in data.get("outgoing_messages", []):
        if isinstance(m, dict):
            outgoing.append(OutgoingMessage(recipient=m["recipient"], content=m["content"]))
        elif isinstance(m, OutgoingMessage):
            outgoing.append(m)

    tool_calls = []
    for tc in data.get("tool_calls", []):
        if isinstance(tc, dict):
            tool_calls.append(ToolCall(id=tc["id"], tool=tc["tool"], arguments=tc["arguments"]))
        elif isinstance(tc, ToolCall):
            tool_calls.append(tc)

    return LLMResponse(
        updated_state=_parse_state(data.get("updated_state")),
        reply=_ensure_str(data.get("reply", "")),
        outgoing_messages=outgoing,
        reasoning=data.get("reasoning", ""),
        tool_calls=tool_calls,
    )
