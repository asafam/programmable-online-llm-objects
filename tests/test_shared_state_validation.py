"""Unit tests for the shared-state owner / shared-invariant checks in the stage-1b
object-graph validator. No API key required — these exercise only the
deterministic helpers (`_shared_state_graph_issues`, `_owns_shared_state`, `_step_text`).

See docs/SHARED_STATE_DESIGN.md for the pattern these checks enforce.
"""
from types import SimpleNamespace

from src.data.validate_workflow_objects import (
    _shared_state_graph_issues,
    _owns_shared_state,
    _step_text,
)


def _obj(object_id, role="", behavior="", peers=(), owns_shared_state=False):
    return SimpleNamespace(
        object_id=object_id,
        role=role,
        behavior=behavior,
        peers=[SimpleNamespace(object_id=p, relationship="") for p in peers],
        owns_shared_state=owns_shared_state,
    )


def _wf(steps, objects):
    # Workflow.steps are plain strings in the canonical schema.
    return SimpleNamespace(steps=list(steps), objects=list(objects))


def test_invariant_without_shared_state_owner_is_flagged():
    wf = _wf(
        ["Assign leads round-robin; no rep may receive more than 2 leads per day."],
        [_obj("lead-assignment", role="Assigns incoming leads.")],
    )
    issues = _shared_state_graph_issues(wf)
    assert len(issues) == 1
    assert "no single-writer shared-state owner" in issues[0]


def test_invariant_with_reachable_shared_state_owner_is_clean():
    wf = _wf(
        ["Assign leads round-robin; no rep may receive more than 2 leads per day."],
        [
            _obj("lead-assignment", role="Assigns leads.", peers=["lead-desk"]),
            _obj("lead-desk", role="Single-writer owner of the per-rep daily counts and queue."),
        ],
    )
    assert _shared_state_graph_issues(wf) == []


def test_unreachable_shared_state_owner_is_flagged():
    wf = _wf(
        ["The cumulative approved discount this quarter cannot exceed $50K."],
        [
            _obj("approval-policy", role="Approves quotes."),  # never peers to the shared-state owner
            _obj("discount-budget", role="Single-writer owner of the running discount total."),
        ],
    )
    issues = _shared_state_graph_issues(wf)
    assert any("no inbound peers" in i for i in issues)


def test_no_invariant_is_clean():
    wf = _wf(
        ["Forward the new ticket to triage for classification."],
        [_obj("triage", role="Classifies tickets.")],
    )
    assert _shared_state_graph_issues(wf) == []


def test_bare_field_noun_does_not_false_positive():
    # "budget" as a captured form field must NOT trip the invariant detector.
    wf = _wf(
        ["Capture the project objectives, target audience, budget, and timeline."],
        [_obj("intake", role="Captures form submissions.")],
    )
    assert _shared_state_graph_issues(wf) == []


def test_owns_shared_state_flag_is_authoritative():
    # The explicit categorical flag decides shared-state owner status, regardless of role text.
    assert _owns_shared_state(_obj("x", role="anything at all", owns_shared_state=True))
    assert not _owns_shared_state(_obj("x", role="Approves quotes and notifies Slack."))


def test_owns_shared_state_legacy_fallback_is_precise():
    # Flag-less (legacy) data falls back to the MANDATED role prefix only — deterministic.
    assert _owns_shared_state(_obj("x", role="Single-writer owner of the quota."))
    # A loose mention of ownership is NOT a shared-state owner (no over-firing).
    assert not _owns_shared_state(_obj("x", role="It owns the shared budget pool."))
    # A behavior that merely references a shared-state owner must not trip detection.
    assert not _owns_shared_state(_obj("x", role="Routes leads.", behavior="Forward each lead to the shared-state owner."))


def test_flagged_shared_state_owner_with_invariant_is_clean():
    wf = _wf(
        ["Assign leads round-robin; no rep may receive more than 2 leads per day."],
        [
            _obj("lead-routing", role="Assigns leads.", peers=["lead-desk"]),
            _obj("lead-desk", role="owns the queue", peers=(), owns_shared_state=True),
        ],
    )
    assert _shared_state_graph_issues(wf) == []


def test_step_text_handles_str_and_object():
    assert _step_text("a plain step") == "a plain step"
    assert _step_text(SimpleNamespace(text="obj step", target="t")) == "obj step"
    assert _step_text(SimpleNamespace(target="t")) == ""
