"""Unit tests for the Custodian / shared-invariant checks in the stage-1b
object-graph validator. No API key required — these exercise only the
deterministic helpers (`_custodian_graph_issues`, `_is_custodian`, `_step_text`).

See docs/SHARED_STATE_DESIGN.md for the pattern these checks enforce.
"""
from types import SimpleNamespace

from src.data.validate_workflow_objects import (
    _custodian_graph_issues,
    _is_custodian,
    _step_text,
)


def _obj(object_id, role="", behavior="", peers=(), is_custodian=False):
    return SimpleNamespace(
        object_id=object_id,
        role=role,
        behavior=behavior,
        peers=[SimpleNamespace(object_id=p, relationship="") for p in peers],
        is_custodian=is_custodian,
    )


def _wf(steps, objects):
    # Workflow.steps are plain strings in the canonical schema.
    return SimpleNamespace(steps=list(steps), objects=list(objects))


def test_invariant_without_custodian_is_flagged():
    wf = _wf(
        ["Assign leads round-robin; no rep may receive more than 2 leads per day."],
        [_obj("lead-assignment", role="Assigns incoming leads.")],
    )
    issues = _custodian_graph_issues(wf)
    assert len(issues) == 1
    assert "no single-writer Custodian" in issues[0]


def test_invariant_with_reachable_custodian_is_clean():
    wf = _wf(
        ["Assign leads round-robin; no rep may receive more than 2 leads per day."],
        [
            _obj("lead-assignment", role="Assigns leads.", peers=["lead-desk"]),
            _obj("lead-desk", role="Single-writer owner of the per-rep daily counts and queue."),
        ],
    )
    assert _custodian_graph_issues(wf) == []


def test_unreachable_custodian_is_flagged():
    wf = _wf(
        ["The cumulative approved discount this quarter cannot exceed $50K."],
        [
            _obj("approval-policy", role="Approves quotes."),  # never peers to the custodian
            _obj("discount-budget", role="Single-writer owner of the running discount total."),
        ],
    )
    issues = _custodian_graph_issues(wf)
    assert any("no inbound peers" in i for i in issues)


def test_no_invariant_is_clean():
    wf = _wf(
        ["Forward the new ticket to triage for classification."],
        [_obj("triage", role="Classifies tickets.")],
    )
    assert _custodian_graph_issues(wf) == []


def test_bare_field_noun_does_not_false_positive():
    # "budget" as a captured form field must NOT trip the invariant detector.
    wf = _wf(
        ["Capture the project objectives, target audience, budget, and timeline."],
        [_obj("intake", role="Captures form submissions.")],
    )
    assert _custodian_graph_issues(wf) == []


def test_is_custodian_flag_is_authoritative():
    # The explicit categorical flag decides custodian status, regardless of role text.
    assert _is_custodian(_obj("x", role="anything at all", is_custodian=True))
    assert not _is_custodian(_obj("x", role="Approves quotes and notifies Slack."))


def test_is_custodian_legacy_fallback_is_precise():
    # Flag-less (legacy) data falls back to the MANDATED role prefix only — deterministic.
    assert _is_custodian(_obj("x", role="Single-writer owner of the quota."))
    # A loose mention of ownership is NOT a custodian (no over-firing).
    assert not _is_custodian(_obj("x", role="It owns the shared budget pool."))
    # A behavior that merely references a custodian must not trip detection.
    assert not _is_custodian(_obj("x", role="Routes leads.", behavior="Forward each lead to the custodian."))


def test_flagged_custodian_with_invariant_is_clean():
    wf = _wf(
        ["Assign leads round-robin; no rep may receive more than 2 leads per day."],
        [
            _obj("lead-routing", role="Assigns leads.", peers=["lead-desk"]),
            _obj("lead-desk", role="owns the queue", peers=(), is_custodian=True),
        ],
    )
    assert _custodian_graph_issues(wf) == []


def test_step_text_handles_str_and_object():
    assert _step_text("a plain step") == "a plain step"
    assert _step_text(SimpleNamespace(text="obj step", target="t")) == "obj step"
    assert _step_text(SimpleNamespace(target="t")) == ""
