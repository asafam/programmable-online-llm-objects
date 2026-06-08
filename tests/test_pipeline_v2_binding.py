"""Offline tests for the two-phase pipeline (no API key).

Cover the object-free SPEC invariant and the pure Phase-2 binding/assembly that turns
a WorkflowSpec + agent graph into the unchanged, eval-compatible Sample.
"""
import json

from src.data import bind_spec as B
from src.data.schema import (
    Ambiguity, EventExpect, ModType, ObjectDef, ObjectGraph, Sample, SpecEvent,
    SpecEventWithExpect, SpecModification, SpecStep, StateConstraint,
    StateConstraintType, WorkflowSpec,
)


def _graph():
    return ObjectGraph(objects=[
        ObjectDef(object_id="lead-form", role="entry", behavior="forward", event_sources=["jotform form"]),
        ObjectDef(object_id="facebook-lead-ads", role="entry", behavior="forward", event_sources=["facebook lead ad"]),
        ObjectDef(object_id="lead-routing", role="router", behavior="assign leads, cap 2/day"),
        ObjectDef(object_id="reps-store", role="read", behavior="call the `reps_store_data` tool to retrieve data."),
    ])


def _spec():
    return WorkflowSpec(
        id="rr", name="RR", domain="g", source_type="x", grounded_steps=["arrive", "assign"],
        steps=[SpecStep(text="New Jotform form for Alice", source="jotform",
                        expect=EventExpect(action="recorded", reason="r"))],
        base_events=[
            SpecEventWithExpect(id="E001", call_type="send_event", input="Jotform lead L1",
                                when="W01-1T09:00", role="base", expect=EventExpect(action="assigned", reason="r")),
            SpecEventWithExpect(id="E002", call_type="send_event", input="Jotform lead L3",
                                when="W01-1T11:00", role="base", triggered_by="E001", depends_on=["E001"],
                                expect=EventExpect(action="blocked", reason="cap")),
        ],
        events=[SpecEvent(id="E001", call_type="send_event", input="Facebook lead ad for Bob",
                          when="W02-1T09:00", role="pre_mod")],
        modifications=[SpecModification(id="M001", when="W02-1T08:00", intent="cap now 3/day",
                                        mod_type=ModType.correction, ambiguity=Ambiguity.precise)],
        state_constraint=StateConstraint(type=StateConstraintType.counter, threshold="2/rep/day",
                                         description="per-rep daily cap"),
    )


def test_spec_is_object_free():
    js = _spec().model_dump_json()
    for bad in ("object_id", "recipient", "target"):
        assert bad not in js, f"spec leaked {bad}"


def test_recipient_binding_distinctive_tokens():
    entries = _graph().objects[:2]
    assert B._bind_recipient("New Jotform form submission for Alice", entries) == "lead-form"
    assert B._bind_recipient("Facebook lead ad submission for Bob", entries) == "facebook-lead-ads"
    # sole entry → trivial
    assert B._bind_recipient("anything", entries[:1]) == "lead-form"


def test_mod_target_single_business_fallback():
    g = _graph()
    business = B._business_objects(g)
    assert [o.object_id for o in business] == ["lead-routing"]
    assert B._bind_mod_target("cap now 3/day", business, None, "M001") == "lead-routing"


def test_assemble_is_eval_compatible():
    s = B.assemble_sample(_spec(), _graph())
    assert isinstance(s, Sample)
    ids = {o.object_id for o in s.objects}
    # every recipient/target resolves to a real object
    assert all(e.recipient in ids for e in s.events)
    assert all(m.target in ids for m in s.modifications)
    # base scenario present, roles carried, ids unique, references remapped
    assert sum(1 for e in s.events if e.role == "base") == 3
    assert len({e.id for e in s.events}) == len(s.events)
    sc2 = next(e for e in s.events if e.id == "SC002")
    assert sc2.triggered_by == "SC001" and sc2.depends_on == ["SC001"]
    # cumulative expect preserved through binding
    assert sc2.expect is not None and sc2.expect.action == "blocked"
    # round-trips as a Sample
    assert json.loads(s.model_dump_json())["id"] == "rr"


def test_assemble_rejects_unbindable_modification():
    """Two business objects + no mapping → mod target unresolvable → raises."""
    g = _graph()
    g.objects.append(ObjectDef(object_id="second-logic", role="other", behavior="does things"))
    try:
        B.assemble_sample(_spec(), g)
    except ValueError as e:
        assert "modification" in str(e).lower()
    else:
        raise AssertionError("expected ValueError for ambiguous mod target")


def test_llm_mapping_resolves_multi_business():
    """A provided mod_mapping (as the LLM map would produce) resolves multi-business."""
    g = _graph()
    g.objects.append(ObjectDef(object_id="second-logic", role="other", behavior="does things"))
    s = B.assemble_sample(_spec(), g, mod_mapping={"M001": "second-logic"})
    assert next(m for m in s.modifications if m.id == "M001").target == "second-logic"


def test_format_graph_for_binding():
    txt = B._format_graph_for_binding(_graph())
    assert "lead-routing" in txt and "behavior:" in txt


def test_every_event_is_timed_and_triggers_spread():
    """Data-timing guarantee: every event has a non-empty `when`; multiple trigger steps
    are spread (not all at one instant)."""
    from src.data.schema import SpecStep
    sp = _spec()
    sp.steps = [SpecStep(text="trigger A"), SpecStep(text="trigger B"), SpecStep(text="trigger C")]
    # blank one base event's `when` — must be backfilled
    sp.base_events[1].when = ""
    s = B.assemble_sample(sp, _graph())
    assert all(e.when for e in s.events), "every event must carry a non-empty when"
    trig = [e for e in s.events if e.id.startswith("S0")]   # S001.. trigger steps
    assert len({e.when for e in trig}) == len(trig), "trigger steps must have distinct times"


def test_run_tag_versions_sample_id():
    """run_tag makes the sample id unique per run, keeping sample_id for grouping."""
    s = B.assemble_sample(_spec(), _graph(), run_tag="20260608_state_timed")
    assert s.id == "rr__20260608_state_timed"
    assert s.sample_id == "rr"
    # default (no run_tag) keeps the plain id
    assert B.assemble_sample(_spec(), _graph()).id == "rr"
