import sys
from pathlib import Path as _Path

# mock/ is a sibling of src/ at the repo root; add it so `from schema import ...` works.
_mock_dir = str(_Path(__file__).parent.parent.parent / "mock")
if _mock_dir not in sys.path:
    sys.path.insert(0, _mock_dir)

from schema import (  # noqa: E402 — mock/schema.py
    MockImmediateResponse,
    MockCallback,
    MockMethodDef,
    MockSystemDef,
    MockScript,
    OrchestratorReaction,
    OrchestratorTrigger,
    OrchestratorScript,
    EventTrigger,
)

from pydantic import BaseModel, field_validator, Field
from typing import Literal, Optional, Union
from enum import Enum

from src.lnl.parser import slugify
from src.lnl.types import ObjectDefinition, PeerDeclaration

class ModType(str, Enum):
    temporal = "temporal"
    contextual = "contextual"
    exception = "exception"
    correction = "correction"
    expansion = "expansion"
    removal = "removal"

class Ambiguity(str, Enum):
    precise = "precise"
    semantic = "semantic"
    vague = "vague"
    implicit = "implicit"

class EventExpect(BaseModel):
    action: str
    reason: str

class Event(BaseModel):
    id: str
    call_type: str       # "send" or "send_event"
    source: str          # external sender
    recipient: str       # target object_id
    input: str
    when: str  # Same format as modification: "W02-1T09:00"
    expect: Optional[EventExpect] = None
    triggered_by: Optional[Union[str, EventTrigger]] = None  # sibling event ID or tool-call trigger
    trigger_delay_minutes: float = 0.0  # simulated delay after triggering event fires
    trigger_delay_seconds: float = 0.0
    role: Optional[Literal["pre_mod", "post_mod", "irrelevant"]] = None
    # pre_mod:    fires before the modification; tests that baseline behavior is unaffected
    # post_mod:   fires after the modification; tests that the system correctly reflects the change
    # irrelevant: fires at any time; tests functionality unrelated to the modification
    after_mod_ids: list[str] = Field(default_factory=list)
    # IDs of modifications that must have fired before this event is evaluated.
    # Empty → baseline (no mods active). Supersedes role-based inference for multi-mod scenarios.
    depends_on: list[str] = Field(default_factory=list)
    # IDs of prior events whose correct processing is required to answer/judge this event.
    # Used by state-probe TCs to condition probe accuracy on supporting events only.
    concurrent_group: Optional[str] = None
    # If set, this event belongs to a named concurrent group (e.g. "cgroup_pre_M001").
    # Events in the same group are dispatched together in one transaction at eval time.
    # Excluded from the main timeline sort; fired via the concurrent dispatch path instead.


class GeneratedEvent(BaseModel):
    """LLM output for a test event — expect is written in a separate pass after mock data is finalized."""
    id: str
    call_type: str
    source: str
    recipient: str
    input: str
    when: str
    triggered_by: Optional[str] = None
    trigger_delay_minutes: float = 0.0
    trigger_delay_seconds: float = 0.0
    role: Optional[Literal["pre_mod", "post_mod", "irrelevant"]] = None
    after_mod_ids: list[str] = Field(default_factory=list)
    concurrent_group: Optional[str] = None


class EventExpectationItem(BaseModel):
    event_id: str
    action: str
    reason: str


class EventExpectations(BaseModel):
    expectations: list[EventExpectationItem]


class ConcurrentGroupEvents(BaseModel):
    """LLM output for a concurrent event group — one focused generation call per group."""
    events: list[GeneratedEvent]


class GeneratedEventWithExpect(BaseModel):
    """LLM output for a state-probe event — expect is included in the generation (no separate pass)."""
    id: str
    call_type: str
    source: str
    recipient: str
    input: str
    when: str
    role: Optional[Literal["pre_mod", "post_mod", "irrelevant"]] = None
    after_mod_ids: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    triggered_by: Optional[str] = None
    trigger_delay_minutes: float = 0.0
    trigger_delay_seconds: float = 0.0
    expect: Optional[EventExpect] = None


