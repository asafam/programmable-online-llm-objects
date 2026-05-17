"""Tests for the wait-step correlation mechanism.

The planner can emit `wait` steps so a workflow that spans multiple external
events lands on a single plan. When a later event arrives, a matcher LLM
decides whether it satisfies a pending wait — on a match, the event's
trace_id is rebound onto the absorbing plan and the wait step closes with
the event payload as its result.

These tests use MockBrain to script both the planner and the matcher
deterministically, so the correlation logic can be verified without a live LLM.
"""
import datetime
import time

import pytest

from src.lnl import (
    LLMObject,
    LLMResponse,
    Message,
    MessageType,
    MockBrain,
    ObjectDefinition,
    PeerDeclaration,
)
from src.lnl.runtime import Runtime
from src.lnl.types import ReactFinish, ReactStep


def _defn(object_id="obj", **overrides):
    return ObjectDefinition(object_id=object_id, role="A test object.", **overrides)


def _order_plan_with_wait(order_id="ORD-1"):
    """A scripted planner plan: place order → wait for shipping email → notify."""
    return {
        "goal": "Place order and finalize once shipping confirmation arrives",
        "steps": [
            {
                "id": "s1", "step_number": 1, "kind": "reason", "target": "self",
                "description": f"Record placed order {order_id}",
                "depends_on": [], "reasoning": "Need to track the order.",
            },
            {
                "id": "s2", "step_number": 2, "kind": "wait", "target": "self",
                "description": f"Wait for shipping confirmation for {order_id}",
                "depends_on": ["s1"],
                "reasoning": "Logistics confirms shipment asynchronously via email.",
                "wait_predicate": f"Shipping confirmation email referencing {order_id}",
                "wait_source": "email-gateway",
                "wait_timeout_seconds": 60.0,
            },
            {
                "id": "s3", "step_number": 3, "kind": "reason", "target": "self",
                "description": "Mark order as shipped",
                "depends_on": ["s2"], "reasoning": "Record completion.",
            },
            {
                "id": "final", "step_number": 4, "kind": "final", "target": "final",
                "description": "All done.", "depends_on": [],
                "reasoning": "Workflow complete.",
            },
        ],
    }


def _trivial_react_finish(reply="ok"):
    return ReactStep(
        thought="done",
        action="finish",
        finish=ReactFinish(reply=reply),
    )


