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
    REPLY = "reply"  # used for both peer-to-peer replies and async tool results (sender distinguishes)
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
    # ── Outcome signalling on REPLY messages ──────────────────────────────────
    # status=None on non-reply messages. On REPLY, status="ok" (default for
    # successful replies) or "failed" (when the responding object reports
    # failure, a tool errored, or the runtime synthesized a failure on the
    # asker's behalf). error carries structured failure detail when status=failed.
    status: Optional[str] = None         # None | "ok" | "failed"
    error: Optional[str] = None          # structured failure detail when status="failed"
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
    # Outcome signalling (propagated onto the chained Message when set).
    # Used when the LLM emits an outgoing reply that should be marked as a
    # failure for the asker's plan step.
    status: Optional[str] = None       # None | "ok" | "failed"
    error: Optional[str] = None
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
    # Optional: when the LLM tags a tool call with a plan step index, the
    # runtime auto-captures ToolResult onto plan.steps[plan_step_index].result.
    plan_step_index: Optional[int] = None


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
    # Outcome of this turn. status="failed" causes the runtime to synthesize
    # a failure REPLY back to the asker (if any) instead of an "ok" reply.
    status: Optional[str] = None   # None | "ok" | "failed"
    error: Optional[str] = None    # structured failure detail when status="failed"
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
    """One step in an active plan.

    Each step has a stable string id (e.g. "s1", "s2") that downstream
    steps can reference in their descriptions, e.g. "post the URL from
    s2.result to the Slack channel". The id stays stable across plan
    updates so the LLM can rely on it. Step descriptions and the
    evaluator's grading both reference steps by id.
    """
    kind: str                            # "ask" | "tell" | "tool" | "reason" | "wait"
    description: str
    id: str = ""                         # stable id, e.g. "s1", "s2". Auto-filled by renderer if empty.
    target: Optional[str] = None         # peer_id for ask/tell; tool name for tool; None for reason/wait
    depends_on: list[str] = field(default_factory=list)  # ids of steps whose results this step references
    status: str = "planned"              # "planned" | "dispatched" | "done" | "failed" | "skipped"
    result_summary: Optional[str] = None
    # Runtime-captured result, preserving the source's native shape:
    # - NL string for peer replies (ask)
    # - structured dict/list/scalar for tool returns
    # - short note for reason steps (LLM-emitted at closure)
    # - NL content of the absorbing event (wait)
    result: Optional[Any] = None
    result_kind: Optional[str] = None    # "nl" | "tool" | "reason" | "event"
    completed_at: Optional[datetime.datetime] = None
    # ── Wait-step fields (kind == "wait") ─────────────────────────────────────
    # The planner emits these so the runtime can correlate a later-arriving
    # external event back to the pending plan instead of starting a new plan.
    wait_predicate: Optional[str] = None        # NL description of what to wait for
    wait_source: Optional[str] = None           # expected event source/sender (soft hint)
    wait_timeout_seconds: Optional[float] = None  # overrides the plan-level stale threshold
    matched_event_id: Optional[str] = None      # runtime-stamped when a wait closes


@dataclass
class Plan:
    """An active or terminated plan, scoped to one trace_id. Multiple plans
    may coexist on a single object — one per concurrent cascade."""
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    status: str = "active"               # "active" | "waiting" | "complete" | "cancelled" | "abandoned" | "failed"
    trace_id: Optional[str] = None       # cascade this plan belongs to
    created_at: datetime.datetime = field(default_factory=_utcnow)
    last_progress_at: datetime.datetime = field(default_factory=_utcnow)
    # Secondary trace_ids absorbed via wait-step matching. plan_for() checks
    # both the primary trace_id and this set so post-correlation lookups
    # resolve to the absorbing plan.
    additional_trace_ids: set[str] = field(default_factory=set)


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
    # The LLM signals overall turn outcome here. status="failed" propagates a
    # failure REPLY to any asker awaiting this object — the asker's plan step
    # flips to status="failed" instead of "done".
    status: Optional[str] = None   # None | "ok" | "failed"
    error: Optional[str] = None    # structured failure detail when status="failed"


@dataclass
class ReactStep:
    """One step in a ReAct loop: an explicit thought and a single action.

    After the async-tools rewrite, a single response can carry both a
    `finish` AND a list of `tool_calls` dispatched async. For backward
    compatibility, `tool_call` (singular) is kept as a legacy alias —
    if set and `tool_calls` is empty, it's normalized into the list.
    """
    thought: str
    action: str  # "tool_call" | "finish"
    state_update: Optional[StateDelta] = None  # applied ONLY on action="finish" (commitment, not reasoning)
    plan_update: Optional[PlanUpdate] = None   # applied ONLY on action="finish"
    tool_call: Optional[ToolCall] = None       # legacy: singular tool call
    tool_calls: list[ToolCall] = field(default_factory=list)  # preferred: batched tool calls dispatched async
    finish: Optional[ReactFinish] = None

    def __post_init__(self) -> None:
        # Normalize legacy singular form into the list. Caller may pass
        # either shape; downstream code reads `self.tool_calls` only.
        if self.tool_call is not None and not self.tool_calls:
            self.tool_calls = [self.tool_call]


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