class StateProbeScenario(BaseModel):
    """LLM output for a state-probe test case generation (legacy single-call)."""
    state_events: list[GeneratedEventWithExpect]  # N state-mutating events (expect filled by LLM)
    probe_events: list[GeneratedEventWithExpect]  # K probe questions (expect filled by LLM)


class StateEventsList(BaseModel):
    """Stage A output: only the N state-mutating events for a state-probe TC."""
    state_events: list[GeneratedEventWithExpect]


class StateProbeQuestion(BaseModel):
    """Stage B output: one probe question of a specific type, with its supporting events."""
    probe_event: GeneratedEventWithExpect


class FidelityEventsList(BaseModel):
    """Stage A output for the state-fidelity experiment.

    Contains the probe-target entity/field (passed to Stage B) plus all depth events.
    """
    probe_target_entity: str          # name/label of the probe-target entity, e.g. "Invoice #1042"
    probe_target_field: str           # scalar field Stage B should probe, e.g. "status"
    state_events: list[GeneratedEventWithExpect]


# ── Probe dataset schemas ─────────────────────────────────────────────────────

class ProbeType(str, Enum):
    direct_lookup = "direct_lookup"              # single field of one tracked entity
    aggregate = "aggregate"                       # sum/count over tracked entity set
    conditional_aggregate = "conditional_aggregate"  # filter then aggregate
    retraction_status = "retraction_status"       # is entity X still active / which of [...] are active


class TrackedEntitySpec(BaseModel):
    """One entity to track: creation + transitions + corrections will be generated for it."""
    entity_id: str       # e.g. "QUOTE-123"
    entity_type: str     # e.g. "quote", "order"
    n_corrections: int   # how many same-field correction events to generate (2–4)
    retracted: bool = False  # if True, a retraction event is appended after corrections


class ProbeTargetSpec(BaseModel):
    """One probe question to ask — references one or more tracked entities."""
    probe_id: str
    probe_type: ProbeType
    target_entities: list[str]   # entity_ids this probe references


class ProbeTargetsList(BaseModel):
    """Stage A output: probe targets + tracked entities for a probe-dataset TC."""
    shared_correction_field: str           # e.g. "discount_rate", "amount" — ALL entities correct this same field
    tracked_entities: list[TrackedEntitySpec]
    probe_targets: list[ProbeTargetSpec]   # ~equal mix of the 3 ProbeTypes


class RelevantEventsList(BaseModel):
    """Stage B output: events for one tracked entity (creation + transitions + corrections)."""
    entity_id: str
    state_events: list[GeneratedEventWithExpect]
    # expect.action = memory-fidelity assertion: "State for {entity_id} should now reflect …"


class ProbeQuestion(BaseModel):
    """Stage C output: one probe question with ground truth in expect.action."""
    probe_event: GeneratedEventWithExpect
    # expect.action = ground truth answer text
    # depends_on = IDs of relevant events this probe requires (2–7)


class BackgroundEventsList(BaseModel):
    """Stage D output: interference events (no expect — judge skips them)."""
    state_events: list[GeneratedEventWithExpect]
    # expect must be None on all events — background events are interference only


class GeneratedModification(BaseModel):
    """LLM output schema — mod_type and ambiguity are set by the script, not the LLM."""
    id: str
    call_type: str = "send"  # always "send" — user instruction to the object
    source: str = "__user__"  # always user-initiated
    target: str       # object_id being modified
    when: str
    intent: str

class Modification(BaseModel):
    """Full modification with script-assigned mod_type and ambiguity."""
    id: str
    call_type: str = "send"  # always "send" — user instruction to the object
    source: str = "__user__"  # always user-initiated
    target: str       # object_id being modified
    when: str
    mod_type: ModType
    intent: str
    ambiguity: Ambiguity

# Object definition schemas (Pydantic mirrors of LNL runtime dataclasses)
class PeerDecl(BaseModel):
    object_id: str
    relationship: str  # communication contract: "Notify with ticket details when unresolved request arrives"

    @field_validator("object_id")
    @classmethod
    def slugify_object_id(cls, v: str) -> str:
        return slugify(v)

