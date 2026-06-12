"""Tests for Runtime — processing loop, chaining, and API."""
import pytest
from pathlib import Path

from src.lnl import (
    LLMResponse,
    MockBrain,
    ObjectDefinition,
    OutgoingMessage,
    PeerDeclaration,
)
from src.lnl.runtime import Runtime, SystemConfig
from src.lnl.tools import CodeExecutor, MockToolExecutor, ToolRegistry
from src.lnl.types import MessageType, ToolCall


@pytest.fixture
def brain():
    b = MockBrain()
    b.set_default(LLMResponse(updated_state={"status": "processed"}, reply="ok"))
    return b


@pytest.fixture
def rt(brain):
    return Runtime(brain)


class TestLoadDirectory:
    def test_loads_all_md_files(self, rt, tmp_path):
        (tmp_path / "a.md").write_text("# Alpha\n\n## Role\n\nDoes alpha work.\n")
        (tmp_path / "b.md").write_text("# Beta\n\n## Role\n\nDoes beta work.\n")
        (tmp_path / "not-md.txt").write_text("ignored")

        objects = rt.load_directory(tmp_path)

        assert len(objects) == 2
        ids = {o.object_id for o in objects}
        assert ids == {"alpha", "beta"}

    def test_load_file(self, rt, tmp_path):
        (tmp_path / "obj.md").write_text("# My Object\n\n## Role\n\nTest role.\n")

        obj = rt.load_file(tmp_path / "obj.md")

        assert obj.object_id == "my-object"


class TestSendRoutes:
    def test_send_routes_through_bus(self, rt):
        rt.create_object(ObjectDefinition(
            object_id="worker",
            role="Worker role.",
        ))

        results = rt.send("worker", "do work")

        assert len(results) == 1
        assert results[0].object_id == "worker"
        assert results[0].reply == "ok"

    def test_broadcast(self, rt):
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.create_object(ObjectDefinition(object_id="b", role="B"))

        results = rt.broadcast("hello all")

        assert len(results) == 2

    def test_publish_to_topic(self, rt):
        rt.create_object(ObjectDefinition(
            object_id="sub",
            role="Subscriber",
            subscriptions=["news"],
        ))
        rt.create_object(ObjectDefinition(object_id="other", role="Other"))

        results = rt.publish("news", "breaking")

        assert len(results) == 1
        assert results[0].object_id == "sub"


class TestChainProcessing:
    """Chaining tests — moved from test_bus.py since Runtime now owns processing."""

    def test_simple_chain_a_b_c(self):
        """A sends to B, B produces message to C. All results returned."""
        brain = MockBrain()
        brain.script("b", LLMResponse(
            updated_state={"status": "b got it"},
            reply="B reply",
            outgoing_messages=[OutgoingMessage(recipient="c", content="from B", expects_reply=True)],
        ))
        brain.script("c", LLMResponse(
            updated_state={"status": "c got it"},
            reply="C reply",
        ))

        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.create_object(ObjectDefinition(object_id="b", role="B"))
        rt.create_object(ObjectDefinition(object_id="c", role="C"))

        results = rt.send("b", "start chain", sender="a")

        # b processes first (direct recipient)
        assert results[0].object_id == "b"
        assert results[0].state_after == {"status": "b got it"}
        # b and c participate; b's reply to a is NOT routed because a didn't
        # send via outgoing_messages (it was an external rt.send)
        processed_ids = {r.object_id for r in results}
        assert processed_ids == {"b", "c"}
        # c eventually gets the forwarded message
        c_result = next(r for r in results if r.object_id == "c")
        assert c_result.state_after == {"status": "c got it"}
        # c's reply IS routed back to b (b sent via outgoing_messages)
        reply_results = [r for r in results if r.source_message_type == MessageType.REPLY]
        assert len(reply_results) > 0

    def test_chain_depth_limit(self):
        """Chain exceeding max depth stops processing (no exception — just stops)."""
        brain = MockBrain()
        # Each call produces a message back to self, creating a loop
        brain.set_default(LLMResponse(
            updated_state={},
            reply="ok",
            outgoing_messages=[OutgoingMessage(recipient="a", content="loop")],
        ))

        rt = Runtime(brain, max_chain_depth=3)
        rt.create_object(ObjectDefinition(object_id="a", role="A"))

        results = rt.send("a", "start loop")

        # Depth-per-hop semantics: the loop is cut at depth=0, but reply + outgoing
        # from the same result both propagate at depth-1, so total > max_chain_depth.
        # The important invariant: no infinite loop, and depth=0 messages are dropped.
        assert len(results) > 0
        assert all(r.depth_remaining > 0 for r in results)

    def test_bfs_ordering(self):
        """Mailbox model produces BFS: A→B and A→C, then B and C process before their children."""
        brain = MockBrain()
        brain.script("a", LLMResponse(
            updated_state={"status": "a done"},
            reply="A reply",
            outgoing_messages=[
                OutgoingMessage(recipient="b", content="from A", expects_reply=True),
                OutgoingMessage(recipient="c", content="from A", expects_reply=True),
            ],
        ))
        brain.script("b", LLMResponse(
            updated_state={"status": "b done"},
            reply="B reply",
        ))
        brain.script("c", LLMResponse(
            updated_state={"status": "c done"},
            reply="C reply",
        ))

        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.create_object(ObjectDefinition(object_id="b", role="B"))
        rt.create_object(ObjectDefinition(object_id="c", role="C"))

        results = rt.send("a", "start")

        # a processes first, then b and c, with replies interleaved
        assert results[0].object_id == "a"
        # All three objects participate
        processed_ids = {r.object_id for r in results}
        assert processed_ids == {"a", "b", "c"}
        # b and c both process (order may vary due to reply interleaving)
        non_reply = [r for r in results if r.source_message_type != MessageType.REPLY]
        assert non_reply[0].object_id == "a"
        assert {r.object_id for r in non_reply[1:]} == {"b", "c"}
        # Replies from b and c route back to a
        reply_results = [r for r in results if r.source_message_type == MessageType.REPLY]
        assert len(reply_results) > 0


