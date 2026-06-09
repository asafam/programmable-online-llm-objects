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

class StateConstraintType(str, Enum):
    cap = "cap"               # cumulative total across requests must stay at/below a ceiling (e.g. $50K discounts)
    counter = "counter"       # per-key count within a period is capped (e.g. max 2 leads / rep / day)
    rate_limit = "rate_limit" # at most N occurrences for the same key in a rolling window (e.g. 2 reorders / 7d)
    trigger = "trigger"       # INVERSE: the Nth related occurrence FIRES the gated action (e.g. 3 complaints
                              # about the same product within 7d → escalation); earlier ones only accumulate
    dedup = "dedup"           # repeats suppressed: the first occurrence per key is processed; identical ones
                              # within a SHORT rolling window (e.g. 10 min) are ignored as duplicates

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
    role: Optional[Literal["base", "pre_mod", "post_mod", "irrelevant"]] = None
    # base:       external-trigger event; fires to build runtime state (no modification active)
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
    role: Optional[Literal["base", "pre_mod", "post_mod", "irrelevant"]] = None
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
    role: Optional[Literal["base", "pre_mod", "post_mod", "irrelevant"]] = None
    after_mod_ids: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    triggered_by: Optional[str] = None
    trigger_delay_minutes: float = 0.0
    trigger_delay_seconds: float = 0.0
    expect: Optional[EventExpect] = None


class StateConstraint(BaseModel):
    """Spec-level descriptor of the cross-request invariant a workflow's base scenario
    exercises — IMPLEMENTATION-AGNOSTIC.

    It names no owning object and no mechanism: whether the invariant is realized by a
    single-writer Custodian or otherwise is decided in object identification (Stage 1),
    not here. Stage 1.5 records this descriptor and authors the base events that exercise
    it; the expectations are phrased in observable terms, not internal mechanics.
    """
    type: StateConstraintType
    threshold: str            # the limit, e.g. "$50,000 cumulative per quarter"
    description: str = ""     # one line stating the invariant in observable terms


class GeneratedStateConstraint(BaseModel):
    """LLM output for Stage 1.5 — the state-infused base scenario, decoupled from
    implementation.

    The model reads the workflow (objects/steps describe its rules), classifies the
    cross-request invariant, and authors a role='base' event sequence whose expects
    state the OBSERVABLE outcome at each step (admitted vs blocked/held at the
    threshold) — never any internal mechanism (no custodian, no reserve/confirm).
    """
    constraint_type: StateConstraintType
    threshold: str
    description: str
    seed: str = ""  # initial reference state the system READS (roster/catalog/approvers/
                    # starting totals), object-agnostic; the base events + expects are consistent with it
    base_events: list["SpecEventWithExpect"]  # object-free; recipient bound in Phase 2


class RolePhrasing(BaseModel):
    """How one kind of request READS (NL template with {ID}/{AMOUNT}/{KEY}/{APPROVER}/{DECO}
    placeholders that code fills). role ∈ {request, submit, approve, allowed, blocked, approved,
    held, submitted}."""
    role: str
    template: str


class SampleVerdict(BaseModel):
    """LLM-judge output for the pre-upload verifier."""
    passed: bool
    issues: list[str] = Field(default_factory=list)