class ObjectDef(BaseModel):
    object_id: str
    role: str
    state_description: str = ""
    behavior: str
    peers: list[PeerDecl] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    subscriptions: list[str] = Field(default_factory=list)
    event_sources: list[str] = Field(default_factory=list)
    @field_validator("object_id")
    @classmethod
    def slugify_object_id(cls, v: str) -> str:
        return slugify(v)

class Step(BaseModel):
    """A single workflow step: an NL text addressed to a specific LLM-object."""
    text: str        # Natural language message to send to the target object
    target: str      # object_id of the LLM-object this step addresses
    source: str = "__external__"  # external system originating this step (e.g. "slack", "hubspot")
    expect: Optional[EventExpect] = None  # Expected default-behavior outcome (no modifications applied)

    @field_validator("target")
    @classmethod
    def slugify_target(cls, v: str) -> str:
        return slugify(v)


# ── In-process mock tool schemas ─────────────────────────────────────────────

class MockToolTrigger(BaseModel):
    """When a tool fires (with optional arg matching), dispatch an event to another LNL object."""
    target_object_id: str
    message_template: str   # {arg_name} interpolation from tool call arguments
    source: str = "external"

class ScriptedMatchResponse(BaseModel):
    """Arg-pattern override: if tool call args match all patterns in `match`, return this response."""
    match: dict[str, str]   # arg key → regex pattern
    response: str           # supports {arg_name} and {call_index} interpolation

class MockToolDef(BaseModel):
    """Mock definition for one external tool used during LNL evaluation."""
    tool_name: str              # e.g. "email.send", "slack.send_message"
    description: str            # shown in system prompt
    arguments_schema: dict      # JSON schema for tool arguments
    response_template: str      # default response when scripted_responses is exhausted ({arg_name} interpolation)
    scripted_responses: list[str] = Field(
        default_factory=list,
        description=(
            "Ordered list of responses consumed one-per-call (FIFO). "
            "Supports {arg_name} and {call_index} interpolation. "
            "When exhausted, falls back to response_template."
        ),
    )
    match: dict[str, str] = Field(default_factory=dict)  # arg key → regex to gate triggers
    triggers: list[MockToolTrigger] = Field(default_factory=list)
    scripted_match_responses: list[ScriptedMatchResponse] = Field(
        default_factory=list,
        description="Checked in order before response_template. First entry whose match dict passes (regex per arg) wins. Supports {arg_name} and {call_index} interpolation.",
    )

class MockConfig(BaseModel):
    """Collection of mock tool definitions — loadable from YAML at evaluation time."""
    tools: list[MockToolDef]


class LLMClassDef(BaseModel):
    """An llm-class template: a reusable blueprint for spawning llm-object instances."""
    class_id: str
    role: str
    state_description: str = ""
    behavior: str
    peers: list[PeerDecl] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    subscriptions: list[str] = Field(default_factory=list)
    event_sources: list[str] = Field(default_factory=list)

    @field_validator("class_id")
    @classmethod
    def slugify_class_id(cls, v: str) -> str:
        return slugify(v)


class Sample(BaseModel):
    id: str
    sample_id: str = ""  # ID of the originating sample; shared across all TC variants from the same sample
    name: str
    domain: str
    source_type: str
    link: str = ""
    objects: list[ObjectDef] = Field(default_factory=list)
    llm_classes: list[LLMClassDef] = Field(default_factory=list, description="llm-class templates available for spawning during evaluation")
    steps: list[Step]
    modifications: list[Modification]
    events: list[Event]
    mock_tools: list[MockToolDef] = Field(default_factory=list, description="Per-test-case mock tool overrides. Merged with --mock-config at eval time (these win on collision).")

class Samples(BaseModel):
    test_cases: list[Sample]

# ── Multi-stage sample generation intermediate schemas ────────────────────────