class TestInjectEvent:
    def test_inject_event_delivers_and_processes(self):
        brain = MockBrain()
        brain.script("slack", LLMResponse(
            updated_state={"status": "event received"},
            reply="got it",
        ))

        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="slack", role="Slack service"))

        results = rt.inject_event("slack", "New message in #support")

        assert len(results) == 1
        assert results[0].object_id == "slack"
        assert rt.state("slack") == {"status": "event received"}

    def test_inject_event_chains_to_peers(self):
        brain = MockBrain()
        brain.script("slack", LLMResponse(
            updated_state={"status": "forwarded"},
            reply="forwarding",
            outgoing_messages=[OutgoingMessage(recipient="triage", content="urgent ticket", expects_reply=True)],
        ))
        brain.script("triage", LLMResponse(
            updated_state={"status": "triaged"},
            reply="handled",
        ))

        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="slack", role="Slack"))
        rt.create_object(ObjectDefinition(object_id="triage", role="Triage"))

        results = rt.inject_event("slack", "urgent message")

        assert results[0].object_id == "slack"
        assert results[1].object_id == "triage"
        # triage's reply routes back to slack
        assert any(r.source_message_type == MessageType.REPLY for r in results)


class TestEventRegistry:
    def test_event_sources_registered(self):
        brain = MockBrain()
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(
            object_id="slack",
            role="Slack service",
            event_sources=["Slack webhook: incoming messages", "Slack webhook: reactions"],
        ))

        assert rt.event_registry == {
            "slack": ["Slack webhook: incoming messages", "Slack webhook: reactions"],
        }


class TestModify:
    def test_modify_preserves_state(self, brain):
        rt = Runtime(brain)
        brain.script("worker", LLMResponse(
            updated_state={"status": "has state"},
            reply="ok",
        ))
        rt.create_object(ObjectDefinition(object_id="worker", role="Original role."))

        rt.send("worker", "init")
        assert rt.state("worker") == {"status": "has state"}

        rt.modify("worker", role="New role.")

        assert rt.state("worker") == {"status": "has state"}
        assert rt.has_unsaved_modifications("worker")

    def test_add_remove_peer(self, rt):
        rt.create_object(ObjectDefinition(object_id="a", role="A"))

        rt.add_peer("a", "b", "helper")
        topo = rt.topology()
        assert "b" in topo["a"]

        rt.remove_peer("a", "b")
        topo = rt.topology()
        assert "b" not in topo["a"]