class GeneratedScenarioSpec(BaseModel):
    """LLM output for the CODE-GENERATED scenario flow: the model supplies ONLY realism (the
    structured seed + phrasing templates + a decoration pool). Code builds the request sequence
    (counts, timestamps, the concurrent pair at the last slot, the reset) and derives every
    expect by simulation — so the scenario is logically correct by construction."""
    constraint_type: StateConstraintType
    threshold: str
    description: str
    seed: str = ""                  # structured JSON reference state (roster/catalog/approvers)
    phrasings: list[RolePhrasing] = Field(default_factory=list)  # request + OUTCOME phrasings (roles below)
    decorations: list[str] = Field(default_factory=list)  # realistic NL fragments for {DECO}
    key: str = ""                   # rate_limit: the specific key (SKU) to exercise, from the seed
    unit: str = ""                  # the gated action's noun ("reorder", "lead assignment", "approval")
    entities: list[str] = Field(default_factory=list)  # counter: rotation members IN ORDER (any domain:
                                                       # reps/channels/agents) — names exactly as in the seed
    keys: list[str] = Field(default_factory=list)      # rate_limit: the limit-tracked key values from the
                                                       # seed (SKUs/categories/contacts); first = main
    irrelevant_key: str = ""        # a real seed entity OUTSIDE the invariant for the irrelevant event


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
    # Categorical: True iff this object is the single-writer owner of a
    # cross-request/instance invariant (a Custodian — see identify_objects.yaml
    # category 3). A custodian decides from its own state in one message and
    # only replies; it has NO peers. Any peers emitted for a custodian are
    # stripped at assembly (generate_workflows._assemble_sample), so the
    # peerless / atomic property holds by construction.
    is_custodian: bool = False
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
    template: list[str] = Field(default_factory=list)  # raw/abstract base steps (the original template)
    seed: str = ""  # initial reference state the system reads (roster/catalog/approvers/starting totals)
    keys: list[str] = Field(default_factory=list)  # rate_limit: limit-tracked key values (for verification)
    entities: list[str] = Field(default_factory=list)  # counter: rotation members (for verification)
    objects: list[ObjectDef] = Field(default_factory=list)
    llm_classes: list[LLMClassDef] = Field(default_factory=list, description="llm-class templates available for spawning during evaluation")
    steps: list[str] = Field(default_factory=list)  # grounded workflow steps (incl. the state-constraint step)
    modifications: list[Modification]
    events: list[Event]
    tools: list[MockToolDef] = Field(
        default_factory=list,
        alias_priority=2,
        validation_alias="tools",
        description="Mock tool definitions. Merged with --mock-config at eval time (these win on collision).",
    )
    mock_tools: list[MockToolDef] = Field(
        default_factory=list,
        description="Deprecated alias for tools. Loaded from old JSONL; use .tools instead.",
        exclude=True,
    )
    state_constraint: Optional[StateConstraint] = Field(
        default=None,
        description="Baseline state invariant enforced from the base scenario (Stage 1.5). None for ordinary test cases.",
    )

    @property
    def all_events(self) -> "list[Event]":
        """Unified event list: base + mod events from self.events."""
        return self.events

    def model_post_init(self, __context: object) -> None:
        # Merge legacy mock_tools into tools if tools is empty
        if self.mock_tools and not self.tools:
            self.tools = self.mock_tools

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
    template: list[str] = Field(default_factory=list)   # abstract steps verbatim from templates.yaml
    raw_steps: list[str] = Field(default_factory=list, exclude=True)  # legacy alias; loaded from old JSONL
    objects: list[ObjectDef]
    steps: list[str] = Field(default_factory=list)  # grounded workflow steps (all N, with object detail)
    events: list[Event] = Field(default_factory=list)   # base events (role="base") for evaluation
    tools: list[MockToolDef] = Field(
        default_factory=list,
        description="Mock tool definitions. Auto-generated at workflow-generation time.",
    )
    mock_tools: list[MockToolDef] = Field(
        default_factory=list,
        description="Deprecated alias for tools. Loaded from old JSONL; use .tools instead.",
        exclude=True,
    )
    flagged: bool = False
    flag_reasons: list[str] = Field(
        default_factory=list,
        description="Human-reviewed structural issues that were kept but not fixed. Flagged samples are included in Stage 2 but marked for manual follow-up.",
    )
    state_constraint: Optional[StateConstraint] = Field(
        default=None,
        description="Baseline state invariant injected by Stage 1.5 (generate_state_constraints). None unless --state-constraint was used.",
    )

    @property
    def all_events(self) -> "list[Event]":
        """Unified event list: base events (in self.events with role='base') + mod events."""
        return self.events

    def model_post_init(self, __context: object) -> None:
        # Merge legacy mock_tools into tools
        if self.mock_tools and not self.tools:
            self.tools = self.mock_tools
        # Merge legacy raw_steps into template
        if self.raw_steps and not self.template:
            self.template = self.raw_steps