class GroundedTemplate(BaseModel):
    """Output of Stage 1a (grounding): abstract template with all placeholders resolved."""
    name: str                   # instantiated name (e.g. "HubSpot → Slack Deal Alerts")
    domain: str
    grounded_steps: list[str]   # raw_steps with all abstract placeholders replaced by specific values

class ObjectGraph(BaseModel):
    """Output of Stage 1b (object identification): distributed LLM-object system design."""
    objects: list[ObjectDef]

class WorkflowSteps(BaseModel):
    """Output of Stage 1c (step writing): external trigger steps only."""
    steps: list[Step]


# Workflow generation schemas
class Workflow(BaseModel):
    id: str
    name: str
    domain: str
    source_type: str
    link: str = ""
    raw_steps: list[str]
    objects: list[ObjectDef]
    steps: list[Step]
    mock_tools: list[MockToolDef] = Field(
        default_factory=list,
        description="Mock tool definitions for read-service objects. Auto-generated by the pipeline from state_description and step context.",
    )
    flagged: bool = False
    flag_reasons: list[str] = Field(
        default_factory=list,
        description="Human-reviewed structural issues that were kept but not fixed. Flagged samples are included in Stage 2 but marked for manual follow-up.",
    )

class Workflows(BaseModel):
    samples: list[Workflow]


# ── Workflow-step validation schemas ──────────────────────────────────────────


class StepJudgement(BaseModel):
    """LLM judge output for one workflow step (fidelity + quality)."""
    # Fidelity: how faithfully is the abstract raw_step grounded?
    verdict: Literal["FAITHFUL", "DRIFTED", "WRONG"]
    reasoning: str
    # Quality: independent of fidelity, is the grounded text well-written?
    quality: Literal["GOOD", "ADEQUATE", "POOR"] = "ADEQUATE"
    quality_issues: list[str] = Field(
        default_factory=list,
        description=(
            "Specific quality complaints the judge found, e.g., 'placeholder "
            "value Company-A not grounded', 'expect.action bundles multiple "
            "observable outcomes', 'product name FormPlatform is not real'."
        ),
    )


class StepVerdict(BaseModel):
    """Per-step verdict combining deterministic health + LLM fidelity + LLM quality."""
    workflow_id: str
    step_index: int
    raw_step: str
    grounded_step: str
    expect_action: Optional[str] = None
    target: str = ""
    # Fidelity: how well the grounded step matches the abstract raw_step (LLM)
    verdict: Literal["FAITHFUL", "DRIFTED", "WRONG", "UNALIGNED"]
    reasoning: str
    # Health: deterministic structural checks (no LLM)
    health_issues: list[str] = Field(
        default_factory=list,
        description=(
            "Structural problems detected without an LLM call. Empty list = "
            "structurally healthy. Examples: 'text is empty', 'target does "
            "not exist in workflow.objects', 'target has no event_sources', "
            "'expect.action is missing'."
        ),
    )
    # Quality: subjective LLM judgement on whether the grounded step is well-written
    quality: Literal["GOOD", "ADEQUATE", "POOR", "UNALIGNED"] = "ADEQUATE"
    quality_issues: list[str] = Field(default_factory=list)
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0


class WorkflowValidation(BaseModel):
    """Aggregate validation result for one workflow."""
    workflow_id: str
    n_template_steps: int
    n_workflow_steps: int
    count_mismatch: bool
    step_verdicts: list[StepVerdict] = Field(default_factory=list)
    # Fidelity rollup (existing)
    aggregate: Literal["CLEAN", "MILD_DRIFT", "NOTABLE_DRIFT", "WRONG"]
    # Health rollup: OK if zero health_issues across all steps; otherwise ISSUES.
    aggregate_health: Literal["OK", "ISSUES"] = "OK"
    # Quality rollup: worst per-step quality.
    aggregate_quality: Literal["GOOD", "ADEQUATE", "POOR"] = "ADEQUATE"

# Scenario generation schemas (LLM output before merging with instance metadata)
class Scenario(BaseModel):
    id: str
    sample_id: str
    description: str
    modifications: list[GeneratedModification]
    events: list[GeneratedEvent]