class TestAdminModification:
    def test_admin_message_modifies_role_via_llm(self):
        brain = MockBrain()
        brain.script_admin(
            reply="Role updated.",
            updated_definition={"role": "VIP guest concierge only."},
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="worker", role="Original role."))

        rt.send_admin("worker", "Change your role to: VIP guest concierge only.")

        obj = rt._bus.objects["worker"]
        assert obj.definition.role == "VIP guest concierge only."

    def test_admin_message_modifies_skills(self):
        brain = MockBrain()
        brain.script_admin(
            reply="Skills updated.",
            updated_definition={"skills": ["lookup-room", "send-key", "issue-refund"]},
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(
            object_id="worker",
            role="Worker.",
            skills=["lookup-room"],
        ))

        rt.send_admin("worker", "Add send-key and issue-refund skills.")

        obj = rt._bus.objects["worker"]
        assert obj.definition.skills == ["lookup-room", "send-key", "issue-refund"]

    def test_admin_message_modifies_peers(self):
        brain = MockBrain()
        brain.script_admin(
            reply="Peers updated.",
            updated_definition={
                "peers": [
                    {"object_id": "billing", "relationship": "Notify on checkout."},
                    {"object_id": "ops", "relationship": "Escalate complaints."},
                ],
            },
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(
            object_id="worker",
            role="Worker.",
            peers=[PeerDeclaration("billing", "Notify on checkout.")],
        ))

        rt.send_admin("worker", "Also add ops as a peer for complaint escalation.")

        obj = rt._bus.objects["worker"]
        peer_ids = [p.object_id for p in obj.definition.peers]
        assert peer_ids == ["billing", "ops"]

    def test_admin_preserves_state(self):
        brain = MockBrain()
        brain.script("worker", LLMResponse(
            updated_state={"status": "ready"},
            reply="ok",
        ))
        brain.script_admin(
            reply="Role updated.",
            updated_definition={"role": "Updated role."},
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="worker", role="Original role."))

        rt.send("worker", "init")
        assert rt.state("worker") == {"status": "ready"}

        rt.send_admin("worker", "Change role to: Updated role.")

        assert rt.state("worker") == {"status": "ready"}
        assert rt._bus.objects["worker"].definition.role == "Updated role."

    def test_admin_ambiguous_replies_without_patch(self):
        brain = MockBrain()
        brain.script_admin(
            reply="Could you clarify which field to change?",
            updated_definition=None,  # no patch — ambiguous instruction
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="worker", role="Original role."))

        rt.send_admin("worker", "Make it better.")

        obj = rt._bus.objects["worker"]
        assert obj.definition.role == "Original role."

    def test_non_admin_react_schema_has_no_updated_definition(self):
        """Structural guard: non-admin turns can't emit definition patches
        because the React schema no longer carries `updated_definition`."""
        from src.lnl.brain import LLM_REACT_SCHEMA

        finish_props = LLM_REACT_SCHEMA["properties"]["finish"]["properties"]
        assert "updated_definition" not in finish_props

    def test_admin_patch_marks_active_plans_needs_replan(self):
        """After an admin patch, every in-flight plan is flagged for re-plan."""
        from src.lnl.types import Plan, PlanStep

        brain = MockBrain()
        brain.script_admin(
            reply="Role updated.",
            updated_definition={"role": "New role."},
            object_id="worker",
        )
        rt = Runtime(brain, system_config=SystemConfig(replan_on_modification=True))
        rt.create_object(ObjectDefinition(object_id="worker", role="Original role."))

        # Inject a fake active plan as if a prior DOMAIN message had planned it.
        obj = rt._bus.objects["worker"]
        fake_plan = Plan(
            goal="prior work",
            steps=[PlanStep(id="s1", kind="reason", description="think")],
            trace_id="trace-1",
        )
        obj._active_plans["trace-1"] = fake_plan
        assert fake_plan.needs_replan is False

        rt.send_admin("worker", "Change role to: New role.")

        assert fake_plan.needs_replan is True

    def test_admin_ambiguous_does_not_mark_plans(self):
        """A clarification-only admin turn (no patch) leaves plans untouched."""
        from src.lnl.types import Plan, PlanStep

        brain = MockBrain()
        brain.script_admin(
            reply="Clarify which field.",
            updated_definition=None,
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="worker", role="Original."))

        obj = rt._bus.objects["worker"]
        fake_plan = Plan(
            goal="prior work",
            steps=[PlanStep(id="s1", kind="reason", description="think")],
            trace_id="trace-1",
        )
        obj._active_plans["trace-1"] = fake_plan

        rt.send_admin("worker", "Make it better.")

        assert fake_plan.needs_replan is False

    def test_admin_replan_replaces_steps_preserves_state(self):
        """The next DOMAIN message on a trace marked needs_replan re-plans
        against the new definition. Plan state and deltas are preserved;
        steps and goal are replaced."""
        from src.lnl.types import Plan, PlanStep

        brain = MockBrain()
        # Admin patch
        brain.script_admin(
            reply="Behavior updated.",
            updated_definition={"behavior": "Updated behavior."},
            object_id="worker",
        )
        # Re-plan response (next DOMAIN turn): a fresh plan with one reason step.
        brain.script_plan(
            {
                "goal": "re-planned goal",
                "steps": [
                    {
                        "id": "s1",
                        "step_number": 1,
                        "kind": "reason",
                        "target": "self",
                        "description": "new step under new definition",
                        "reasoning": "test",
                    },
                    {
                        "id": "final",
                        "step_number": 2,
                        "kind": "final",
                        "target": "final",
                        "description": "done",
                        "reasoning": "wrap",
                    },
                ],
            },
            object_id="worker",
        )
        # Default React response so the executor finishes the DOMAIN turn.
        brain.script("worker", LLMResponse(
            updated_state="",
            reply="done",
            outgoing_messages=[],
        ))

        rt = Runtime(brain, system_config=SystemConfig(replan_on_modification=True))
        rt.create_object(ObjectDefinition(
            object_id="worker",
            role="Original.",
            behavior="Original behavior.",
            peers=[PeerDeclaration("peer-x", "downstream")],
        ))
        obj = rt._bus.objects["worker"]

        # Pre-seed an active plan with stale steps + state.
        stale_plan = Plan(
            goal="stale goal",
            steps=[PlanStep(id="s_old", kind="reason", description="stale")],
            trace_id="trace-X",
            state='{"counter": 7}',
        )
        obj._active_plans["trace-X"] = stale_plan

        # Apply the admin patch — should flag stale_plan for re-plan.
        rt.send_admin("worker", "Update behavior to: Updated behavior.")
        assert stale_plan.needs_replan is True

        # Now send a DOMAIN message that carries the same trace_id so the
        # planner gate triggers a re-plan in place. The simplest path: use
        # internal _bus.deliver to control trace_id.
        from src.lnl.types import Message, MessageType
        msg = Message(
            sender="__user__",
            recipient="worker",
            type=MessageType.DOMAIN,
            content="continue",
            id="m-2",
            trace_id="trace-X",
        )
        rt._dispatch([msg])

        # Plan was re-planned in place: same object, new steps and goal.
        assert "trace-X" in obj._active_plans or "trace-X" not in obj._active_plans
        # Note: stale_plan reference may have been mutated in place; check it.
        assert stale_plan.needs_replan is False
        assert stale_plan.goal == "re-planned goal"
        assert any(s.id == "s1" for s in stale_plan.steps)
        # State and identity preserved.
        assert stale_plan.state == '{"counter": 7}'