class Workflows(BaseModel):
    samples: list[Workflow]


# ── Workflow-step validation schemas ──────────────────────────────────────────


class RawStepClassification(BaseModel):
    """LLM classification of a template raw_step as TRIGGER or CASCADE."""
    index: int  # 1-based
    classification: Literal["TRIGGER", "CASCADE"]


class StepJudgement(BaseModel):
    """LLM judge output for one workflow Step (trigger-fidelity + quality)."""
    workflow_step_index: int  # 1-based
    # Fidelity: which TRIGGER raw_step (if any) this Step grounds, and how well.
    aligned_to: Optional[int] = None   # 1-based template raw_step index
    verdict: Literal["FAITHFUL", "DRIFTED", "WRONG"]
    reasoning: str
    # Quality: independent of fidelity, is the grounded text well-written?
    quality: Literal["GOOD", "ADEQUATE", "POOR"] = "ADEQUATE"
    quality_issues: list[str] = Field(default_factory=list)


class MissedTrigger(BaseModel):
    """A template raw_step classified as TRIGGER that no workflow Step grounds."""
    index: int  # 1-based
    raw_step: str


class WorkflowStepsJudgement(BaseModel):
    """LLM output covering one workflow's complete step audit in a single call."""
    raw_step_classifications: list[RawStepClassification]
    step_judgements: list[StepJudgement]
    missed_triggers: list[MissedTrigger] = Field(default_factory=list)


class StepExpectOutput(BaseModel):
    """LLM output: one step's regenerated expect.action + expect.reason."""
    step_index: int  # 1-based
    action: Optional[str] = None
    reason: Optional[str] = None


class WorkflowExpectsRegenOutput(BaseModel):
    """LLM output for regen_expects: one entry per Step, in input order."""
    step_expects: list[StepExpectOutput]


class StepVerdict(BaseModel):
    """Per-workflow-Step verdict combining deterministic health + LLM fidelity + LLM quality.

    Fidelity is grounded in TRIGGER fidelity: each workflow Step is supposed
    to be an external trigger, so the validator checks whether it grounds
    one of the template's TRIGGER raw_steps (cascade raw_steps are NOT the
    Step's job — they're covered by object behaviors).
    """
    workflow_id: str
    step_index: int       # 0-based index into Workflow.steps
    grounded_step: str
    expect_action: Optional[str] = None
    target: str = ""
    # Fidelity (LLM)
    aligned_to: Optional[int] = None   # 1-based template raw_step index this Step grounds, or None
    verdict: Literal["FAITHFUL", "DRIFTED", "WRONG"]
    reasoning: str
    # Health (deterministic, no LLM)
    health_issues: list[str] = Field(default_factory=list)
    # Quality (LLM, independent of fidelity)
    quality: Literal["GOOD", "ADEQUATE", "POOR"] = "ADEQUATE"
    quality_issues: list[str] = Field(default_factory=list)


class WorkflowValidation(BaseModel):
    """Aggregate validation result for one workflow's Steps."""
    workflow_id: str
    n_template_steps: int          # total raw_steps in template
    n_template_triggers: int = 0   # raw_steps classified as TRIGGER by the LLM
    n_workflow_steps: int
    step_verdicts: list[StepVerdict] = Field(default_factory=list)
    # Triggers in the template that no workflow Step grounds.
    missed_triggers: list[MissedTrigger] = Field(default_factory=list)
    # Classification of every raw_step (for audit & inspection).
    raw_step_classifications: list[RawStepClassification] = Field(default_factory=list)
    # Fidelity rollup (CLEAN / MILD_DRIFT / NOTABLE_DRIFT / WRONG)
    aggregate: Literal["CLEAN", "MILD_DRIFT", "NOTABLE_DRIFT", "WRONG"]
    # Health rollup: OK if every workflow Step has zero health_issues
    aggregate_health: Literal["OK", "ISSUES"] = "OK"
    # Quality rollup: worst per-step quality
    aggregate_quality: Literal["GOOD", "ADEQUATE", "POOR"] = "ADEQUATE"


