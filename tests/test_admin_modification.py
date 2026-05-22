"""Real-LLM scenario tests for the admin-modification mechanism.

These exercise object_admin.yaml + admin_call() against a live model to
verify the LLM actually produces well-shaped definition patches from NL
admin instructions, and that ambiguous / out-of-scope requests fall back
to a clarifying reply with no patch.

Requires AZURE_OPENAI_API_KEY (preferred — canonical judge stack uses Azure)
or OPENAI_API_KEY in .env. Run:

    pytest tests/test_admin_modification.py -v -s
"""
from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

from src.lnl.runtime import Runtime, SystemConfig
from src.lnl.types import ObjectDefinition, PeerDeclaration

load_dotenv()

# Prefer Azure when its keys are present (matches canonical judge_model=gpt-5.4);
# fall back to OpenAI gpt-5.4-mini otherwise.
_HAS_AZURE = bool(os.environ.get("AZURE_OPENAI_API_KEY") and os.environ.get("AZURE_OPENAI_ENDPOINT"))
_HAS_OPENAI = bool(os.environ.get("OPENAI_API_KEY"))

pytestmark = pytest.mark.skipif(
    not (_HAS_AZURE or _HAS_OPENAI),
    reason="Neither AZURE_OPENAI_* nor OPENAI_API_KEY is set",
)


def _make_brain():
    if _HAS_AZURE:
        from src.lnl.brain import AzureBrain
        return AzureBrain(model="gpt-5.4-mini", temperature=0.0, seed=42)
    from src.lnl.brain import OpenAIBrain
    return OpenAIBrain(model="gpt-5.4-mini", temperature=0.0, seed=42)


def _make_runtime() -> Runtime:
    # Planner / evaluator off — admin path doesn't use them, and turning
    # them off keeps each test to a single LLM call (the admin_call itself).
    cfg = SystemConfig(enable_planner=False, enable_evaluator=False)
    return Runtime(_make_brain(), system_config=cfg)


# ---------------------------------------------------------------------------
# Patch-shape tests — each verifies the LLM produces the right patch for
# one type of NL admin instruction.
# ---------------------------------------------------------------------------

class TestRealLLMRoleChange:
    def test_role_change_via_natural_language(self):
        rt = _make_runtime()
        rt.create_object(ObjectDefinition(
            object_id="library-clerk",
            role="Helps patrons find and check out books from the general collection.",
            behavior="On a book request, look up the title in the catalog and offer to check it out.",
        ))

        rt.send_admin(
            "library-clerk",
            "Please update your role to be a research-desk specialist for the special-collections wing.",
        )

        d = rt._bus.objects["library-clerk"].definition
        role_lower = d.role.lower()
        # Role should mention research, special collections, or specialist —
        # the new focus from the admin instruction.
        assert any(kw in role_lower for kw in ("research", "special collection", "specialist")), (
            f"Expected research/special-collections specialist in new role, got: {d.role!r}"
        )
        # Behavior is NOT in the instruction → should be unchanged.
        assert "catalog" in d.behavior.lower(), f"Behavior should be unchanged, got: {d.behavior!r}"


class TestRealLLMBehaviorChange:
    def test_behavior_change_keeps_role(self):
        rt = _make_runtime()
        rt.create_object(ObjectDefinition(
            object_id="ticket-router",
            role="Routes incoming support tickets to the right team.",
            behavior="On a new ticket, classify by keyword and forward to the team queue.",
        ))
        original_role = rt._bus.objects["ticket-router"].definition.role

        rt.send_admin(
            "ticket-router",
            "Stop using keywords. Instead, send every new ticket directly to the triage-lead peer for human classification.",
        )

        d = rt._bus.objects["ticket-router"].definition
        assert d.role == original_role, "Role must not change when only behavior is asked to change"
        # Behavior mentions triage-lead or human classification
        b = d.behavior.lower()
        assert "triage" in b or "human" in b, f"New behavior should mention triage/human, got: {d.behavior!r}"


class TestRealLLMPeerAdd:
    def test_add_peer(self):
        rt = _make_runtime()
        rt.create_object(ObjectDefinition(
            object_id="order-processor",
            role="Processes online orders.",
            peers=[PeerDeclaration("billing", "Charge the customer card on order confirmation.")],
        ))

        rt.send_admin(
            "order-processor",
            "Add a new peer called 'shipping' — when an order is confirmed, notify shipping with the address and item list.",
        )

        d = rt._bus.objects["order-processor"].definition
        peer_ids = [p.object_id for p in d.peers]
        assert "shipping" in peer_ids, f"shipping should be a peer, got {peer_ids}"
        # billing should still be there (full-replace semantics — LLM must include both)
        assert "billing" in peer_ids, f"billing should be preserved, got {peer_ids}"