class Scenarios(BaseModel):
    scenarios: list[Scenario]


# Run configuration record (written as first line of every eval results file)

class RunConfig(BaseModel):
    """Snapshot of all CLI parameters used for an evaluation run.

    Written as the first record (or appended on continuation) in the results
    JSONL so any results file is self-describing. The continuation parser skips
    this record because it lacks ``tc_id`` / ``run_index`` fields.
    """
    record_type: str = "run_config"
    version: str = "unknown"
    timestamp: str
    input_path: str
    output_path: str
    model: str
    provider: str
    judge_model: str
    judge_provider: str
    judge_specs: list[str] = Field(
        default_factory=list,
        description="All judge specs (provider/model) when using --llm-judge. Empty for single-judge runs.",
    )
    tracked_judge: Optional[str] = Field(
        default=None,
        description="Path to YAML override prompt used to judge tracked (role=irrelevant) events. "
                    "None means the default per-event judge was used.",
    )
    runs: int
    workers: int
    timeout_s: Optional[float]
    seed: Optional[int]
    steps_only: bool
    max_chain_depth: int
    mock_config_paths: list[str] = Field(default_factory=list)
    tc_filter: Optional[list[str]] = None
    limit: Optional[int] = None
    concurrency: Optional[int] = None
    modifications: Optional[int] = None
    is_continuation: bool = False


# Evaluation result schemas

class EventResult(BaseModel):
    """Result of executing a single test event."""
    event_id: str
    passed: bool
    reasoning: str
    expected: str = ""
    evidence: str = ""      # gather_evidence() output — what the judge saw
    prior_context: str = "" # _format_prior_state() snapshot before this event
    role: Optional[Literal["pre_mod", "post_mod", "irrelevant"]] = None  # propagated from Event.role
    input_tokens: int = 0   # entry-agent LLM input tokens (baseline) or LNL agent tokens (lnl eval)
    output_tokens: int = 0  # entry-agent LLM output tokens
    planner_input_tokens: int = 0
    planner_output_tokens: int = 0
    executor_input_tokens: int = 0
    executor_output_tokens: int = 0
    evaluator_input_tokens: int = 0
    evaluator_output_tokens: int = 0
    latency_ms: float = 0.0
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0
    judge_votes: list[dict] = Field(
        default_factory=list,
        description="Per-judge votes (always populated). Each entry: {judge, passed, reasoning, input_tokens, output_tokens}.",
    )
    # Chattiness / roundtrip metrics (baseline eval only)
    agent_tool_calls: int = 0   # total tool calls made by the entry agent (file reads, sessions_send, external)
    a2a_calls: int = 0          # sessions_send (agentToAgent) calls made by the entry agent
    entry_tool_names: list[str] = Field(
        default_factory=list,
        description=(
            "Ordered list of tool names invoked by the entry agent for this event "
            "(includes sessions_send, file ops, external tool calls). Sourced from "
            "sessions.get() — authoritative LLM message history."
        ),
    )
    peer_tool_names: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Per-peer tool inventory for this event: {peer_object_id: [tool, tool, ...]}. "
            "Peers are the non-entry agents in the TC. Each value is the ordered list of "
            "tools that peer's :main session executed during this event's cascade — "
            "captured via sessions.get() right after the entry's execute() returns. "
            "Lets us distinguish cascade-stall modes: empty list = peer never received / "
            "errored before processing; non-empty without sessions_send = peer received but "
            "didn't propagate further; non-empty with sessions_send = peer cascaded further."
        ),
    )
    mock_tool_calls: int = 0    # external tool calls across all agents (from mock server log)
    infra_error: bool = False   # True when failure_class in {oc_eval, infra_provider} — excluded from scores (back-compat mirror of failure_class)
    failure_class: Optional[Literal["oc_eval", "infra_provider", "behavioral"]] = Field(
        default=None,
        description=(
            "Three-way classification of failed events. None = event passed. "
            "'oc_eval' = OpenClaw integration / our framework failed (gateway not ready, "
            "sessions_send timeout, TC wall-clock timeout, runtime aborted, pairing errors) — "
            "factored OUT of pass-rate, on us to fix. "
            "'infra_provider' = Azure/LLM-provider failed (rate-limit, content filter, "
            "schema reject, HTTP 5xx, 'agent completed with no response') — factored OUT "
            "of pass-rate, not our responsibility. "
            "'behavioral' = agent's reasoning produced a real failure (wrong tool args, "
            "missing field, wrong value, missing dispatch, lied about completion, wrong "
            "branch). Kept IN pass-rate as the true measure of agent quality."
        ),
    )
    # Transaction trace (LNL eval only) — structured per-hop cascade of the triggering event
    trace: list[dict] = Field(default_factory=list)
    trace_root_id: Optional[str] = None  # msg.id of the root trigger; shared as trace_id by every hop
    # ── Diagnostic logging (LNL eval only — opt-in via --log-planner-output) ──
    planner_plans: list[dict] = Field(
        default_factory=list,
        description=(
            "Per-object planner output snapshot taken at the end of this event. "
            "Each entry: {object_id, trace_id, goal, status, steps:[{id, kind, target, "
            "description, status, depends_on}]}. Lets us see whether the planner emitted "
            "the steps the modification required. Empty unless --log-planner-output is set."
        ),
    )
    outgoing_messages: list[dict] = Field(
        default_factory=list,
        description=(
            "Bus messages emitted during this event's cascade. Each entry: "
            "{sender, recipient, type, content, trace_id, in_reply_to}. Lets us inspect "
            "the actual content of peer dispatches (whether the executor included the "
            "modification's new directive in the outgoing message text). Empty unless "
            "--log-planner-output is set."
        ),
    )