# ── Object graph validation schemas (Stage 1b) ────────────────────────────────


class SingleObjectQuality(BaseModel):
    """LLM judgement for one object inside a graph judgement."""
    object_id: str
    quality: Literal["GOOD", "ADEQUATE", "POOR"]
    quality_issues: list[str] = Field(default_factory=list)


class ObjectGraphJudgement(BaseModel):
    """LLM judge output covering an entire object graph in one call."""
    objects: list[SingleObjectQuality]
    graph_quality: Literal["GOOD", "ADEQUATE", "POOR"]
    graph_issues: list[str] = Field(default_factory=list)
    reasoning: str


class ObjectVerdict(BaseModel):
    """Per-object verdict combining deterministic health + LLM quality."""
    workflow_id: str
    object_id: str
    role: str = ""
    is_entry_point: bool = False
    health_issues: list[str] = Field(default_factory=list)
    quality: Literal["GOOD", "ADEQUATE", "POOR"] = "ADEQUATE"
    quality_issues: list[str] = Field(default_factory=list)


class ObjectGraphValidation(BaseModel):
    """Aggregate validation result for one workflow's object graph."""
    workflow_id: str
    n_objects: int
    object_verdicts: list[ObjectVerdict] = Field(default_factory=list)
    graph_quality: Literal["GOOD", "ADEQUATE", "POOR"] = "ADEQUATE"
    graph_issues: list[str] = Field(default_factory=list)
    graph_reasoning: str = ""
    aggregate_health: Literal["OK", "ISSUES"] = "OK"
    aggregate_quality: Literal["GOOD", "ADEQUATE", "POOR"] = "ADEQUATE"
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0


# ── Sample (modifications + events) validation schemas (Stage 2) ──────────────


class SingleModificationJudgement(BaseModel):
    """LLM judgement for one modification."""
    mod_id: str
    quality: Literal["GOOD", "ADEQUATE", "POOR"]
    quality_issues: list[str] = Field(default_factory=list)
    type_match: Literal["YES", "NO"]            # mod_type matches intent shape?
    ambiguity_match: Literal["YES", "NO"]        # ambiguity rating fits the intent style?
    events_test_mod: Literal["YES", "PARTIAL", "NO"]  # do post_mod events actually test it?
    reasoning: str


class SampleModificationsJudgement(BaseModel):
    """LLM judge output covering all modifications in one sample."""
    modifications: list[SingleModificationJudgement]
    overall_quality: Literal["GOOD", "ADEQUATE", "POOR"]
    overall_issues: list[str] = Field(default_factory=list)
    reasoning: str


class ModificationVerdict(BaseModel):
    """Per-modification verdict combining health + quality."""
    sample_id: str
    mod_id: str
    mod_type: str
    ambiguity: str
    target: str
    when: str = ""
    intent_preview: str = ""
    health_issues: list[str] = Field(default_factory=list)
    quality: Literal["GOOD", "ADEQUATE", "POOR"] = "ADEQUATE"
    quality_issues: list[str] = Field(default_factory=list)
    type_match: Literal["YES", "NO"] = "YES"
    ambiguity_match: Literal["YES", "NO"] = "YES"
    events_test_mod: Literal["YES", "PARTIAL", "NO"] = "YES"