class TestRealLLMPeerRemove:
    def test_remove_peer(self):
        rt = _make_runtime()
        rt.create_object(ObjectDefinition(
            object_id="notifier",
            role="Sends notifications to subscribed peers.",
            peers=[
                PeerDeclaration("slack", "Send Slack messages on incidents."),
                PeerDeclaration("pager", "Page on critical incidents."),
                PeerDeclaration("email", "Email on info-level events."),
            ],
        ))

        rt.send_admin("notifier", "Remove the email peer. We don't send emails anymore.")

        d = rt._bus.objects["notifier"].definition
        peer_ids = [p.object_id for p in d.peers]
        assert "email" not in peer_ids, f"email should be removed, got {peer_ids}"
        assert "slack" in peer_ids and "pager" in peer_ids, f"others preserved, got {peer_ids}"


class TestRealLLMSkillChange:
    def test_add_skill(self):
        rt = _make_runtime()
        rt.create_object(ObjectDefinition(
            object_id="data-fetcher",
            role="Fetches data from internal systems.",
            skills=["http-get"],
        ))

        rt.send_admin(
            "data-fetcher",
            "Also enable the 'sql-query' and 'redis-read' skills so you can read from databases.",
        )

        skills = rt._bus.objects["data-fetcher"].definition.skills
        assert "sql-query" in skills, f"sql-query missing, got {skills}"
        assert "redis-read" in skills, f"redis-read missing, got {skills}"
        assert "http-get" in skills, f"http-get preserved, got {skills}"


# ---------------------------------------------------------------------------
# Defensive tests — ambiguous and out-of-scope instructions.
# ---------------------------------------------------------------------------

class TestRealLLMAmbiguous:
    def test_ambiguous_instruction_returns_no_patch(self):
        """A clearly ambiguous instruction should result in NO definition
        change (LLM asks for clarification instead). Role is unchanged."""
        rt = _make_runtime()
        rt.create_object(ObjectDefinition(
            object_id="worker",
            role="Worker.",
            behavior="Does work.",
        ))
        original = rt._bus.objects["worker"].definition

        rt.send_admin("worker", "Make it better.")

        d = rt._bus.objects["worker"].definition
        assert d.role == original.role, f"Role should be unchanged for ambiguous instruction, got {d.role!r}"
        assert d.behavior == original.behavior, f"Behavior should be unchanged, got {d.behavior!r}"


class TestRealLLMOutOfScope:
    def test_request_to_change_state_is_refused(self):
        """The admin path cannot modify state — only the 4 patchable fields.
        A request to 'change state' must NOT mutate role/behavior either."""
        rt = _make_runtime()
        rt.create_object(ObjectDefinition(
            object_id="worker",
            role="Worker.",
            behavior="Does work.",
        ))
        original_role = rt._bus.objects["worker"].definition.role
        original_behavior = rt._bus.objects["worker"].definition.behavior

        rt.send_admin("worker", "Reset your state to {}.")

        d = rt._bus.objects["worker"].definition
        # The LLM should either ignore (no patch) or ask for clarification.
        # Either way, role and behavior shouldn't get a state-shaped value.
        assert d.role == original_role, (
            f"Role must not be hijacked by state-change request, got: {d.role!r}"
        )
        assert d.behavior == original_behavior, (
            f"Behavior must not be hijacked, got: {d.behavior!r}"
        )

    def test_request_to_change_object_id_is_refused(self):
        rt = _make_runtime()
        rt.create_object(ObjectDefinition(object_id="worker", role="Worker."))

        rt.send_admin("worker", "Rename yourself to 'manager' from now on.")

        d = rt._bus.objects["worker"].definition
        assert d.object_id == "worker", (
            f"object_id is not patchable; got {d.object_id!r}"
        )


# ---------------------------------------------------------------------------
# State-preservation under real LLM
# ---------------------------------------------------------------------------

class TestRealLLMStatePreserved:
    def test_state_preserved_across_admin(self):
        """A DOMAIN message produces state, then an admin patch changes the
        definition, and state must remain intact."""
        rt = _make_runtime()
        rt.create_object(ObjectDefinition(
            object_id="counter",
            role="Counts events.",
            behavior=(
                "On every incoming message, increment a 'count' field in state by 1 "
                "and reply with the new count. Keep all prior fields in state."
            ),
        ))

        # Produce some state.
        rt.send("counter", "tick")
        rt.send("counter", "tick")
        state_before = rt.state("counter")

        # Admin modification.
        rt.send_admin("counter", "Update your role to: Counts and labels events.")

        state_after = rt.state("counter")
        d = rt._bus.objects["counter"].definition

        assert state_before == state_after, (
            f"State must not change on admin turn.\nBefore: {state_before}\nAfter:  {state_after}"
        )
        # Role should mention "label" (the new requirement)
        assert "label" in d.role.lower(), f"Expected updated role, got: {d.role!r}"
