"""Core data types for the LNL runtime."""
from __future__ import annotations

import datetime
import secrets
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


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
    initial_state: str = ""  # optional ## State section from markdown (private state)
    shared_state: str = ""   # optional ## Shared State section — the object's shared partition


# ---------------------------------------------------------------------------
# Admin-modification spec — the single source of truth for which fields of
# ObjectDefinition can be patched by an ADMIN message, what JSON shape each
# patch entry takes, and how the runtime coerces the raw dict back into
# ObjectDefinition's field types.
#
# Everything downstream — the LLM-facing JSON schema, the admin prompt's
# "Patchable Fields" section, and the runtime apply step — is derived from
# this list. Adding a new patchable field means one new PatchableField entry
# here and nothing else.
# ---------------------------------------------------------------------------

@dataclass
class PatchableField:
    """One patchable field of ObjectDefinition.

    Every downstream artifact that mentions this field — the admin LLM's
    JSON schema, the admin prompt's "Current Definition" rendering, the
    "Patchable Fields" spec, the response-format example, and the runtime
    apply step — is derived from this struct. Adding a new patchable field
    is one PatchableField entry and nothing else.
    """
    name: str
    title: str                    # display title in the "Current Definition" section
    json_schema: dict             # JSON schema fragment for this field's value
    description: str              # human-readable purpose, used in the admin prompt
    list_semantics_note: str = ""  # extra prompt note for list-typed fields
    # Coerce the LLM-supplied raw value into the type expected on
    # ObjectDefinition (e.g. list[PeerDeclaration]).
    coercer: Callable[[Any], Any] = lambda v: v
    # Render this field's CURRENT value (read off the ObjectDefinition) as
    # the body of its "## {title}" block in the admin prompt.
    renderer: Callable[[Any], str] = lambda v: str(v) if v else "(none)"
    # Short example value used in the response-format JSON template, e.g.
    # '"..."' for a string, '[ ... ]' for a list.
    example_literal: str = '"..."'


def _coerce_peers(value: Any) -> list[PeerDeclaration]:
    if not isinstance(value, list):
        return []
    return [
        PeerDeclaration(object_id=p["object_id"], relationship=p["relationship"])
        for p in value
        if isinstance(p, dict) and "object_id" in p and "relationship" in p
    ]


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [s for s in value if isinstance(s, str)]


def _render_peers(value: Any) -> str:
    if not value:
        return "(none)"
    return "\n".join(f"- {p.object_id}: {p.relationship}" for p in value)


def _render_str_list(value: Any) -> str:
    if not value:
        return "(none)"
    return "\n".join(f"- {s}" for s in value)


def _render_text(value: Any) -> str:
    return str(value) if value else "(none)"


PATCHABLE_FIELDS: list[PatchableField] = [
    PatchableField(
        name="role",
        title="Role",
        json_schema={"type": "string"},
        description="your one-line purpose statement.",
        renderer=_render_text,
    ),
    PatchableField(
        name="behavior",
        title="Behavior",
        json_schema={"type": "string"},
        description=(
            "the longer description of how you operate, what events you "
            "react to, and what you produce."
        ),
        renderer=_render_text,
    ),
    PatchableField(
        name="peers",
        title="Peers",
        json_schema={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "object_id": {"type": "string"},
                    "relationship": {"type": "string"},
                },
                "required": ["object_id", "relationship"],
                "additionalProperties": False,
            },
        },
        description=(
            "who you may message and the contract for each relationship."
        ),
        list_semantics_note=(
            "To add a peer, include the full list with the new entry. "
            "To remove a peer, include the full list without it. "
            "To change a relationship, include the full list with the edited "
            "entry. Peers you omit from the list are removed."
        ),
        coercer=_coerce_peers,
        renderer=_render_peers,
        example_literal='[ {"object_id": "...", "relationship": "..."} ]',
    ),
    PatchableField(
        name="skills",
        title="Skills",
        json_schema={"type": "array", "items": {"type": "string"}},
        description="your tool/skill identifiers.",
        list_semantics_note=(
            "Same list-replace semantics as peers — include the full intended list."
        ),
        coercer=_coerce_str_list,
        renderer=_render_str_list,
        example_literal='[ "..." ]',
    ),
]