class SampleModificationValidation(BaseModel):
    """Aggregate validation for one Sample's modifications + events linkage."""
    sample_id: str
    workflow_id: str = ""          # the underlying workflow (Sample.sample_id)
    n_modifications: int
    n_events: int
    modification_verdicts: list[ModificationVerdict] = Field(default_factory=list)
    overall_quality: Literal["GOOD", "ADEQUATE", "POOR"] = "ADEQUATE"
    overall_issues: list[str] = Field(default_factory=list)
    overall_reasoning: str = ""
    aggregate_health: Literal["OK", "ISSUES"] = "OK"
    aggregate_quality: Literal["GOOD", "ADEQUATE", "POOR"] = "ADEQUATE"
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0

# Scenario generation schemas (LLM output before merging with instance metadata)
class Scenario(BaseModel):
    id: str
    sample_id: str
    description: str
    modifications: list[GeneratedModification]
    events: list[GeneratedEvent]

class Scenarios(BaseModel):
    scenarios: list[Scenario]


# ── Object-AGNOSTIC SPEC models (Phase 1) ─────────────────────────────────────
# These mirror the bound models below but carry NO object reference (no target /
# recipient / object_id). Phase 1 emits a WorkflowSpec; Phase 2 (bind_spec.py)
# derives the agent graph and binds these into the unchanged bound models
# (Event / Modification / Sample) so evaluate.py is unaffected.

class SpecStep(BaseModel):
    """A workflow step as an external stimulus — object-agnostic (no target)."""
    text: str
    source: str = "__external__"
    expect: Optional[EventExpect] = None

class SpecEvent(BaseModel):
    """LLM/spec event with no recipient — bound to an object_id in Phase 2."""
    id: str
    call_type: str
    source: str = "__external__"
    input: str
    when: str
    triggered_by: Optional[str] = None
    trigger_delay_minutes: float = 0.0
    trigger_delay_seconds: float = 0.0
    role: Optional[Literal["base", "pre_mod", "post_mod", "irrelevant"]] = None
    after_mod_ids: list[str] = Field(default_factory=list)
    concurrent_group: Optional[str] = None

class SpecEventWithExpect(BaseModel):
    """Spec event carrying its own expect (state-infused base events) — no recipient."""
    id: str
    call_type: str
    source: str = "__external__"
    input: str
    when: str
    role: Optional[Literal["base", "pre_mod", "post_mod", "irrelevant"]] = None
    after_mod_ids: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    triggered_by: Optional[str] = None
    trigger_delay_minutes: float = 0.0
    trigger_delay_seconds: float = 0.0
    concurrent_group: Optional[str] = None  # same label + same `when` → concurrent arrivals
    expect: Optional[EventExpect] = None

class SpecModification(BaseModel):
    """A modification as an object-agnostic intent — target bound in Phase 2.
    mod_type / ambiguity are still script-assigned."""
    id: str
    call_type: str = "send"
    source: str = "__user__"
    when: str
    intent: str
    mod_type: Optional[ModType] = None
    ambiguity: Optional[Ambiguity] = None

class SpecScenario(BaseModel):
    """Object-agnostic analogue of Scenario (Phase-1 LLM output)."""
    id: str = ""
    sample_id: str = ""
    description: str = ""
    modifications: list[SpecModification]
    events: list[SpecEventWithExpect]   # expect optional; code-built post_mod carry derived expects

class WorkflowSpecScenarios(BaseModel):
    scenarios: list[SpecScenario]

class ModTargetBinding(BaseModel):
    """Phase-2 LLM output: which object a modification addresses."""
    mod_id: str
    object_id: str

class ModTargetBindings(BaseModel):
    bindings: list[ModTargetBinding]