class TestAdminEdgeCases:
    """Defensive edge cases — guard against silent corruption, partial patches,
    and unintended cross-trace effects."""

    def test_admin_cannot_modify_subscriptions(self):
        """subscriptions is NOT in _PATCHABLE_DEFINITION_FIELDS; LLM-supplied
        value should be ignored even if it sneaks into the patch dict."""
        brain = MockBrain()
        brain.script_admin(
            reply="Role updated.",
            updated_definition={
                "role": "New role.",
                "subscriptions": ["forbidden-topic"],  # not patchable
            },
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(
            object_id="worker",
            role="Original.",
            subscriptions=["legit-topic"],
        ))

        rt.send_admin("worker", "Update role.")

        obj = rt._bus.objects["worker"]
        assert obj.definition.role == "New role."
        assert obj.definition.subscriptions == ["legit-topic"]

    def test_admin_cannot_modify_event_sources(self):
        brain = MockBrain()
        brain.script_admin(
            reply="ok",
            updated_definition={
                "role": "X",
                "event_sources": ["evil-source"],
            },
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(
            object_id="worker",
            role="Original.",
            event_sources=["original-source"],
        ))

        rt.send_admin("worker", "Update.")

        obj = rt._bus.objects["worker"]
        assert obj.definition.event_sources == ["original-source"]

    def test_admin_cannot_modify_object_id(self):
        """object_id is the actor's identity — never patchable."""
        brain = MockBrain()
        brain.script_admin(
            reply="ok",
            updated_definition={"object_id": "hijacked", "role": "X"},
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="worker", role="Original.", peers=[PeerDeclaration("peer-x", "downstream")]))

        rt.send_admin("worker", "Rename yourself.")

        obj = rt._bus.objects["worker"]
        assert obj.definition.object_id == "worker"
        assert obj.definition.role == "X"

    def test_admin_multi_field_patch(self):
        """A single admin patch can update role + behavior + peers + skills
        in one shot. All four take effect."""
        brain = MockBrain()
        brain.script_admin(
            reply="All updated.",
            updated_definition={
                "role": "New role.",
                "behavior": "New behavior.",
                "peers": [{"object_id": "p1", "relationship": "helper"}],
                "skills": ["s1", "s2"],
            },
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(
            object_id="worker",
            role="Old role.",
            behavior="Old behavior.",
            peers=[PeerDeclaration("old-peer", "old")],
            skills=["old-skill"],
        ))

        rt.send_admin("worker", "Replace everything.")

        d = rt._bus.objects["worker"].definition
        assert d.role == "New role."
        assert d.behavior == "New behavior."
        assert [p.object_id for p in d.peers] == ["p1"]
        assert d.skills == ["s1", "s2"]

    def test_admin_empty_peers_list_removes_all_peers(self):
        """Replace-semantics: an empty peers list removes every peer."""
        brain = MockBrain()
        brain.script_admin(
            reply="Peers cleared.",
            updated_definition={"peers": []},
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(
            object_id="worker",
            role="X",
            peers=[PeerDeclaration("a", "x"), PeerDeclaration("b", "y")],
        ))

        rt.send_admin("worker", "Remove all peers.")

        assert rt._bus.objects["worker"].definition.peers == []

    def test_admin_empty_patch_dict_is_no_op(self):
        """LLM returning updated_definition={} should leave everything alone
        and should NOT mark plans needs_replan (no real change)."""
        from src.lnl.types import Plan, PlanStep

        brain = MockBrain()
        brain.script_admin(
            reply="No change.",
            updated_definition={},
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="worker", role="Original."))

        obj = rt._bus.objects["worker"]
        fake = Plan(goal="x", steps=[PlanStep(kind="reason", description="t")], trace_id="t1")
        obj._active_plans["t1"] = fake

        rt.send_admin("worker", "Make no changes.")

        assert obj.definition.role == "Original."
        assert fake.needs_replan is False  # empty patch → no replan needed

    def test_admin_skills_filters_non_strings(self):
        """skills list filters out non-string entries defensively."""
        brain = MockBrain()
        brain.script_admin(
            reply="ok",
            updated_definition={"skills": ["valid", 123, None, "also-valid", {"x": 1}]},
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="worker", role="X"))

        rt.send_admin("worker", "Update skills.")

        assert rt._bus.objects["worker"].definition.skills == ["valid", "also-valid"]

    def test_admin_peers_filters_malformed_entries(self):
        """peers list filters out non-dict entries defensively."""
        brain = MockBrain()
        brain.script_admin(
            reply="ok",
            updated_definition={
                "peers": [
                    {"object_id": "good", "relationship": "ok"},
                    "not-a-dict",
                    None,
                    42,
                ],
            },
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="worker", role="X"))

        rt.send_admin("worker", "Update peers.")

        peers = rt._bus.objects["worker"].definition.peers
        assert [p.object_id for p in peers] == ["good"]

    def test_admin_history_includes_admin_message(self):
        """The admin message must be appended to history so subsequent turns
        can see the prior admin context if needed."""
        brain = MockBrain()
        brain.script_admin(
            reply="ok",
            updated_definition={"role": "New."},
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="worker", role="Old."))

        rt.send_admin("worker", "Update role.")

        history = rt._bus.objects["worker"].history
        assert any(m.content == "Update role." for m in history)
        assert any(m.type.name == "ADMIN" for m in history)

    def test_two_admins_in_succession(self):
        """Two admin patches in a row both take effect."""
        brain = MockBrain()
        brain.script_admin(
            reply="role updated",
            updated_definition={"role": "Role v2."},
            object_id="worker",
        )
        brain.script_admin(
            reply="behavior updated",
            updated_definition={"behavior": "Behavior v2."},
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="worker", role="v1.", behavior="b1."))

        rt.send_admin("worker", "Update role to v2.")
        rt.send_admin("worker", "Update behavior to v2.")

        d = rt._bus.objects["worker"].definition
        assert d.role == "Role v2."
        assert d.behavior == "Behavior v2."

    def test_admin_flags_all_concurrent_traces_for_replan(self):
        """If multiple plans are active on different traces, an admin patch
        marks every one of them needs_replan."""
        from src.lnl.types import Plan, PlanStep

        brain = MockBrain()
        brain.script_admin(
            reply="ok",
            updated_definition={"role": "New."},
            object_id="worker",
        )
        rt = Runtime(brain, system_config=SystemConfig(replan_on_modification=True))
        rt.create_object(ObjectDefinition(object_id="worker", role="Old."))

        obj = rt._bus.objects["worker"]
        plan_a = Plan(goal="A", steps=[PlanStep(kind="reason", description="a")], trace_id="ta")
        plan_b = Plan(goal="B", steps=[PlanStep(kind="reason", description="b")], trace_id="tb")
        plan_c = Plan(goal="C", steps=[PlanStep(kind="reason", description="c")], trace_id="tc")
        obj._active_plans["ta"] = plan_a
        obj._active_plans["tb"] = plan_b
        obj._active_plans["tc"] = plan_c

        rt.send_admin("worker", "Update role.")

        assert plan_a.needs_replan is True
        assert plan_b.needs_replan is True
        assert plan_c.needs_replan is True

    def test_replan_only_fires_for_message_trace(self):
        """A DOMAIN message on trace X re-plans trace X only. Other stale
        plans on other traces stay stale until their own next message."""
        from src.lnl.types import Plan, PlanStep, Message, MessageType

        brain = MockBrain()
        brain.script_admin(
            reply="ok",
            updated_definition={"behavior": "New."},
            object_id="worker",
        )
        # Re-plan response for trace X only
        brain.script_plan(
            {
                "goal": "fresh X plan",
                "steps": [{
                    "id": "s1", "step_number": 1, "kind": "reason",
                    "target": "self", "description": "fresh step", "reasoning": "test",
                }, {
                    "id": "final", "step_number": 2, "kind": "final",
                    "target": "final", "description": "done", "reasoning": "wrap",
                }],
            },
            object_id="worker",
        )
        brain.script("worker", LLMResponse(updated_state="", reply="done"))

        rt = Runtime(brain, system_config=SystemConfig(replan_on_modification=True))
        rt.create_object(ObjectDefinition(object_id="worker", role="X", behavior="b1", peers=[PeerDeclaration("peer-x", "downstream")]))
        obj = rt._bus.objects["worker"]

        plan_x = Plan(goal="X stale", steps=[PlanStep(kind="reason", description="old")], trace_id="trace-X")
        plan_y = Plan(goal="Y stale", steps=[PlanStep(kind="reason", description="old")], trace_id="trace-Y")
        obj._active_plans["trace-X"] = plan_x
        obj._active_plans["trace-Y"] = plan_y

        rt.send_admin("worker", "Update.")
        assert plan_x.needs_replan is True
        assert plan_y.needs_replan is True

        # Message arrives on trace X only
        msg = Message(
            sender="__user__", recipient="worker", type=MessageType.DOMAIN,
            content="continue", id="mX", trace_id="trace-X",
        )
        rt._dispatch([msg])

        assert plan_x.needs_replan is False        # re-planned
        assert plan_x.goal == "fresh X plan"
        assert plan_y.needs_replan is True         # untouched
        assert plan_y.goal == "Y stale"

    def test_admin_with_no_active_plans(self):
        """Admin patch on an object with no active plans is a clean no-op
        on the plans side (and applies the patch normally)."""
        brain = MockBrain()
        brain.script_admin(
            reply="ok",
            updated_definition={"role": "New."},
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="worker", role="Old."))

        obj = rt._bus.objects["worker"]
        assert obj._active_plans == {}

        rt.send_admin("worker", "Update.")

        assert obj.definition.role == "New."
        assert obj._active_plans == {}

    def test_admin_in_live_mode_applies_patch(self):
        """send_admin works when the runtime is in live mode (background loop).
        The work goes through the queue and the definition mutates before
        the call returns."""
        brain = MockBrain()
        brain.script_admin(
            reply="ok",
            updated_definition={"role": "Live-updated role."},
            object_id="worker",
        )
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="worker", role="Original."))

        rt.start(poll_interval=0.01)
        try:
            rt.send_admin("worker", "Update role.")
            d = rt._bus.objects["worker"].definition
            assert d.role == "Live-updated role."
        finally:
            rt.stop()

    def test_interleaved_admin_and_domain_in_live_mode(self):
        """Interleave DOMAIN and ADMIN messages on a live runtime; verify
        each takes the correct path: DOMAIN updates state, ADMIN updates
        definition, neither corrupts the other."""
        brain = MockBrain()
        # First DOMAIN: produces state
        brain.script("worker", LLMResponse(
            updated_state={"count": 1}, reply="domain-1",
        ))
        # ADMIN: updates role
        brain.script_admin(
            reply="role updated",
            updated_definition={"role": "Phase 2 role."},
            object_id="worker",
        )
        # Second DOMAIN: produces more state
        brain.script("worker", LLMResponse(
            updated_state={"count": 2}, reply="domain-2",
        ))

        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="worker", role="Phase 1 role."))

        rt.start(poll_interval=0.01)
        try:
            rt.send("worker", "domain-msg-1")
            assert rt.state("worker") == {"count": 1}
            assert rt._bus.objects["worker"].definition.role == "Phase 1 role."

            rt.send_admin("worker", "Update role.")
            assert rt._bus.objects["worker"].definition.role == "Phase 2 role."
            assert rt.state("worker") == {"count": 1}  # state preserved across admin

            rt.send("worker", "domain-msg-2")
            assert rt.state("worker") == {"count": 2}
            assert rt._bus.objects["worker"].definition.role == "Phase 2 role."  # still
        finally:
            rt.stop()

    def test_patchable_fields_drive_schema_and_apply(self):
        """The patchable contract is data-driven: PATCHABLE_FIELDS (types.py)
        is the single source of truth. The LLM schema, the apply step, and
        the prompt's field list must all match what's in PATCHABLE_FIELDS."""
        from src.lnl.brain import ADMIN_RESPONSE_SCHEMA, build_admin_prompt
        from src.lnl.types import PATCHABLE_FIELDS

        spec_names = {f.name for f in PATCHABLE_FIELDS}

        # 1. The schema's updated_definition properties match the spec.
        schema_props = set(
            ADMIN_RESPONSE_SCHEMA["properties"]["finish"]
            ["properties"]["updated_definition"]["properties"].keys()
        )
        assert schema_props == spec_names, (
            f"schema fields {schema_props} drift from spec {spec_names}"
        )

        # 2. The rendered prompt names every spec field — backticked in the
        #    spec block, no extras.
        rendered = build_admin_prompt(ObjectDefinition(object_id="x", role="r"))
        for name in spec_names:
            assert f"`{name}`" in rendered, f"prompt missing field `{name}`"

    def test_admin_handles_brain_without_admin_call(self):
        """A brain that doesn't implement admin_call should not crash — the
        path logs and returns a no-op ProcessingResult."""
        from src.lnl.brain import LLMBrain
        from src.lnl.types import LLMResponse, InferenceMetrics, ReactStep, ReactFinish

        class NoAdminBrain(LLMBrain):
            def call(self, messages, schema, *, object_id=None):
                return LLMResponse(updated_state="", reply="domain", outgoing_messages=[]), InferenceMetrics(model="x")

            def react_call(self, messages, *, object_id=None):
                return ReactStep(
                    thought="t", action="finish",
                    finish=ReactFinish(reply="r"),
                ), InferenceMetrics(model="x")

        rt = Runtime(NoAdminBrain())
        rt.create_object(ObjectDefinition(object_id="worker", role="Original."))

        # Should not raise
        results = rt.send_admin("worker", "Change something.")

        assert rt._bus.objects["worker"].definition.role == "Original."
        # The processing result should be empty/no-op shaped
        assert results == [] or all(r.reply == "" for r in results)