class ModificationResult(BaseModel):
    """Cost of applying a single modification."""
    mod_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    planner_input_tokens: int = 0
    planner_output_tokens: int = 0
    executor_input_tokens: int = 0
    executor_output_tokens: int = 0
    evaluator_input_tokens: int = 0
    evaluator_output_tokens: int = 0
    latency_ms: float = 0.0



class SampleResult(BaseModel):
    """Result of running a single Sample (one run)."""
    tc_id: str
    sample_id: str = ""          # propagated from Sample.sample_id
    tc_index: int = -1           # 0-based position in input file; -1 for legacy results
    seed: Optional[int] = None   # effective seed used for this run (base_seed + run_index)
    name: str
    domain: str
    run_index: int  # 0-based; >0 only when --runs > 1
    events: list[EventResult]
    modifications: list[ModificationResult]
    pass_rate: Optional[float]  # passed_events / total_events; None if no evaluable events
    elapsed_ms: Optional[float] = None  # wall-clock time for the entire TC run in milliseconds
    base_elapsed_ms: Optional[float] = None        # sum of event latency_ms where role=None
    pre_mod_elapsed_ms: Optional[float] = None     # sum of event latency_ms where role=pre_mod
    post_mod_elapsed_ms: Optional[float] = None    # sum of event latency_ms where role=post_mod
    irrelevant_elapsed_ms: Optional[float] = None  # sum of event latency_ms where role=irrelevant
    error_type: Optional[str] = None    # "infra" = infrastructure failure (pairing, network, terminated); None = behavioral

