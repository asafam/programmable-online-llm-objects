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

    @field_validator("object_id")
    @classmethod
    def slugify_object_id(cls, v: str) -> str:
        return slugify(v)

class Step(BaseModel):
    """A single workflow step: an NL text addressed to a specific LLM-object."""
    text: str        # Natural language message to send to the target object
    target: str      # object_id of the LLM-object this step addresses
    expect: Optional[EventExpect] = None  # Expected default-behavior outcome (no modifications applied)

    @field_validator("target")
    @classmethod
    def slugify_target(cls, v: str) -> str:
        return slugify(v)

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
    )