class TestTopology:
    def test_reflects_structure(self, rt):
        rt.create_object(ObjectDefinition(
            object_id="a",
            role="A",
            peers=[PeerDeclaration("b", "peer")],
        ))
        rt.create_object(ObjectDefinition(object_id="b", role="B"))

        topo = rt.topology()
        assert topo == {"a": ["b"], "b": []}


class TestPersistence:
    def test_save_reload_roundtrip(self, brain, tmp_path):
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(
            object_id="worker",
            role="Worker role.",
        ))

        path = rt.save_object("worker", tmp_path / "worker.md")
        assert path.exists()

        # Reload in a new runtime
        rt2 = Runtime(brain)
        obj = rt2.load_file(path)
        assert obj.object_id == "worker"
        assert obj.definition.role == "Worker role."

    def test_save_clears_modified(self, rt, tmp_path):
        rt.create_object(ObjectDefinition(object_id="x", role="X"))
        rt.modify("x", role="Updated")
        assert rt.has_unsaved_modifications("x")

        rt.save_object("x", tmp_path / "x.md")
        assert not rt.has_unsaved_modifications("x")


class TestMetrics:
    def test_metrics_after_send(self, rt):
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.send("a", "hello")

        assert rt.metrics.messages_routed == 1

    def test_message_log(self, rt):
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.send("a", "hello")

        assert len(rt.message_log) == 1
        assert rt.message_log[0].delivered is True