PATCHABLE_FIELD_NAMES: frozenset[str] = frozenset(f.name for f in PATCHABLE_FIELDS)


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
    # Sender-side provenance: the task and plan generation in the sender
    # LLM-object that produced this message. Lets receivers (and traces /
    # debug tooling) correlate an inbound message back to the sender's
    # task and plan generation. None when the sender had no active plan
    # (admin path, external bootstrap, broadcasts).
    task_id: Optional[str] = None
    plan_id: Optional[str] = None


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
    # Sender-side provenance: identifies the task and plan generation that
    # produced this outgoing within the sender LLM-object. Together with
    # plan_step_index and in_reply_to, the full causal chain is preserved.
    task_id: Optional[str] = None          # sender's plan.task_id (stable across replans)
    plan_id: Optional[str] = None          # sender's plan.id (re-minted on replan)


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
    # Count of full executor cycles for this message: 1 means the executor
    # ran once and the evaluator passed (or was off). >1 means the evaluator
    # FAIL'd and the executor was re-invoked. Equivalent to (eval_cycle + 1)
    # at loop exit. executor_retries == executor_cycles - 1.
    executor_cycles: int = 1
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
    """A single state change operation emitted by the LLM at any ReAct step.

    Base ops: set | delete | append.

    Guarded ops enforce an invariant deterministically (see
    docs/SHARED_STATE_SPEC.md): incr/decr (bounded counters) and
    reserve/confirm/release (two-phase holds). A guarded op that would violate
    its bound is a no-op — the invariant holds regardless of what the LLM
    computed, which is what lets an ordinary object own a shared cap/quota
    safely.
    """
    op: str    # set | delete | append | incr | decr | reserve | confirm | release
    key: str
    value: Any = None  # set/append value; reserve: the amount to hold; ignored for delete
    # ── Guarded-op params (optional; only read by the guarded ops) ─────────────
    by: Any = None        # incr/decr: amount to add (decr negates it)
    min: Any = None       # incr/decr: reject if result < min (decr defaults min=0)
    max: Any = None       # incr: reject if result > max
    cap: Any = None       # reserve: reject if committed + held + amount > cap
    hold_id: Any = None   # reserve/confirm/release: hold identifier


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
    kind: str                            # "ask" | "tell" | "tool" | "reason" | "wait" | "replan"
    description: str
    id: str = ""                         # stable id, e.g. "s1", "s2". Auto-filled by renderer if empty.
    target: Optional[str] = None         # peer_id for ask/tell; tool name for tool; None for reason/wait/replan
    depends_on: list[str] = field(default_factory=list)  # ids of steps whose results this step references
    status: str = "planned"              # "planned" | "dispatched" | "done" | "failed" | "skipped"
    result_summary: Optional[str] = None
    # Runtime-captured result, preserving the source's native shape:
    # - NL string for peer replies (ask)
    # - structured dict/list/scalar for tool returns
    # - short note for reason steps (LLM-emitted at closure)
    # - NL content of the absorbing event (wait)
    # - count of appended continuation steps for replan
    result: Optional[Any] = None
    result_kind: Optional[str] = None    # "nl" | "tool" | "reason" | "event" | "replan"
    completed_at: Optional[datetime.datetime] = None
    # ── Wait-step fields (kind == "wait") ─────────────────────────────────────
    # The planner emits these so the runtime can correlate a later-arriving
    # external event back to the pending plan instead of starting a new plan.
    wait_predicate: Optional[str] = None        # NL description of what to wait for
    wait_source: Optional[str] = None           # expected event source/sender (soft hint)
    wait_timeout_seconds: Optional[float] = None  # overrides the plan-level stale threshold
    matched_event_id: Optional[str] = None      # runtime-stamped when a wait closes
    # ── Replan-step fields (kind == "replan") ─────────────────────────────────
    # The planner emits a replan step to defer a decision until prior deps land.
    # When deps complete, the runtime re-invokes the planner with completed step
    # results so it can emit continuation steps (appended via add_steps).
    replan_question: Optional[str] = None       # NL description of the deferred decision
    # ── Reactive retry/replan tracking (SystemConfig.enable_step_retry_replan) ─
    # retry_count: incremented each time the post-execution evaluator invalidates
    # this step (FAIL verdict citing it). reactive_replan_count: incremented when
    # the runtime synthesizes a kind=replan step targeting this one because its
    # retry_count crossed step_max_retries. Both counters are inert when the
    # feature flag is off.
    retry_count: int = 0
    reactive_replan_count: int = 0
    # Last evaluator failure summary for this step — overwritten on each FAIL
    # cycle; used to give the planner context when a reactive replan fires.
    last_failure_reason: Optional[str] = None
    # Tag set by _synthesize_reactive_replans on the synthetic kind=replan step
    # so the terminal-failure branch in _dispatch_pending_replans can recognize
    # it and propagate failure back to the originating step's id.
    reactive_replan_for: Optional[str] = None


