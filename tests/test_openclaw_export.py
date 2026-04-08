"""Tests for src/lnl/openclaw_export.py"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.lnl.openclaw_export import (
    EventSourceBinding,
    _agents_md,
    _classify_event_source,
    _parse_cron_schedule,
    _slug_to_name,
    _soul_md,
    _state_md,
    apply_modification,
    export_workflow,
)
from src.lnl.types import ObjectDefinition, PeerDeclaration


# ---------------------------------------------------------------------------
# Helper fixtures
# ---------------------------------------------------------------------------

def _minimal_md(name: str = "Guest Manager", role: str = "Manages guests.") -> str:
    return f"# {name}\n\n## Role\n\n{role}\n"


def _full_md() -> str:
    return (
        "# Quote Approvals\n\n"
        "## Role\n\nProcesses quote approval requests.\n\n"
        "## State\n\nApproval rules:\n- Quotes under $1000: auto-approved\n\n"
        "## Behavior\n\nWhen a quote arrives, check rules.\n\n"
        "## Peers\n\n"
        "- active-directory: Query for employee info\n"
        "- slack-notifier: Send notifications\n\n"
        "## Skills\n\n- check-rules\n- send-notification\n\n"
        "## Event Sources\n\n"
        "- email-webhook\n"
        "- cron: daily at 9am\n"
    )


# ---------------------------------------------------------------------------
# _slug_to_name
# ---------------------------------------------------------------------------

def test_slug_to_name_basic():
    assert _slug_to_name("guest-manager") == "Guest Manager"


def test_slug_to_name_single_word():
    assert _slug_to_name("billing") == "Billing"


def test_slug_to_name_multiple_hyphens():
    assert _slug_to_name("front-desk-agent") == "Front Desk Agent"


# ---------------------------------------------------------------------------
# _classify_event_source
# ---------------------------------------------------------------------------

def test_classify_webhook():
    b = _classify_event_source("email-webhook")
    assert b.kind == "webhook"
    assert b.webhook_name == "email-webhook"
    assert b.warning == ""


def test_classify_webhook_case_insensitive():
    b = _classify_event_source("Webhook-inbound")
    assert b.kind == "webhook"


def test_classify_cron_daily_am():
    b = _classify_event_source("cron: daily at 9am")
    assert b.kind == "cron"
    assert b.cron_expr == "0 9 * * *"
    assert b.warning == ""


def test_classify_cron_daily_pm():
    b = _classify_event_source("cron: daily at 2pm")
    assert b.kind == "cron"
    assert b.cron_expr == "0 14 * * *"


def test_classify_cron_every_minutes():
    b = _classify_event_source("cron: every 5 minutes")
    assert b.kind == "cron"
    assert b.cron_expr == "*/5 * * * *"


def test_classify_cron_hourly():
    b = _classify_event_source("cron: hourly")
    assert b.kind == "cron"
    assert b.cron_expr == "0 * * * *"


def test_classify_cron_weekly():
    b = _classify_event_source("cron: weekly on Monday")
    assert b.kind == "cron"
    assert b.cron_expr == "0 9 * * 0"


def test_classify_cron_unresolved():
    b = _classify_event_source("cron: twice a month on paydays")
    assert b.kind == "cron"
    assert b.cron_expr == ""
    assert "unresolved" in b.warning


def test_classify_unknown():
    b = _classify_event_source("pms-api")
    assert b.kind == "unknown"
    assert "unresolved" in b.warning


# ---------------------------------------------------------------------------
# Content builders
# ---------------------------------------------------------------------------

def test_agents_md_contains_role():
    obj = ObjectDefinition(object_id="guest-manager", role="Manages guests.")
    md = _agents_md(obj)
    assert "Manages guests." in md
    assert "## Role" in md
    assert "## Behavior" in md
    assert "(No specific behavior defined.)" in md
    assert "(No peers defined.)" in md


def test_agents_md_with_peers():
    obj = ObjectDefinition(
        object_id="guest-manager",
        role="Manages guests.",
        peers=[PeerDeclaration("billing", "Handles payments")],
    )
    md = _agents_md(obj)
    assert "**billing**" in md
    assert "Handles payments" in md


def test_soul_md_first_sentence():
    obj = ObjectDefinition(
        object_id="quote-approvals",
        role="Processes quote approval requests. Also does routing.",
    )
    md = _soul_md(obj)
    assert "Processes quote approval requests" in md
    assert "Also does routing" not in md


def test_state_md_initial_state():
    obj = ObjectDefinition(object_id="x", role="r", initial_state="No items yet.")
    md = _state_md(obj)
    assert "No items yet." in md
    assert "_Empty" not in md


def test_state_md_empty():
    obj = ObjectDefinition(object_id="x", role="r")
    md = _state_md(obj)
    assert "_Empty" in md


# ---------------------------------------------------------------------------
# export_workflow — structure
# ---------------------------------------------------------------------------

def _write_objects(tmp_path: Path, *md_texts: str) -> Path:
    obj_dir = tmp_path / "objects"
    obj_dir.mkdir()
    for i, text in enumerate(md_texts):
        (obj_dir / f"obj{i}.md").write_text(text)
    return obj_dir


def test_export_workflow_creates_openclaw_json(tmp_path):
    obj_dir = _write_objects(tmp_path, _minimal_md())
    out_dir = tmp_path / "out"
    export_workflow(obj_dir, out_dir)
    assert (out_dir / "openclaw.json").exists()


def test_export_workflow_openclaw_json_schema(tmp_path):
    obj_dir = _write_objects(tmp_path, _full_md())
    out_dir = tmp_path / "out"
    export_workflow(obj_dir, out_dir)
    config = json.loads((out_dir / "openclaw.json").read_text())
    assert "agents" in config
    assert "tools" in config
    assert config["tools"]["agentToAgent"]["enabled"] is True
    assert "quote-approvals" in config["tools"]["agentToAgent"]["allow"]
    assert "_meta" in config
    assert config["_meta"]["object_count"] == 1


def test_export_workflow_per_object_files(tmp_path):
    obj_dir = _write_objects(tmp_path, _full_md())
    out_dir = tmp_path / "out"
    export_workflow(obj_dir, out_dir)
    ws = out_dir / "workspace-quote-approvals"
    assert (ws / "AGENTS.md").exists()
    assert (ws / "SOUL.md").exists()
    assert (ws / "state.md").exists()


def test_export_workflow_skills_stubs(tmp_path):
    obj_dir = _write_objects(tmp_path, _full_md())
    out_dir = tmp_path / "out"
    export_workflow(obj_dir, out_dir)
    ws = out_dir / "workspace-quote-approvals"
    assert (ws / "skills" / "check-rules.md").exists()
    assert (ws / "skills" / "send-notification.md").exists()


def test_export_workflow_agents_md_content(tmp_path):
    obj_dir = _write_objects(tmp_path, _full_md())
    out_dir = tmp_path / "out"
    export_workflow(obj_dir, out_dir)
    agents_md = (out_dir / "workspace-quote-approvals" / "AGENTS.md").read_text()
    assert "Processes quote approval requests." in agents_md
    assert "**active-directory**" in agents_md
    assert "**slack-notifier**" in agents_md


def test_export_workflow_state_md_initial_content(tmp_path):
    obj_dir = _write_objects(tmp_path, _full_md())
    out_dir = tmp_path / "out"
    export_workflow(obj_dir, out_dir)
    state_md = (out_dir / "workspace-quote-approvals" / "state.md").read_text()
    assert "Approval rules" in state_md


def test_export_workflow_event_bindings(tmp_path):
    obj_dir = _write_objects(tmp_path, _full_md())
    out_dir = tmp_path / "out"
    export_workflow(obj_dir, out_dir)
    config = json.loads((out_dir / "openclaw.json").read_text())
    bindings = config["eventBindings"]
    types = {b["type"] for b in bindings}
    assert "webhook" in types
    assert "cron" in types


def test_export_workflow_agent_dir_created(tmp_path):
    obj_dir = _write_objects(tmp_path, _minimal_md())
    out_dir = tmp_path / "out"
    export_workflow(obj_dir, out_dir)
    assert (out_dir / "agents" / "guest-manager" / "agent").is_dir()


def test_export_workflow_no_md_files_raises(tmp_path):
    obj_dir = tmp_path / "empty"
    obj_dir.mkdir()
    with pytest.raises(ValueError, match="No .md files found"):
        export_workflow(obj_dir, tmp_path / "out")


def test_export_workflow_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        export_workflow(tmp_path / "nonexistent", tmp_path / "out")


def test_export_workflow_force_flag(tmp_path):
    obj_dir = _write_objects(tmp_path, _minimal_md())
    out_dir = tmp_path / "out"
    export_workflow(obj_dir, out_dir)
    # Second export without force raises
    with pytest.raises(FileExistsError):
        export_workflow(obj_dir, out_dir, force=False)
    # With force — succeeds
    export_workflow(obj_dir, out_dir, force=True)


def test_export_workflow_dry_run_no_writes(tmp_path):
    obj_dir = _write_objects(tmp_path, _minimal_md())
    out_dir = tmp_path / "out"
    written = export_workflow(obj_dir, out_dir, dry_run=True)
    assert len(written) > 0
    assert not out_dir.exists()


def test_export_workflow_dry_run_returns_paths(tmp_path):
    obj_dir = _write_objects(tmp_path, _minimal_md())
    written = export_workflow(obj_dir, tmp_path / "out", dry_run=True)
    assert any("openclaw.json" in p for p in written)
    assert any("AGENTS.md" in p for p in written)


def test_export_workflow_multi_object_allow_list(tmp_path):
    obj_dir = _write_objects(tmp_path, _minimal_md("Guest Manager"), _minimal_md("Billing"))
    out_dir = tmp_path / "out"
    export_workflow(obj_dir, out_dir)
    config = json.loads((out_dir / "openclaw.json").read_text())
    allow = config["tools"]["agentToAgent"]["allow"]
    assert "guest-manager" in allow
    assert "billing" in allow


# ---------------------------------------------------------------------------
# apply_modification
# ---------------------------------------------------------------------------

def test_apply_modification_role(tmp_path):
    obj_dir = _write_objects(tmp_path, _minimal_md())
    out_dir = tmp_path / "out"
    export_workflow(obj_dir, out_dir)
    path = apply_modification("guest-manager", "role", "New role description.", out_dir)
    content = Path(path).read_text()
    assert "New role description." in content


def test_apply_modification_behavior(tmp_path):
    obj_dir = _write_objects(tmp_path, _full_md())
    out_dir = tmp_path / "out"
    export_workflow(obj_dir, out_dir)
    apply_modification("quote-approvals", "behavior", "Updated behavior rules.", out_dir)
    content = (out_dir / "workspace-quote-approvals" / "AGENTS.md").read_text()
    assert "Updated behavior rules." in content


def test_apply_modification_missing_workspace_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        apply_modification("nonexistent", "role", "x", tmp_path)


def test_apply_modification_invalid_field_raises(tmp_path):
    obj_dir = _write_objects(tmp_path, _minimal_md())
    out_dir = tmp_path / "out"
    export_workflow(obj_dir, out_dir)
    with pytest.raises(ValueError, match="Unsupported field"):
        apply_modification("guest-manager", "subscriptions", "x", out_dir)
