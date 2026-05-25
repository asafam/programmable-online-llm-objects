"""Unit tests for the steps-based OpenClaw single-agent export.

Verifies that ``export_single_agent_workspace(sample, …)`` produces an
AGENTS.md driven by the workflow's grounded steps (NOT by the LLM-object
decomposition), per the steps-based refactor in src/lnl/openclaw_export.py.

Run:
    pytest tests/test_openclaw_steps_based.py -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.data.schema import (
    MockToolDef,
    ObjectDef,
    PeerDecl,
    Sample,
)
from src.lnl.openclaw_export import (
    export_single_agent_workspace,
    reset_single_agent_state,
)

# ── Fixture: a minimal Sample we control end-to-end ──────────────────────────


@pytest.fixture
def sample() -> Sample:
    """Minimal Sample with steps + objects + tools.

    `steps` and `objects` are intentionally lexically disjoint so the test
    can prove which one ended up in AGENTS.md.
    """
    return Sample(
        id="widget-approval-temporal-TC001",
        sample_id="widget-approval",
        name="Widget Approval Pipeline",
        domain="sales",
        source_type="Zapier/Workflow Logic",
        link="",
        objects=[
            ObjectDef(
                object_id="alpha-ingest",
                role="ingest_service",
                behavior="ALPHA_BEHAVIOR_MARKER receives widget submissions.",
                state_description="",
                peers=[PeerDecl(object_id="beta-orchestrator",
                                relationship="forwards to")],
                skills=[],
                event_sources=["alpha:submitted"],
            ),
            ObjectDef(
                object_id="beta-orchestrator",
                role="business_logic",
                behavior="BETA_BEHAVIOR_MARKER routes approval requests.",
                state_description="",
                peers=[],
                skills=[],
                event_sources=[],
            ),
        ],
        steps=[
            "When a sales rep submits a widget for approval, the workflow captures the submission.",
            "The workflow identifies approvers based on widget category and submitter chain.",
            "Approval requests are sent by email and posted to a Slack channel.",
            "Approvers respond in Slack; each response is recorded against the active approval case.",
            "If approved, the widget status is updated; if rejected, the submitter may resubmit.",
        ],
        modifications=[],
        events=[],
        tools=[
            MockToolDef(
                tool_name="org_directory_data",
                description="Org directory",
                arguments_schema={"type": "object", "properties": {},
                                  "required": []},
                response_template='{"employees": []}',
            ),
        ],
    )


# ── Test 1: file layout ─────────────────────────────────────────────────────


def test_workspace_files_created(sample, tmp_path):
    """export_single_agent_workspace writes the expected file layout."""
    ws_str = export_single_agent_workspace(
        sample, tmp_path, agent_id="lnl-eval", force=True
    )
    ws = Path(ws_str)
    assert ws == tmp_path / "workspace-lnl-eval"
    assert (ws / "AGENTS.md").is_file()
    assert (ws / "SOUL.md").is_file()
    assert (ws / "state.md").is_file()
    # OpenClaw needs the agentDir to exist for auth profiles.
    assert (tmp_path / "agents" / "lnl-eval" / "agent").is_dir()


# ── Test 2: AGENTS.md is built from steps, not objects ──────────────────────


def test_agents_md_uses_steps_not_objects(sample, tmp_path):
    """AGENTS.md must contain every step verbatim and zero object_ids/behaviors."""
    export_single_agent_workspace(sample, tmp_path, agent_id="lnl-eval", force=True)
    agents_md = (tmp_path / "workspace-lnl-eval" / "AGENTS.md").read_text()

    # Every step must appear verbatim.
    for step in sample.steps:
        assert step in agents_md, f"Step missing from AGENTS.md: {step!r}"

    # No object_ids should leak in.
    for obj in sample.objects:
        assert obj.object_id not in agents_md, (
            f"Object id {obj.object_id!r} leaked into steps-based AGENTS.md"
        )

    # No behavior markers should leak in (proves the object behaviors weren't used).
    assert "ALPHA_BEHAVIOR_MARKER" not in agents_md
    assert "BETA_BEHAVIOR_MARKER" not in agents_md

    # The workflow name must be present (it titles the system prompt).
    assert sample.name in agents_md


def test_agents_md_has_tool_catalog_and_no_a2a(sample, tmp_path):
    """Steps-based AGENTS.md must include the SaaS tool catalog and forbid A2A."""
    export_single_agent_workspace(sample, tmp_path, agent_id="lnl-eval", force=True)
    agents_md = (tmp_path / "workspace-lnl-eval" / "AGENTS.md").read_text()

    # Smoke check on the catalog.
    for tool_sig in (
        "slack_send_message(channel, message)",
        "email_send(to, subject, body)",
        "hubspot_update_deal(deal_id, properties)",
    ):
        assert tool_sig in agents_md, f"Missing tool signature: {tool_sig}"

    # The anti-A2A directive must be there (it's what enforces single-agent mode).
    assert "agentToAgent" in agents_md
    assert "only agent" in agents_md.lower()


def test_steps_are_numbered_in_order(sample, tmp_path):
    """Steps must be rendered as a numbered list in declaration order."""
    export_single_agent_workspace(sample, tmp_path, agent_id="lnl-eval", force=True)
    agents_md = (tmp_path / "workspace-lnl-eval" / "AGENTS.md").read_text()

    positions = []
    for i, step in enumerate(sample.steps, start=1):
        marker = f"{i}. {step}"
        assert marker in agents_md, f"Step {i} not numbered correctly"
        positions.append(agents_md.index(marker))
    # Strictly increasing → order preserved.
    assert positions == sorted(positions)


# ── Test 3: SOUL.md is generic (no object decomposition leakage) ────────────


def test_soul_md_is_generic(sample, tmp_path):
    export_single_agent_workspace(sample, tmp_path, agent_id="lnl-eval", force=True)
    soul = (tmp_path / "workspace-lnl-eval" / "SOUL.md").read_text()

    assert sample.name in soul
    for obj in sample.objects:
        assert obj.object_id not in soul, f"Object id leaked into SOUL.md: {obj.object_id}"


# ── Test 4: state.md starts empty (no per-object seed states) ───────────────


def test_state_md_starts_empty(sample, tmp_path):
    export_single_agent_workspace(sample, tmp_path, agent_id="lnl-eval", force=True)
    state = (tmp_path / "workspace-lnl-eval" / "state.md").read_text()

    assert "Empty" in state or "empty" in state.lower()
    # No per-object section headers — those would belong to the legacy combined-state mode.
    for obj in sample.objects:
        assert obj.object_id not in state


def test_reset_clears_state(sample, tmp_path):
    """reset_single_agent_state must restore the empty state placeholder."""
    export_single_agent_workspace(sample, tmp_path, agent_id="lnl-eval", force=True)
    state_file = tmp_path / "workspace-lnl-eval" / "state.md"

    # Mutate state to simulate runtime updates.
    state_file.write_text("# State\n\nRecord 1 was created.\n")
    assert "Record 1" in state_file.read_text()

    reset_single_agent_state(sample, tmp_path, agent_id="lnl-eval")
    refreshed = state_file.read_text()
    assert "Record 1" not in refreshed
    assert "empty" in refreshed.lower()


# ── Test 5: backward compatibility with the legacy list-of-objects path ─────


def test_legacy_list_input_falls_back_to_combined_objects(sample, tmp_path):
    """Passing list[ObjectDef] (legacy) must still produce a valid workspace
    that uses the combined-objects helpers, not the steps-based ones."""
    export_single_agent_workspace(sample.objects, tmp_path,
                                  agent_id="lnl-eval", force=True)
    agents_md = (tmp_path / "workspace-lnl-eval" / "AGENTS.md").read_text()

    # Legacy path SHOULD include object_ids and behaviors.
    for obj in sample.objects:
        assert obj.object_id in agents_md
    assert "ALPHA_BEHAVIOR_MARKER" in agents_md
    assert "BETA_BEHAVIOR_MARKER" in agents_md
    # Workflow name should NOT be there (legacy path doesn't reference it).
    # Steps should NOT be there in the legacy path.
    for step in sample.steps:
        assert step not in agents_md, (
            f"Steps leaked into legacy AGENTS.md — should be objects-only: {step!r}"
        )


# ── Test 6: real benchmark TC end-to-end (no Docker, just file generation) ──


@pytest.fixture
def real_hubspot_sample() -> Sample | None:
    """Load a real Sample from the v6 dataset if available."""
    path = Path("outputs/data/zapier/20260522_rev/workflows-mods.jsonl")
    if not path.exists():
        return None
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            s = Sample.model_validate_json(line)
            if s.sample_id == "deal-desk-manage-hubspot-quote-approvals-slack":
                return s
    return None


def test_real_hubspot_sample_produces_steps_based_agents_md(real_hubspot_sample, tmp_path):
    """End-to-end on a real benchmark sample — proves the wiring works on actual data."""
    if real_hubspot_sample is None:
        pytest.skip("v6 dataset not available; this test runs in-repo only.")

    s = real_hubspot_sample
    export_single_agent_workspace(s, tmp_path, agent_id="lnl-eval", force=True)
    agents_md = (tmp_path / "workspace-lnl-eval" / "AGENTS.md").read_text()

    assert "HubSpot" in agents_md  # workflow name
    assert "HubSpot Quotes" in agents_md  # step 1 mentions the source system

    # Hard test: none of the LNL object_ids should leak in.
    for obj in s.objects:
        assert obj.object_id not in agents_md, (
            f"Real LNL object_id {obj.object_id!r} leaked into steps-based AGENTS.md"
        )

    # And the agent should be told it's the only agent.
    assert "only agent" in agents_md.lower()
    assert "agentToAgent" in agents_md