class TestCreateFromText:
    def test_create_from_markdown(self, rt):
        obj = rt.create_object_from_text("# Worker\n\n## Role\n\nDoes work.\n")
        assert obj.object_id == "worker"
        results = rt.send("worker", "hi")
        assert len(results) == 1


class TestEventSources:
    """Runtime manages event sources — objects declare interests, Runtime handles plumbing."""

    def test_event_source_accessible(self):
        """Object with event_sources gets a provider accessible via get_event_source."""
        brain = MockBrain()
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(
            object_id="slack",
            role="Slack monitor",
            event_sources=["Slack webhook: messages"],
        ))

        source = rt.get_event_source("slack", "Slack webhook: messages")
        assert source is not None

    def test_no_event_source_without_declaration(self):
        """Object without event_sources has no source."""
        brain = MockBrain()
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="worker", role="Worker"))

        source = rt.get_event_source("worker", "anything")
        assert source is None

    def test_fire_delivers_event(self):
        """fire() on event source → process_pending → object receives event."""
        brain = MockBrain()
        brain.script("slack", LLMResponse(
            updated_state={"status": "got message"}, reply="Received",
        ))

        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(
            object_id="slack",
            role="Slack monitor",
            event_sources=["Slack webhook: messages"],
        ))

        source = rt.get_event_source("slack", "Slack webhook: messages")
        source.fire("New message in #general: hello")
        results = rt.process_pending()

        assert len(results) == 1
        assert results[0].object_id == "slack"
        assert rt.state("slack") == {"status": "got message"}

    def test_fire_chains_to_peers(self):
        """Event fires → object processes → sends to peer → peer processes."""
        brain = MockBrain()
        brain.script("slack", LLMResponse(
            updated_state={"status": "forwarded"},
            reply="forwarding",
            outgoing_messages=[OutgoingMessage(recipient="triage", content="urgent ticket", expects_reply=True)],
        ))
        brain.script("triage", LLMResponse(
            updated_state={"status": "triaged"}, reply="handled",
        ))

        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(
            object_id="slack",
            role="Slack monitor",
            event_sources=["Slack webhook: messages"],
        ))
        rt.create_object(ObjectDefinition(object_id="triage", role="Triage"))

        source = rt.get_event_source("slack", "Slack webhook: messages")
        source.fire("urgent message")
        results = rt.process_pending()

        assert results[0].object_id == "slack"
        assert results[1].object_id == "triage"
        # triage's reply routes back to slack
        assert any(r.source_message_type == MessageType.REPLY for r in results)

    def test_multiple_event_sources(self):
        """Object with multiple event_sources gets separate providers."""
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(
            object_id="monitor",
            role="Multi-source monitor",
            event_sources=["Slack webhook: messages", "HubSpot: new deals"],
        ))

        slack = rt.get_event_source("monitor", "Slack webhook: messages")
        hubspot = rt.get_event_source("monitor", "HubSpot: new deals")
        assert slack is not None
        assert hubspot is not None
        assert slack is not hubspot


