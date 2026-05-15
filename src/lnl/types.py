"""Core data types for the LNL runtime."""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class MessageType(Enum):
    """Type of message exchanged between LLM-objects."""
    DOMAIN = "domain"
    ADMIN = "admin"
    EVENT = "event"
    REPLY = "reply"
    HEARTBEAT = "heartbeat"
    PLAN = "plan"   # synthetic — never delivered; surfaces planner output in bus log


@dataclass
class PeerDeclaration:
    """Declares a peer relationship for an LLM-object."""
    object_id: str
    relationship: str


@dataclass
class ObjectDefinition:
    """Complete definition of an LLM-object parsed from markdown."""
    object_id: str
    role: str
    behavior: str = ""
    peers: list[PeerDeclaration] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    subscriptions: list[str] = field(default_factory=list)
    event_sources: list[str] = field(default_factory=list)
    initial_state: str = ""  # optional ## State section from markdown


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass
class Message:
    """A message passed between LLM-objects or from external senders."""
    sender: str
    recipient: str
    type: MessageType
    content: str
    topic: Optional[str] = None
    depth_remaining: int = 10  # hops remaining before chain is cut
    timestamp: datetime.datetime = field(default_factory=_utcnow)
    id: str = ""                         # runtime-assigned deterministic ID
    in_reply_to: Optional[str] = None    # ID of the message being replied to
    reference: Optional[str] = None      # legacy correlation tag (unused by runtime now)
    expects_reply: bool = False          # True = Ask (sender wants a reply); False = Tell (propagated from OutgoingMessage)
    plan_step_index: Optional[int] = None  # runtime-stamped: index of plan step this message dispatches (for correlation)
    # ── Transaction tracing (runtime-only; not exposed to the LLM) ──────────────
    trace_id: Optional[str] = None       # root msg.id of the cascade; propagated through every hop
    parent_id: Optional[str] = None      # msg.id whose processing caused this message to be sent


@dataclass
class OutgoingMessage:
    """An outgoing message produced by the LLM.

    The LLM sets only {recipient, content, expects_reply}. The runtime
    populates the correlation fields during auto-matching.
    """
    recipient: str
    content: str
    expects_reply: bool = False        # True = Ask (sender wants a reply); False = Tell (fire-and-forget)
    # Runtime-stamped fields (LLM never sets these):
    plan_step_index: Optional[int] = None  # index of the plan step this dispatches (if correlated)
    in_reply_to: Optional[str] = None      # original message id when this is a cross-turn reply to a pending Ask
    is_reply: bool = False                 # true when this fulfills a pending inbound Ask (routed as MessageType.REPLY)


@dataclass
class ToolCall:
    """A tool call requested by the LLM."""
    id: str
    tool: str
    arguments: dict


@dataclass
class ToolResult:
    """Result of executing a tool call."""
    id: str
    output: str
    error: str = ""


@dataclass
class ExternalAction:
    """A structured action directed at an external system (Slack, Email, Jira, etc.)."""
    system: str    # e.g. "slack", "email", "jira"
    action: str    # e.g. "send_message", "send", "create_issue"
    content: str   # NL content: message body, email text, ticket description, etc.
    params: dict = field(default_factory=dict)  # structured params: channel, to, subject, project, etc.


@dataclass
class LLMResponse:
    """Structured response returned by an LLM brain."""
    updated_state: str
    reply: str
    outgoing_messages: list[OutgoingMessage] = field(default_factory=list)
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class InferenceMetrics:
    """Metrics from a single LLM inference call."""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    model: str = ""