class TestWaitCorrelation:
    def test_positive_match_absorbs_event_into_existing_plan(self):
        """First event creates plan with wait; matching second event closes
        the wait step on the SAME plan rather than starting a new one."""
        brain = MockBrain()
        # Planner: emit the order plan only once — second event must NOT replan.
        brain.script_plan(_order_plan_with_wait("ORD-1"), object_id="order-svc")
        # Two react finishes: one per processed message.
        brain.script_react(_trivial_react_finish("placed"))
        brain.script_react(_trivial_react_finish("shipped"))

        obj = LLMObject(
            _defn("order-svc"),
            brain,
            enable_planner=True,
        )

        # Event 1: order arrives (DOMAIN, fresh trace).
        m1 = Message(
            sender="__user__", recipient="order-svc",
            type=MessageType.DOMAIN,
            content="Place order ORD-1",
            id="m1", trace_id="t1",
        )
        obj.process_message(m1)

        plan = obj.plan_for("t1")
        assert plan is not None
        assert plan.status == "waiting"
        wait_step = next(s for s in plan.steps if s.kind == "wait")
        assert wait_step.status == "dispatched"
        assert obj._pending_waits and obj._pending_waits[0]["trace_id"] == "t1"

        # Event 2: the matching shipping email (EVENT, different trace).
        brain.script_wait_match("t1:1", reasoning="matched on ORD-1")
        m2 = Message(
            sender="email-gateway", recipient="order-svc",
            type=MessageType.EVENT,
            content="Subject: Shipped! Your order ORD-1 has shipped.",
            id="m2", trace_id="t2",
        )
        result = obj.process_message(m2)

        # The event's trace was rebound onto the absorbing plan.
        assert result.source_trace_id == "t1"
        # Only one plan ever existed.
        assert len(obj.active_plans) <= 1
        # Either still active or auto-closed (depends on whether s3 also
        # closed) — in both cases the absorbing plan must resolve from t2.
        resolved = obj.plan_for("t2")
        if resolved is not None:
            # Active plan: wait step must be done with the event content.
            assert resolved.trace_id == "t1"
            assert "t2" in resolved.additional_trace_ids
            ws = next(s for s in resolved.steps if s.kind == "wait")
            assert ws.status == "done"
            assert "ORD-1" in (ws.result or "")
            assert ws.matched_event_id == "m2"
        else:
            # Plan auto-closed after the wait step + reason steps reached terminal.
            archived = next((p for p in obj.completed_plans if p.trace_id == "t1"), None)
            assert archived is not None
            ws = next(s for s in archived.steps if s.kind == "wait")
            assert ws.status == "done"

        # Registry must be cleared after the match.
        assert not obj._pending_waits

    def test_negative_match_starts_fresh_plan(self):
        """When the matcher returns null, the event is not absorbed; the
        existing waiting plan stays in 'waiting' and a separate code path
        handles the event normally (no replan in this test because the
        planner script is empty)."""
        brain = MockBrain()
        brain.script_plan(_order_plan_with_wait("ORD-1"), object_id="order-svc")
        brain.script_react(_trivial_react_finish("placed"))
        brain.script_react(_trivial_react_finish("ignored"))

        obj = LLMObject(_defn("order-svc"), brain, enable_planner=True)

        # Set up the waiting plan.
        obj.process_message(Message(
            sender="__user__", recipient="order-svc",
            type=MessageType.DOMAIN,
            content="Place order ORD-1",
            id="m1", trace_id="t1",
        ))
        assert obj.plan_for("t1").status == "waiting"

        # Unrelated email — matcher returns null.
        brain.script_wait_match(None, reasoning="different order id")
        m2 = Message(
            sender="email-gateway", recipient="order-svc",
            type=MessageType.EVENT,
            content="Subject: Shipped! Your order ORD-999 has shipped.",
            id="m2", trace_id="t2",
        )
        result = obj.process_message(m2)

        # Event 2 keeps its own trace_id — no rebind happened.
        assert result.source_trace_id == "t2"
        # Original plan still waiting; wait step still dispatched.
        plan = obj.plan_for("t1")
        assert plan is not None
        assert plan.status == "waiting"
        wait_step = next(s for s in plan.steps if s.kind == "wait")
        assert wait_step.status == "dispatched"
        # Registry still holds the wait.
        assert any(w["trace_id"] == "t1" for w in obj._pending_waits)

    def test_wait_timeout_fails_step_and_closes_plan(self):
        """A wait step that exceeds wait_timeout_seconds fails on the next
        stale-plan sweep; the plan moves to 'failed'."""
        brain = MockBrain()
        # Plan with a very short timeout.
        plan_dict = _order_plan_with_wait("ORD-1")
        plan_dict["steps"][1]["wait_timeout_seconds"] = 0.05
        brain.script_plan(plan_dict, object_id="order-svc")
        brain.script_react(_trivial_react_finish("placed"))
        brain.script_react(_trivial_react_finish("processed"))

        obj = LLMObject(
            _defn("order-svc"), brain,
            enable_planner=True,
            stale_plan_seconds=3600.0,   # so the global threshold can't trip
        )

        obj.process_message(Message(
            sender="__user__", recipient="order-svc",
            type=MessageType.DOMAIN,
            content="Place order ORD-1",
            id="m1", trace_id="t1",
        ))
        assert obj.plan_for("t1").status == "waiting"

        # Wait past the per-step timeout, then trigger any sweep-bearing call.
        time.sleep(0.1)
        obj._sweep_stale_plans()

        # Plan should be gone from active and archived as failed.
        assert obj.plan_for("t1") is None
        archived = next((p for p in obj.completed_plans if p.trace_id == "t1"), None)
        assert archived is not None
        assert archived.status == "failed"
        ws = next(s for s in archived.steps if s.kind == "wait")
        assert ws.status == "failed"
        assert not obj._pending_waits

    def test_executor_can_add_wait_step_via_plan_update(self):
        """The executor (inside the ReAct + evaluator loop) can amend the
        active plan with a `wait` step via plan_update.add_steps. The wait
        fields (predicate, source, timeout) must be plumbed through, and
        the wait must register in _pending_waits after the turn closes."""
        from src.lnl.types import PlanUpdate, Plan, PlanStep

        brain = MockBrain()
        obj = LLMObject(_defn("approval-policy"), brain, enable_planner=True)

        # Start with a minimal plan (no wait) — simulate what the planner
        # would emit for the first-stage handlers.
        obj._active_plans["t1"] = Plan(
            goal="Handle document submission",
            steps=[
                PlanStep(id="s1", kind="reason", description="Record pending approval", status="done"),
            ],
            status="active",
            trace_id="t1",
        )

        # Apply a plan_update that the executor would emit on realizing the
        # behavior has a second-stage approval trigger to wait for.
        upd = PlanUpdate(add_steps=[{
            "id": "s2",
            "kind": "wait",
            "description": "Wait for approver's decision on DOC-2024-1118-002",
            "depends_on": ["s1"],
            "wait_predicate": "Approval decision (approve or reject) for document DOC-2024-1118-002",
            "wait_source": "approval-action-receiver",
            "wait_timeout_seconds": 604800,
        }])
        obj._apply_plan_update(upd, "t1")

        plan = obj.plan_for("t1")
        added = next(s for s in plan.steps if s.id == "s2")
        assert added.kind == "wait"
        assert added.status == "planned"
        # The bug being fixed: these fields must be carried through.
        assert added.wait_predicate == "Approval decision (approve or reject) for document DOC-2024-1118-002"
        assert added.wait_source == "approval-action-receiver"
        assert added.wait_timeout_seconds == 604800.0

        # Trigger the dispatch sweep that runs at end of process_message —
        # the new wait must register in _pending_waits and the plan status
        # must flip to "waiting".
        obj._dispatch_pending_waits("t1")
        assert plan.status == "waiting"
        assert any(w["trace_id"] == "t1" and w["step_index"] == 1 for w in obj._pending_waits)

    def test_registry_captures_originating_event_and_prior_results(self):
        """The wait-registry entry must carry the triggering message + any
        completed prior step results, so the matcher can correlate against
        identifiers that weren't in the planner's predicate (e.g. ids
        produced by a tool return AFTER plan time)."""
        from src.lnl.types import Plan, PlanStep

        brain = MockBrain()

        obj = LLMObject(_defn("order-svc"), brain, enable_planner=True)

        # Hand-craft a plan with one completed `tool` step + one pending
        # `wait`, so we can deterministically exercise the snapshot logic.
        plan = Plan(
            goal="Place order and wait for shipping",
            steps=[
                PlanStep(
                    id="s1", kind="tool", description="place_order",
                    target="place_order", status="done",
                    result={"order_id": "ORD-42", "total": 99.5},
                    result_kind="tool",
                ),
                PlanStep(
                    id="s2", kind="wait", description="Wait for shipping email",
                    target="self", status="planned",
                    wait_predicate="shipping confirmation for the placed order",
                    wait_source="email-gateway",
                ),
            ],
            status="active",
            trace_id="t1",
        )
        obj._active_plans["t1"] = plan

        # Pretend the inbound trigger that produced this plan looked like:
        trigger = Message(
            sender="storefront",
            recipient="order-svc",
            type=MessageType.DOMAIN,
            content="New order from Acme Corp: please place an order for 3x Widget.",
            id="m0",
            trace_id="t1",
        )
        obj._dispatch_pending_waits("t1", originating_message=trigger)

        assert len(obj._pending_waits) == 1
        entry = obj._pending_waits[0]
        assert entry["originating_sender"] == "storefront"
        assert "Acme Corp" in entry["originating_content"]
        # Prior tool result with the ORD-42 identifier is captured for the matcher.
        assert any("ORD-42" in (r.get("summary") or "") for r in entry["prior_step_results"])

        # Verify the rendered prompt actually surfaces both pieces of context.
        from src.lnl.brain import build_wait_matcher_prompt
        prompt = build_wait_matcher_prompt(
            "order-svc",
            Message(
                sender="email-gateway", recipient="order-svc",
                type=MessageType.EVENT,
                content="Your order ORD-42 has shipped via FedEx.",
                id="m_inbound", trace_id="t_inbound",
            ),
            [entry],
        )
        assert "ORD-42" in prompt
        assert "Acme Corp" in prompt
        assert "originating_event:" in prompt
        assert "prior_step_results:" in prompt

    def test_plan_for_resolves_secondary_trace(self):
        """After a positive match, plan_for(original_trace_id) returns the
        absorbing plan via additional_trace_ids fallback."""
        brain = MockBrain()
        brain.script_plan(_order_plan_with_wait("ORD-7"), object_id="order-svc")
        brain.script_react(_trivial_react_finish("placed"))
        brain.script_react(_trivial_react_finish("shipped"))

        obj = LLMObject(_defn("order-svc"), brain, enable_planner=True)

        obj.process_message(Message(
            sender="__user__", recipient="order-svc",
            type=MessageType.DOMAIN,
            content="Place order ORD-7",
            id="m1", trace_id="tA",
        ))
        brain.script_wait_match("tA:1", reasoning="ORD-7 match")
        obj.process_message(Message(
            sender="email-gateway", recipient="order-svc",
            type=MessageType.EVENT,
            content="Order ORD-7 shipped.",
            id="m2", trace_id="tB",
        ))

        # If the absorbing plan is still active, plan_for("tB") must resolve to
        # it via the additional_trace_ids fallback.
        resolved = obj.plan_for("tB")
        if resolved is not None:
            assert resolved.trace_id == "tA"
            assert "tB" in resolved.additional_trace_ids