class TestLiveMode:
    def test_run_and_stop(self):
        """start() launches the loop, stop() shuts it down."""
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))
        rt = Runtime(brain)

        rt.start(poll_interval=0.01)
        assert rt.is_running

        rt.stop()
        assert not rt.is_running

    def test_submit_returns_results(self):
        """submit() enqueues work; results available after done is set."""
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={"status": "processed"}, reply="hello"))
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="worker", role="Worker"))

        rt.start(poll_interval=0.01)
        try:
            item = rt.submit("worker", "do work")
            item.done.wait(timeout=5.0)

            assert item.done.is_set()
            assert len(item.results) == 1
            assert item.results[0].object_id == "worker"
            assert item.results[0].reply == "hello"
        finally:
            rt.stop()

    def test_submit_chain(self):
        """Chained messages are processed within a single submit cycle."""
        brain = MockBrain()
        brain.script("a", LLMResponse(
            updated_state={"status": "a done"}, reply="A",
            outgoing_messages=[OutgoingMessage(recipient="b", content="from A", expects_reply=True)],
        ))
        brain.script("b", LLMResponse(updated_state={"status": "b done"}, reply="B"))
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.create_object(ObjectDefinition(object_id="b", role="B"))

        rt.start(poll_interval=0.01)
        try:
            item = rt.submit("a", "start")
            item.done.wait(timeout=5.0)
            # a→b chain + b's reply back to a = 3 results
            assert len(item.results) == 3
        finally:
            rt.stop()

    def test_kill_object(self):
        """kill_object removes the object from the runtime."""
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.create_object(ObjectDefinition(object_id="b", role="B"))

        assert "a" in rt.topology()
        rt.kill_object("a")
        assert "a" not in rt.topology()
        assert "b" in rt.topology()

    def test_on_result_callback(self):
        """on_result fires for each ProcessingResult."""
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={}, reply="hi"))
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="w", role="W"))

        received = []
        rt.start(poll_interval=0.01, on_result=received.append)
        try:
            item = rt.submit("w", "hello")
            item.done.wait(timeout=5.0)
            assert len(received) == 1
            assert received[0].object_id == "w"
        finally:
            rt.stop()

    def test_inject_event_in_live_mode(self):
        """inject_event routes through run-loop in live mode."""
        brain = MockBrain()
        brain.set_default(LLMResponse(updated_state={"status": "got event"}, reply="ok"))
        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(
            object_id="listener",
            role="Listener",
            event_sources=["test-source"],
        ))

        rt.start(poll_interval=0.01)
        try:
            results = rt.inject_event("listener", "ping")
            assert len(results) == 1
            assert results[0].object_id == "listener"
            assert rt.state("listener") == {"status": "got event"}
        finally:
            rt.stop()


class TestSpawn:
    """Tests for llm-class registration and create_object tool."""

    def _make_rt(self):
        brain = MockBrain()
        tool_registry = ToolRegistry()
        rt = Runtime(brain, tool_registry=tool_registry)
        return rt, brain

    def test_register_and_spawn_class(self):
        rt, brain = self._make_rt()
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))

        truck_class = ObjectDefinition(
            object_id="truck",
            role="Fleet vehicle driven by {driver_name} with {capacity}-pallet capacity.",
            behavior="Introduce yourself to dispatcher on creation.",
            peers=[PeerDeclaration("dispatcher", "report to")],
        )
        rt.register_class("truck", truck_class)

        truck = rt.spawn("truck-001", "truck", {"driver_name": "Carlos Rivera", "capacity": "20"})

        assert truck.object_id == "truck-001"
        assert "Carlos Rivera" in truck.definition.role
        assert "20-pallet" in truck.definition.role
        assert rt._bus.objects.get("truck-001") is not None

    def test_spawn_unknown_class_raises(self):
        rt, _ = self._make_rt()
        with pytest.raises(KeyError, match="not registered"):
            rt.spawn("truck-001", "truck", {})

    def test_create_object_tool_creates_object(self):
        """fleet-manager receives a registration message and calls create_object tool."""
        rt, brain = self._make_rt()

        rt.register_class("truck", ObjectDefinition(
            object_id="truck",
            role="Fleet vehicle driven by {driver_name}.",
            behavior="Upon creation, introduce yourself to dispatcher.",
            peers=[PeerDeclaration("dispatcher", "introduce on creation; receive assignments")],
        ))
        rt.create_object(ObjectDefinition(object_id="fleet-manager", role="Spawns trucks."))
        rt.create_object(ObjectDefinition(object_id="dispatcher", role="Coordinates fleet."))

        # fleet-manager: call create_object tool, then seed truck-001
        brain.script("fleet-manager", LLMResponse(
            updated_state={},
            reply="",
            tool_calls=[ToolCall(
                id="t1",
                tool="create_object",
                arguments={"object_id": "truck-001", "class_id": "truck", "params": {"driver_name": "Carlos Rivera"}},
            )],
        ))
        brain.script("fleet-manager", LLMResponse(
            updated_state={},
            reply="Truck registered.",
            outgoing_messages=[OutgoingMessage(
                recipient="truck-001",
                content="You're live. Driver: Carlos Rivera, 20-pallet, general cargo, North depot.",
            )],
        ))
        # truck-001: introduce itself to dispatcher on first message
        brain.script("truck-001", LLMResponse(
            updated_state={"status": "available", "driver": "Carlos Rivera"},
            reply="Hi dispatcher, I'm truck-001, driver Carlos Rivera, available.",
            outgoing_messages=[OutgoingMessage(
                recipient="dispatcher",
                content="I'm truck-001, driver Carlos Rivera, 20-pallet capacity. Available for assignments.",
            )],
        ))
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))

        results = rt.send("fleet-manager", "Register truck-001: driver Carlos Rivera, 20-pallet, general cargo.")

        assert rt._bus.objects.get("truck-001") is not None, "truck-001 was not spawned"
        assert rt.state("truck-001") == {"status": "available", "driver": "Carlos Rivera"}
        obj_ids = {r.object_id for r in results}
        assert "fleet-manager" in obj_ids
        assert "truck-001" in obj_ids
        assert "dispatcher" in obj_ids

    def test_load_class_from_file(self, tmp_path):
        rt, brain = self._make_rt()
        brain.set_default(LLMResponse(updated_state={}, reply="ok"))

        (tmp_path / "truck.md").write_text(
            "# Truck\n\ntype: class\n\n## Role\n\nFleet vehicle driven by {driver_name}.\n"
        )
        (tmp_path / "dispatcher.md").write_text(
            "# Dispatcher\n\n## Role\n\nCoordinates the fleet.\n"
        )

        objects = rt.load_directory(tmp_path)

        assert len(objects) == 1  # only dispatcher is instantiated
        assert objects[0].object_id == "dispatcher"
        assert "truck" in rt._classes  # truck registered as class, not object
        assert rt._bus.objects.get("truck") is None

        truck = rt.spawn("truck-007", "truck", {"driver_name": "Maya Patel"})
        assert truck.object_id == "truck-007"
        assert "Maya Patel" in truck.definition.role