class EvalSummary(BaseModel):
    """Aggregate metrics across all test cases and runs."""
    total_test_cases: int
    total_runs: int
    total_events: int
    mean_pass_rate: float
    pass_rate_std: Optional[float] = None  # behavioral consistency (std dev across runs); None for single-run evals
    # ── Per-role pass rates ──────────────────────────────────────────���─────────
    steps_pass_rate: Optional[float] = None        # S\d+ events mean fraction (baseline setup)
    steps_pass_rate_std: Optional[float] = None
    samples_completion: Optional[float] = None     # fraction of TCs where ALL step events passed
    samples_completion_std: Optional[float] = None
    mod_pass_rate: Optional[float] = None          # all modification events (pre+post+irrelevant combined, conclusive TCs only)
    mod_pass_rate_std: Optional[float] = None
    mod_pass_rate_all: Optional[float] = None      # same but including inconclusive TCs
    mod_pass_rate_all_std: Optional[float] = None
    pre_mod_pass_rate: Optional[float] = None      # events before modification fires (conclusive TCs only)
    pre_mod_pass_rate_std: Optional[float] = None
    pre_mod_pass_rate_all: Optional[float] = None  # same but including inconclusive TCs
    pre_mod_pass_rate_all_std: Optional[float] = None
    post_mod_pass_rate: Optional[float] = None     # events after modification fires (key signal, conclusive TCs only)
    post_mod_pass_rate_std: Optional[float] = None
    post_mod_pass_rate_all: Optional[float] = None # same but including inconclusive TCs
    post_mod_pass_rate_all_std: Optional[float] = None
    irrelevant_pass_rate: Optional[float] = None   # events unrelated to the modification (conclusive TCs only)
    irrelevant_pass_rate_std: Optional[float] = None
    irrelevant_pass_rate_all: Optional[float] = None  # same but including inconclusive TCs
    irrelevant_pass_rate_all_std: Optional[float] = None
    inconclusive_tcs: int = 0                      # TCs where steps failed → mod result uninterpretable
    infra_error_tcs: int = 0                       # TCs excluded from scoring due to infra errors (e.g. content filter)
    # ── Token / latency means ──────────────────────────────────────────────────
    mean_event_input_tokens: float
    mean_event_output_tokens: float
    mean_event_latency_ms: float
    mean_mod_input_tokens: float
    mean_mod_output_tokens: float
    mean_mod_latency_ms: float
    # ── Per-role mean event latency ────────────────────────────────────────────
    mean_base_event_latency_ms: Optional[float] = None
    mean_pre_mod_event_latency_ms: Optional[float] = None
    mean_post_mod_event_latency_ms: Optional[float] = None
    mean_irrelevant_event_latency_ms: Optional[float] = None
    # ── Token totals ───────────────────────────────────────────────────────────
    total_agent_input_tokens: int = 0
    total_agent_output_tokens: int = 0
    total_judge_input_tokens: int = 0
    total_judge_output_tokens: int = 0
    total_planner_input_tokens: int = 0
    total_planner_output_tokens: int = 0
    total_executor_input_tokens: int = 0
    total_executor_output_tokens: int = 0
    total_evaluator_input_tokens: int = 0
    total_evaluator_output_tokens: int = 0


# MockImmediateResponse, MockCallback, MockMethodDef, MockSystemDef, MockScript,
# OrchestratorReaction, OrchestratorTrigger, OrchestratorScript — imported from mock/schema.py above.


def to_lnl_class_definition(cls_def: LLMClassDef) -> ObjectDefinition:
    """Convert Pydantic LLMClassDef to dataclass ObjectDefinition."""
    return ObjectDefinition(
        object_id=cls_def.class_id,
        role=cls_def.role,
        behavior=cls_def.behavior,
        peers=[
            PeerDeclaration(object_id=p.object_id, relationship=p.relationship)
            for p in cls_def.peers
        ],
        skills=list(cls_def.skills),
        subscriptions=list(cls_def.subscriptions),
        event_sources=list(cls_def.event_sources),
        initial_state=cls_def.state_description,
    )


def to_lnl_definition(obj: ObjectDef) -> ObjectDefinition:
    """Convert Pydantic ObjectDef to dataclass ObjectDefinition."""
    return ObjectDefinition(
        object_id=obj.object_id,
        role=obj.role,
        behavior=obj.behavior,
        peers=[
            PeerDeclaration(object_id=p.object_id, relationship=p.relationship)
            for p in obj.peers
        ],
        skills=list(obj.skills),
        subscriptions=list(obj.subscriptions),
        event_sources=list(obj.event_sources),
        initial_state=obj.state_description,
    )