class WorkflowSpec(BaseModel):
    """The Phase-1 object-agnostic carrier: everything about WHAT the workflow does,
    nothing about HOW it is decomposed into objects. No `objects`, no `tools`."""
    id: str
    name: str
    domain: str
    source_type: str
    link: str = ""
    template: list[str] = Field(default_factory=list)        # abstract template steps
    grounded_steps: list[str] = Field(default_factory=list)  # grounded NL steps (no object_ids)
    seed: str = ""                                            # initial reference state the system reads (grounded)
    phrasings: list["RolePhrasing"] = Field(default_factory=list)  # carried from infuse for the mod builder
    decorations: list[str] = Field(default_factory=list)
    key: str = ""                                            # rate_limit: the key the base scenario exercised
    unit: str = ""                                           # the gated action's noun (carried for the mod)
    entities: list[str] = Field(default_factory=list)        # counter rotation members (carried for the mod)
    keys: list[str] = Field(default_factory=list)            # rate_limit key values (carried for the mod)
    irrelevant_key: str = ""                                 # entity outside the invariant (irrelevant event)
    steps: list[SpecStep] = Field(default_factory=list)      # external-stimulus steps with observable expects
    base_events: list[SpecEventWithExpect] = Field(default_factory=list)  # state-infused base scenario
    modifications: list[SpecModification] = Field(default_factory=list)
    events: list[SpecEventWithExpect] = Field(default_factory=list)  # mod/pre/post/irrelevant (code-built post_mod carry expects)
    state_constraint: Optional[StateConstraint] = None
    flagged: bool = False
    flag_reasons: list[str] = Field(default_factory=list)

class WorkflowSpecs(BaseModel):
    specs: list[WorkflowSpec]

class SpecWorkflowSteps(BaseModel):
    """LLM output for object-free step writing (Phase 1) — steps with no target."""
    steps: list[SpecStep]

class SpecBaseEventsList(BaseModel):
    """LLM output for grounding the infused base scenario — concrete entities, no recipient."""
    base_events: list[SpecEventWithExpect]


class EntityMapEntry(BaseModel):
    placeholder: str  # the abstract label, e.g. "Rep #1", "Lead #10", "SKU #1"
    value: str        # the concrete grounded value, e.g. "Maya Patel", "LF-2026-0417"


class EntityGroundingMap(BaseModel):
    """LLM output: a placeholder→concrete map for grounding the infused base scenario by
    DETERMINISTIC substitution (so expect semantics — reset/block/concurrent — are preserved)."""
    mappings: list[EntityMapEntry]


# ── Event sequence validation schemas (Stage 1e) ─────────────────────────────

class EventVerdictOutput(BaseModel):
    """LLM output: issues found for one base event."""
    event_id: str
    issues: list[str] = Field(default_factory=list)
    # issue codes: sequential_paradox | causal_orphan | expect_leak |
    #              expect_incomplete | expect_null_invalid | redundant
    issue_descriptions: list[str] = Field(default_factory=list)
    quality: Literal["GOOD", "ADEQUATE", "POOR"]

class EventSequenceJudgement(BaseModel):
    """LLM output: all event verdicts + sequence-level verdict."""
    event_verdicts: list[EventVerdictOutput]
    sequence_verdict: Literal["CLEAN", "MILD_ISSUES", "PARADOX", "INCOMPLETE"]
    sequence_issues: list[str] = Field(default_factory=list)
    reasoning: str

class EventVerdict(BaseModel):
    """Per-event verdict combining deterministic health checks + LLM quality."""
    workflow_id: str
    event_id: str
    event_input_preview: str = ""
    issues: list[str] = Field(default_factory=list)
    issue_descriptions: list[str] = Field(default_factory=list)
    quality: Literal["GOOD", "ADEQUATE", "POOR"] = "ADEQUATE"

class EventSequenceValidation(BaseModel):
    """Aggregate validation result for one workflow's base event sequence."""
    workflow_id: str
    n_base_events: int
    event_verdicts: list[EventVerdict] = Field(default_factory=list)
    sequence_verdict: Literal["CLEAN", "MILD_ISSUES", "PARADOX", "INCOMPLETE"]
    sequence_issues: list[str] = Field(default_factory=list)
    sequence_reasoning: str = ""
    aggregate_health: Literal["OK", "ISSUES"] = "OK"
    aggregate_quality: Literal["GOOD", "ADEQUATE", "POOR"] = "ADEQUATE"
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0