@dataclass
class ProcessingResult:
    """Result of processing a message by an LLM-object."""
    object_id: str
    reply: str
    outgoing_messages: list[OutgoingMessage] = field(default_factory=list)
    state_before: Any = None  # dict if JSON-parseable state, else str; None = {}
    state_after: Any = None   # dict if JSON-parseable state, else str; None = {}
    metrics: Optional[InferenceMetrics] = None
    planner_metrics: Optional[InferenceMetrics] = None
    executor_metrics: Optional[InferenceMetrics] = None
    evaluator_metrics: Optional[InferenceMetrics] = None
    in_reply_to: Optional[str] = None  # sender of the message that was processed
    source_message_type: Optional[MessageType] = None  # type of the message that was processed
    depth_remaining: int = 10  # propagated from the processed message
    sequence: int = 0          # assigned by Runtime for ordering concurrent results
    source_message_id: str = ""  # ID of the message that was processed
    source_plan_step_index: Optional[int] = None  # plan_step_index from the processed message (propagated onto replies)
    source_trace_id: Optional[str] = None  # trace_id from the processed message (propagated onto cascaded messages)
    # ── Per-message processing wall-clock (for tracing) ───────────────────────
    processing_started_at: Optional[datetime.datetime] = None
    processing_completed_at: Optional[datetime.datetime] = None


@dataclass
class StateDelta:
    """A single state change operation emitted by the LLM at any ReAct step."""
    op: str    # "set" | "delete" | "append"
    key: str
    value: Any = None  # required for set/append; ignored for delete


@dataclass
class KnowledgeGap:
    """A knowledge gap signalled by the LLM via finish.knowledge_gap."""
    question: str
    context: str = ""


# --- Plan types ---

STEP_TERMINAL_STATUSES = ("done", "failed", "skipped")
PLAN_TERMINAL_STATUSES = ("complete", "cancelled")


@dataclass
class PlanStep:
    """One step in an active plan. The LLM never references steps by id —
    it sees them by position (0-based index) in the rendered plan."""
    kind: str                            # "ask" | "tell"
    description: str
    target: Optional[str] = None
    status: str = "planned"              # "planned" | "dispatched" | "done" | "failed" | "skipped"
    result_summary: Optional[str] = None


@dataclass
class Plan:
    """An active or terminated plan. One active plan per object at a time."""
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    status: str = "active"               # "active" | "complete" | "cancelled"


@dataclass
class PlanUpdate:
    """A plan update emitted by the LLM. Exactly one of three shapes:

    - Create/replace: `goal` + `steps` → new plan (or replace active)
    - Incremental: `step_updates` and/or `add_steps` → modify active plan
    - Close: `status="complete"` or `status="cancelled"` → terminate active plan
    """
    goal: Optional[str] = None
    steps: Optional[list[dict]] = None         # for create/replace: [{kind, description, target}, ...]
    step_updates: Optional[list[dict]] = None  # incremental: [{index, status?, result_summary?}, ...]
    add_steps: Optional[list[dict]] = None     # incremental: steps to append
    status: Optional[str] = None               # "complete" | "cancelled" to close the plan


@dataclass
class ReactFinish:
    """The finish action in a ReAct step — commits reply and outgoing messages."""
    reply: str
    updated_state: str = ""  # legacy compat for MockBrain/tests; not in LLM schema
    outgoing_messages: list[OutgoingMessage] = field(default_factory=list)
    updated_definition: Optional[dict] = None  # set when an ADMIN message triggers a definition change
    knowledge_gap: Optional["KnowledgeGap"] = None


@dataclass
class ReactStep:
    """One step in a ReAct loop: an explicit thought and a single action."""
    thought: str
    action: str  # "tool_call" | "finish"
    state_update: Optional[StateDelta] = None  # optional at any step; accumulated by runtime
    plan_update: Optional[PlanUpdate] = None   # optional at any step; accumulated by runtime
    tool_call: Optional[ToolCall] = None
    finish: Optional[ReactFinish] = None


@dataclass
class MessageLog:
    """Log entry for a message delivered through the bus."""
    message: Message
    delivered: bool = True
    error: Optional[str] = None
    metrics: Optional[InferenceMetrics] = None
    # ── Tracing fields (runtime-only) ──────────────────────────────────────────
    received_at: Optional[datetime.datetime] = None              # bus delivery wall-clock
    processing_started_at: Optional[datetime.datetime] = None    # first ReAct step begins
    processing_completed_at: Optional[datetime.datetime] = None  # process_message returns
    hop_depth: int = 0                                            # max_chain_depth - depth_remaining