@dataclass
class Plan:
    """An active or terminated plan, scoped to one trace_id. Multiple plans
    may coexist on a single object — one per concurrent cascade.

    Identifiers:
      - task_id: stable for the entire object-local processing of an
        incoming request. Preserved across replan-in-place (the plan is
        regenerated, but the task is the same). Used to tag HistoryEntry
        rows and to stamp outgoing-message provenance.
      - id: identifies a specific plan *generation*. Re-minted on every
        replan-in-place so each generation is distinguishable.
    """
    goal: str
    # Stable across plan regeneration.
    task_id: str = field(default_factory=lambda: secrets.token_hex(8))
    # Re-minted on every replan-in-place.
    id: str = field(default_factory=lambda: secrets.token_hex(8))
    steps: list[PlanStep] = field(default_factory=list)
    status: str = "active"               # "active" | "waiting" | "complete" | "cancelled" | "abandoned" | "failed"
    trace_id: Optional[str] = None       # cascade this plan belongs to
    created_at: datetime.datetime = field(default_factory=_utcnow)
    last_progress_at: datetime.datetime = field(default_factory=_utcnow)
    # Secondary trace_ids absorbed via wait-step matching. plan_for() checks
    # both the primary trace_id and this set so post-correlation lookups
    # resolve to the absorbing plan.
    additional_trace_ids: set[str] = field(default_factory=set)
    # Per-plan "dirty" state: a JSON-object COPY of the private (master) state,
    # taken on creation. Deltas apply here instead of master during plan
    # execution. On successful completion the whole object is COPIED OVER to
    # master (last-write-wins snapshot); discarded on cancel/abandon.
    state: dict = field(default_factory=dict)
    tool_rounds: int = 0                          # total tools dispatched for this trace (cross-turn cap)
    # Original DOMAIN message context, preserved for reply routing after async tool dispatch:
    original_sender: Optional[str] = None
    original_source_message_id: str = ""
    original_source_message_type: Optional["MessageType"] = None
    original_depth_remaining: int = 10
    original_source_plan_step_index: Optional[int] = None
    # Tool names dispatched across all async turns for this trace. Populated
    # when a turn ends with status="pending" (tools dispatched async) so the
    # evaluator in the continuation turn can verify tool steps were executed.
    accumulated_tools_called: list[str] = field(default_factory=list)
    # Set to True when the object's definition was modified by an admin
    # message while this plan was active. The runtime re-plans this trace
    # against the new definition on the next inbound message before
    # dispatching, replacing plan.steps and clearing the flag. The dirty
    # `state` is preserved across the re-plan.
    needs_replan: bool = False


@dataclass
class HistoryEntry:
    """A past message in an LLM-object's history, tagged with the task and
    plan generation that owned its processing.

    History is rendered grouped first by task_id (the stable request id),
    then by plan_id (the specific plan generation — re-minted on every
    replan). When a plan generation terminates (complete / cancelled /
    failed / abandoned) or is replaced by a replan, its entries are
    flushed.

    Both fields are None for messages that arrived outside any plan
    (admin, broadcasts, planner-off DOMAIN).
    """
    message: Message
    task_id: Optional[str] = None
    plan_id: Optional[str] = None


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
    state_update: Optional[Any] = None         # legacy singular delta; first entry of state_updates
    state_updates: list = field(default_factory=list)  # full list of deltas from this step (flat or nested)
    plan_update: Optional[PlanUpdate] = None   # applied ONLY on action="finish"
    tool_call: Optional[ToolCall] = None       # legacy: singular tool call
    tool_calls: list[ToolCall] = field(default_factory=list)  # preferred: batched tool calls dispatched async
    finish: Optional[ReactFinish] = None

    def __post_init__(self) -> None:
        # Normalize legacy singular form into the list. Caller may pass
        # either shape; downstream code reads `self.tool_calls` only.
        if self.tool_call is not None and not self.tool_calls:
            self.tool_calls = [self.tool_call]
        # Mirror state_update <-> state_updates so both forms are populated.
        if self.state_update is not None and not self.state_updates:
            self.state_updates = [self.state_update]
        elif self.state_updates and self.state_update is None:
            self.state_update = self.state_updates[0]


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