class SampleEventSequenceValidation(BaseModel):
    """Aggregate validation result for one Sample's full event sequence (base + mod events)."""
    sample_id: str
    workflow_id: str
    n_events: int
    event_verdicts: list[EventVerdict] = Field(default_factory=list)
    sequence_verdict: Literal["CLEAN", "MILD_ISSUES", "PARADOX", "INCOMPLETE"]
    sequence_issues: list[str] = Field(default_factory=list)
    sequence_reasoning: str = ""
    aggregate_health: Literal["OK", "ISSUES"] = "OK"
    aggregate_quality: Literal["GOOD", "ADEQUATE", "POOR"] = "ADEQUATE"
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0


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
    enable_step_retry_replan: bool = False
    step_max_retries: int = 2
    step_replan_max: int = 1
    reactive_replan_max_per_trace: int = 4


# Evaluation result schemas

class EventResult(BaseModel):
    """Result of executing a single test event."""
    event_id: str
    passed: bool
    reasoning: str
    expected: str = ""
    evidence: str = ""      # gather_evidence() output — what the judge saw
    prior_context: str = "" # _format_prior_state() snapshot before this event
    role: Optional[Literal["base", "pre_mod", "post_mod", "irrelevant"]] = None  # propagated from Event.role
    input_tokens: int = 0   # entry-agent LLM input tokens (baseline) or LNL agent tokens (lnl eval)
    output_tokens: int = 0  # entry-agent LLM output tokens
    planner_input_tokens: int = 0
    planner_output_tokens: int = 0
    executor_input_tokens: int = 0
    executor_output_tokens: int = 0
    # Total executor invocations across this event's cascade (sum of
    # ProcessingResult.executor_cycles over every object that processed a
    # message for this event). One "cycle" = one ReAct executor pass.
    # executor_retries = max(0, executor_calls - n_objects_processed).
    # Baseline eval reports 0 (no analogue).
    executor_calls: int = 0
    executor_retries: int = 0
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
    executor_calls: int = 0
    executor_retries: int = 0
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
    pass_rate_ci95: Optional[float] = None  # 95% CI half-width (t-based, across-TC variance); None for single-run evals
    # ── Per-role pass rates ────────────────────────────────────────────────────
    steps_pass_rate: Optional[float] = None        # S\d+ events mean fraction (baseline setup)
    steps_pass_rate_ci95: Optional[float] = None
    samples_completion: Optional[float] = None     # fraction of TCs where ALL step events passed
    samples_completion_ci95: Optional[float] = None
    mod_pass_rate: Optional[float] = None          # all modification events (pre+post+irrelevant combined, conclusive TCs only)
    mod_pass_rate_ci95: Optional[float] = None
    mod_pass_rate_all: Optional[float] = None      # same but including inconclusive TCs
    mod_pass_rate_all_ci95: Optional[float] = None
    pre_mod_pass_rate: Optional[float] = None      # events before modification fires (conclusive TCs only)
    pre_mod_pass_rate_ci95: Optional[float] = None
    pre_mod_pass_rate_all: Optional[float] = None  # same but including inconclusive TCs
    pre_mod_pass_rate_all_ci95: Optional[float] = None
    post_mod_pass_rate: Optional[float] = None     # events after modification fires (key signal, conclusive TCs only)
    post_mod_pass_rate_ci95: Optional[float] = None
    post_mod_pass_rate_all: Optional[float] = None # same but including inconclusive TCs
    post_mod_pass_rate_all_ci95: Optional[float] = None
    irrelevant_pass_rate: Optional[float] = None   # events unrelated to the modification (conclusive TCs only)
    irrelevant_pass_rate_ci95: Optional[float] = None
    irrelevant_pass_rate_all: Optional[float] = None  # same but including inconclusive TCs
    irrelevant_pass_rate_all_ci95: Optional[float] = None
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
    total_executor_calls: int = 0
    total_executor_retries: int = 0
    mean_executor_calls_per_event: float = 0.0
    mean_executor_retries_per_event: float = 0.0
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
