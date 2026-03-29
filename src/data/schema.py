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
    expect: EventExpect
    triggered_by: Optional[str] = None  # ID of sibling event that causes this one to fire
    trigger_delay_minutes: float = 0.0  # simulated delay after triggering event fires
    trigger_delay_seconds: float = 0.0

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
    state_description: str
    behavior: str
    peers: list[PeerDecl] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    subscriptions: list[str] = Field(default_factory=list)
    event_sources: list[str] = Field(default_factory=list)
    seed_data: dict = Field(default_factory=dict, description="Static reference data for read services (org chart, price list, policy table, etc.). Never mutated at runtime. Write services and business logic objects leave this as {}.")

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

class MockConfig(BaseModel):
    """Collection of mock tool definitions — loadable from YAML at evaluation time."""
    tools: list[MockToolDef]


class TestCase(BaseModel):
    id: str
    name: str
    domain: str
    source_type: str
    link: str
    objects: list[ObjectDef] = Field(default_factory=list)
    steps: list[Step]
    modifications: list[Modification]
    events: list[Event]
    mock_tools: list[MockToolDef] = Field(default_factory=list, description="Per-test-case mock tool overrides. Merged with --mock-config at eval time (these win on collision).")

class TestCases(BaseModel):
    test_cases: list[TestCase]

# Sample generation schemas
class Sample(BaseModel):
    id: str
    name: str
    domain: str
    source_type: str
    link: str
    raw_steps: list[str]
    objects: list[ObjectDef]
    steps: list[Step]

class Samples(BaseModel):
    samples: list[Sample]

# Scenario generation schemas (LLM output before merging with instance metadata)
class Scenario(BaseModel):
    id: str
    sample_id: str
    description: str
    modifications: list[GeneratedModification]
    events: list[Event]

class Scenarios(BaseModel):
    scenarios: list[Scenario]


# Evaluation result schemas

class EventResult(BaseModel):
    """Result of executing a single test event."""
    event_id: str
    passed: bool
    reasoning: str
    expected: str = ""
    evidence: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0

class ModificationResult(BaseModel):
    """Cost of applying a single modification."""
    mod_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0

class TestCaseResult(BaseModel):
    """Result of running a single TestCase (one run)."""
    tc_id: str
    name: str
    domain: str
    run_index: int  # 0-based; >0 only when --runs > 1
    events: list[EventResult]
    modifications: list[ModificationResult]
    pass_rate: float  # passed_events / total_events

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
        state_description=obj.state_description,
        behavior=obj.behavior,
        peers=[
            PeerDeclaration(object_id=p.object_id, relationship=p.relationship)
            for p in obj.peers
        ],
        skills=list(obj.skills),
        subscriptions=list(obj.subscriptions),
        event_sources=list(obj.event_sources),
        seed_data=dict(obj.seed_data),
    )