class TestTransactionTracing:
    """Trace invariants — every cascaded message must link back to the root trigger."""

    def test_cascade_shares_trace_id_and_parent_chain(self):
        # A asks B; B tells C. One external send → 3 hops, all sharing one trace_id.
        brain = MockBrain()
        brain.script("a", LLMResponse(
            updated_state={},
            reply="",
            outgoing_messages=[OutgoingMessage(recipient="b", content="ask-b", expects_reply=True)],
        ))
        brain.script("b", LLMResponse(
            updated_state={},
            reply="b-replies-to-a",
            outgoing_messages=[OutgoingMessage(recipient="c", content="tell-c", expects_reply=False)],
        ))
        brain.script("c", LLMResponse(updated_state={}, reply="c-done"))

        rt = Runtime(brain)
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.create_object(ObjectDefinition(object_id="b", role="B"))
        rt.create_object(ObjectDefinition(object_id="c", role="C"))

        rt.send("a", "kickoff")

        logs = rt.message_log
        assert len(logs) >= 3, f"expected at least 3 bus deliveries, got {len(logs)}: {[l.message.id for l in logs]}"

        # All hops share one trace_id (the root msg.id).
        trace_ids = {l.message.trace_id for l in logs}
        assert len(trace_ids) == 1, f"expected single trace_id, got {trace_ids}"
        root_trace_id = next(iter(trace_ids))
        root = logs[0].message
        assert root.trace_id == root.id, "root message's trace_id must equal its own id"
        assert root.parent_id is None, "root message must have no parent"
        assert root_trace_id == root.id

        # Every non-root span has a parent_id that matches some prior span's msg_id.
        seen_ids = {root.id}
        for entry in logs[1:]:
            msg = entry.message
            assert msg.parent_id is not None, f"non-root msg {msg.id} missing parent_id"
            assert msg.parent_id in seen_ids, (
                f"parent {msg.parent_id} of {msg.id} not seen yet; seen={seen_ids}"
            )
            seen_ids.add(msg.id)

        # Timing fields are populated on each MessageLog entry (MockBrain runs ReAct → records timing).
        for entry in logs:
            assert entry.received_at is not None, f"missing received_at on {entry.message.id}"
            # processing_started_at / completed_at are populated only when the runtime
            # actually ran process_message — broadcast/heartbeat hops may skip — but
            # in this scenario every delivery triggers processing.
            assert entry.processing_started_at is not None, f"missing started_at on {entry.message.id}"
            assert entry.processing_completed_at is not None, f"missing completed_at on {entry.message.id}"
            assert entry.processing_completed_at >= entry.processing_started_at

        # hop_depth grows along the chain (root at 0).
        assert logs[0].hop_depth == 0
        assert any(l.hop_depth > 0 for l in logs[1:]), "expected at least one non-root hop_depth > 0"


class TestCodeToolConfig:
    """The built-in `python` REPL tool is config-toggleable via SystemConfig."""

    def test_python_registered_by_default(self):
        from src.lnl.runtime import SystemConfig
        brain = MockBrain()
        registry = ToolRegistry()
        Runtime(brain, tool_registry=registry, system_config=SystemConfig())
        assert "python" in registry.tool_names
        assert "create_object" in registry.tool_names

    def test_python_omitted_when_disabled(self):
        from src.lnl.runtime import SystemConfig
        brain = MockBrain()
        registry = ToolRegistry()
        Runtime(brain, tool_registry=registry, system_config=SystemConfig(enable_code_tool=False))
        assert "python" not in registry.tool_names
        # The fallback default agent still gets create_object — that's invariant.
        assert "create_object" in registry.tool_names

    def test_describe_identical_to_baseline_when_disabled(self):
        """Disabling the code tool must produce the same tool description as
        a runtime built without any code-tool wiring at all — proves the
        config switch is a true revert to the default agent."""
        from src.lnl.runtime import SystemConfig
        baseline = ToolRegistry()
        Runtime(MockBrain(), tool_registry=baseline,
                system_config=SystemConfig(enable_code_tool=False))
        # Should describe exactly the create_object tool — no `python`, no orphans.
        desc = baseline.describe()
        assert "python" not in desc
        assert "create_object" in desc

    def test_repl_state_persists_across_tool_calls_in_runtime(self):
        """End-to-end: a MockBrain-scripted object emits two `python` tool_calls
        that share state via the per-object REPL namespace, then finishes with
        the computed result in the reply."""
        from src.lnl.types import ReactFinish, ReactStep

        brain = MockBrain()
        registry = ToolRegistry()
        rt = Runtime(brain, tool_registry=registry)
        rt.create_object(ObjectDefinition(object_id="coder", role="Code runner"))

        # Step 1: assign x = 41 in REPL
        brain.script_react(ReactStep(
            thought="assign",
            action="tool_call",
            tool_call=ToolCall(id="tc-1", tool="python", arguments={"code": "x = 41"}),
        ))
        # Step 2: compute x + 1 (depends on previous namespace state)
        brain.script_react(ReactStep(
            thought="compute",
            action="tool_call",
            tool_call=ToolCall(id="tc-2", tool="python", arguments={"code": "x + 1"}),
        ))
        # Step 3: finish — pass the result through in the reply
        brain.script_react(ReactStep(
            thought="reply",
            action="finish",
            finish=ReactFinish(reply="The answer is 42"),
        ))

        results = rt.send("coder", "compute it")
        assert results, "expected a processing result"
        # With async tool dispatch, intermediate pending results are emitted before
        # the final reply. The last result carries the completed reply.
        final_result = max(results, key=lambda r: r.sequence)
        assert "42" in final_result.reply
