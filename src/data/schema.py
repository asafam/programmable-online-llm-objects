from pydantic import BaseModel, field_validator, Field
from typing import Literal, Optional
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
    triggered_by: Optional[str] = None  # ID of sibling event that causes this one to fire
    trigger_delay_minutes: float = 0.0  # simulated delay after triggering event fires
    trigger_delay_seconds: float = 0.0


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


class EventExpectationItem(BaseModel):
    event_id: str
    action: str
    reason: str


class EventExpectations(BaseModel):
    expectations: list[EventExpectationItem]

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


class TestCase(BaseModel):
    id: str
    sample_id: str = ""  # ID of the originating sample; shared across all TC variants from the same sample
    name: str
    domain: str
    source_type: str
    link: str = ""
    objects: list[ObjectDef] = Field(default_factory=list)
    steps: list[Step]
    modifications: list[Modification]
    events: list[Event]
    mock_tools: list[MockToolDef] = Field(default_factory=list, description="Per-test-case mock tool overrides. Merged with --mock-config at eval time (these win on collision).")

class TestCases(BaseModel):
    test_cases: list[TestCase]

# ── Multi-stage sample generation intermediate schemas ────────────────────────

class GroundedTemplate(BaseModel):
    """Output of Stage 1a (grounding): abstract template with all placeholders resolved."""
    name: str                   # instantiated name (e.g. "HubSpot → Slack Deal Alerts")
    domain: str
    grounded_steps: list[str]   # raw_steps with all abstract placeholders replaced by specific values

class ObjectGraph(BaseModel):
    """Output of Stage 1b (object identification): distributed LLM-object system design."""
    objects: list[ObjectDef]

class SampleSteps(BaseModel):
    """Output of Stage 1c (step writing): external trigger steps only."""
    steps: list[Step]


# Sample generation schemas
class Sample(BaseModel):
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

class Samples(BaseModel):
    samples: list[Sample]

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
    timestamp: str
    input_path: str
    output_path: str
    runs_path: str = ""   # path to sibling _runs.jsonl artifact file (empty for legacy runs)
    model: str
    provider: str
    judge_model: str
    judge_provider: str
    judge_specs: list[str] = Field(
        default_factory=list,
        description="All judge specs (provider/model) when using --llm-judge. Empty for single-judge runs.",
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
    is_continuation: bool = False


# Evaluation result schemas

class EventResult(BaseModel):
    """Result of executing a single test event."""
    event_id: str
    passed: bool
    reasoning: str
    expected: str = ""
    evidence: str = Field(
        default="",
        description=(
            "Deprecated: no longer populated by evaluate.py. "
            "Full evidence is stored in the sibling _runs.jsonl artifact file. "
            "Field kept for backward-compatible deserialization of older result files."
        ),
    )
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    judge_votes: list[dict] = Field(
        default_factory=list,
        description="Per-judge votes when a panel judge is used. Each entry: {judge, passed, reasoning}.",
    )

class ModificationResult(BaseModel):
    """Cost of applying a single modification."""
    mod_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0


class RawEventData(BaseModel):
    """Raw execution data for one evaluable event — judge inputs captured before any verdict.

    Written to the _runs.jsonl artifact file alongside each evaluation run.
    Used by re_evaluate.py to re-judge with a different model or updated expectations.
    """
    event_id: str
    expected: str          # event.expect.action (condition passed to judge)
    evidence: str          # gather_evidence() output
    prior_context: str     # _format_prior_state() snapshot before this event
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0


class RawTestCaseResult(BaseModel):
    """Execution snapshot for one TestCase run — no judge verdicts included.

    Written as one line per (tc, run) to the _runs.jsonl artifact file.
    Contains everything needed to re-judge without re-running the LNL runtime.
    """
    record_type: Literal["raw_run"] = "raw_run"
    tc_id: str
    sample_id: str = ""
    tc_index: int = -1
    seed: Optional[int] = None
    name: str
    domain: str
    run_index: int
    events: list[RawEventData]               # only events/steps that have expect
    modifications: list[ModificationResult]


class TestCaseResult(BaseModel):
    """Result of running a single TestCase (one run)."""
    tc_id: str
    sample_id: str = ""          # propagated from TestCase.sample_id
    tc_index: int = -1           # 0-based position in input file; -1 for legacy results
    seed: Optional[int] = None   # effective seed used for this run (base_seed + run_index)
    name: str
    domain: str
    run_index: int  # 0-based; >0 only when --runs > 1
    events: list[EventResult]
    modifications: list[ModificationResult]
    pass_rate: Optional[float]  # passed_events / total_events; None if no evaluable events

class EvalSummary(BaseModel):
    """Aggregate metrics across all test cases and runs."""
    total_test_cases: int
    total_runs: int
    total_events: int
    mean_pass_rate: float
    pass_rate_std: float      # behavioral consistency (std dev across runs)
    mean_event_input_tokens: float
    mean_event_output_tokens: float
    mean_event_latency_ms: float
    mean_mod_input_tokens: float
    mean_mod_output_tokens: float
    mean_mod_latency_ms: float


# ── Mock external system schemas (OpenClaw baseline) ─────────────────────────

class MockImmediateResponse(BaseModel):
    """Synchronous response returned to the tool caller."""
    template: str   # e.g. "message_id: {tool_call_id}, delivered to #{channel}"
    status: str = "ok"

class MockCallback(BaseModel):
    """Optional follow-up message injected back into the agent session."""
    delay_seconds: float = 0.5
    message_template: str   # interpolated with tool call args; ignored in LLM mode
    source: str             # e.g. "slack" — for log grouping

class MockMethodDef(BaseModel):
    """Behaviour definition for one tool method."""
    method: str                          # e.g. "slack_send_message"
    immediate: MockImmediateResponse
    callback: Optional[MockCallback] = None
    llm_persona: Optional[str] = None   # if set, use LLM mode for this method

class MockSystemDef(BaseModel):
    """Complete mock definition for one external system."""
    system: str
    tools: list[MockMethodDef]

class MockScript(BaseModel):
    """Collection of mock system definitions for one evaluation run."""
    systems: list[MockSystemDef]

    def get_method(self, method: str) -> Optional[MockMethodDef]:
        for sys in self.systems:
            for tool in sys.tools:
                if tool.method == method:
                    return tool
        return None


# ── Orchestration schemas ─────────────────────────────────────────────────────

class OrchestratorReaction(BaseModel):
    """A single action to fire after a trigger matches."""
    source: str                         # e.g. "slack", "email" — appears in injection prefix
    message: str                        # template with {arg} interpolation from tool call args
    after_seconds: float = 0.0          # real-time delay (scaled by time_scale)
    after_minutes: float = 0.0          # simulated minutes (scaled by time_scale)

class OrchestratorTrigger(BaseModel):
    """Rule: when tool `tool` fires and args match `match`, schedule `reactions`."""
    tool: str                           # tool method name, e.g. "email_send"
    match: dict[str, str] = Field(default_factory=dict)  # arg key → regex pattern (empty = match all)
    reactions: list[OrchestratorReaction]
    fire_once: bool = True              # if True, fires only on the first matching call per session

class OrchestratorScript(BaseModel):
    """Named scenario script defining cross-system event chains."""
    name: str
    time_scale: float = 1.0             # compress time: 0.01 → 1 simulated min = 0.6 real sec
    triggers: list[OrchestratorTrigger]


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
