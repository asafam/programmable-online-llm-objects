"""
Shim — re-exports MockServer and supporting symbols from mock/server.py.

Evaluation-layer helpers (resolve_mock_configs, merge_tc_mock_tools) live here
because they depend on Sample and MockToolDef from src/data/schema.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import yaml

# Bring mock/ onto sys.path so mock/server.py can resolve `from schema import ...`
_mock_dir = str(Path(__file__).parent.parent.parent / "mock")
if _mock_dir not in sys.path:
    sys.path.insert(0, _mock_dir)

from mock.server import (  # noqa: E402
    MockServer,
    _trigger_matches,
    _ServerState,
    load_orchestration_file,
)
from src.data.schema import (
    MockCallback,
    MockImmediateResponse,
    MockMethodDef,
    MockScript,
    MockSystemDef,
    MockToolDef,
    OrchestratorReaction,
    OrchestratorScript,
    OrchestratorTrigger,
    EventTrigger,
    Sample,
)

__all__ = [
    "MockServer",
    "load_orchestration_file",
    "resolve_mock_configs",
    "resolve_orchestration",
    "merge_tc_mock_tools",
    # schema re-exports for backward compat
    "MockScript",
    "MockSystemDef",
    "MockMethodDef",
    "MockImmediateResponse",
    "MockCallback",
    "OrchestratorScript",
    "OrchestratorTrigger",
    "OrchestratorReaction",
]

# ── Keyword → config file mapping ─────────────────────────────────────────────

_SYSTEM_KEYWORDS: dict[str, str] = {
    "slack": "slack.yaml",
    "email": "email.yaml",
    "gmail": "email.yaml",
    "jira": "jira.yaml",
    "webhook": "generic_webhook.yaml",
    "zapier": "zapier.yaml",
    "google calendar": "google_calendar.yaml",
    "calendar": "google_calendar.yaml",
    "stripe": "stripe.yaml",
    "monday": "monday.yaml",
    "salesforce": "salesforce.yaml",
    "airtable": "airtable.yaml",
    "hubspot": "hubspot.yaml",
    "github": "github.yaml",
    "google sheets": "google_sheets.yaml",
    "spreadsheet": "google_sheets.yaml",
    "asana": "asana.yaml",
    "notion": "notion.yaml",
    "twilio": "twilio.yaml",
    "openai": "openai.yaml",
    "chatgpt": "openai.yaml",
    "google drive": "google_drive.yaml",
    "zendesk": "zendesk.yaml",
    "intercom": "intercom.yaml",
    "pipedrive": "pipedrive.yaml",
    "google contacts": "google_contacts.yaml",
    "clickup": "clickup.yaml",
    "docusign": "docusign.yaml",
}

# Canonical source of truth lives in mock/config/
_MOCKS_DIR = Path(__file__).parent.parent.parent / "mock" / "config"


def resolve_orchestration(tc: Sample, time_scale: float = 1.0) -> Optional[OrchestratorScript]:
    """Build an OrchestratorScript from a Sample's events whose triggered_by is an EventTrigger.

    Events sharing the same (tool, match) key are merged into a single OrchestratorTrigger
    with multiple reactions. Returns None if no event has an EventTrigger.
    """
    # key: (tool, frozenset of match items) → (trigger_def, list of reactions)
    trigger_map: dict[tuple, tuple[EventTrigger, list[OrchestratorReaction]]] = {}

    for event in tc.events:
        if not isinstance(event.triggered_by, EventTrigger):
            continue
        et = event.triggered_by
        key = (et.tool, frozenset(et.match.items()))
        reaction = OrchestratorReaction(
            source=event.source,
            message=event.input,
            after_seconds=et.after_seconds,
            after_minutes=et.after_minutes,
        )
        if key not in trigger_map:
            trigger_map[key] = (et, [reaction])
        else:
            trigger_map[key][1].append(reaction)

    if not trigger_map:
        return None

    triggers = [
        OrchestratorTrigger(tool=et.tool, match=et.match, reactions=reactions)
        for et, reactions in trigger_map.values()
    ]
    return OrchestratorScript(name="tc_orchestration", time_scale=time_scale, triggers=triggers)


def resolve_mock_configs(tc: Sample) -> Optional[MockScript]:
    """Scan a Sample's object fields and expects for known system keywords.

    Scans skills, event_sources, behavior, role, and step/event expect.action
    so that systems demanded by expects (even if not named in object text) are
    included in the mock script.

    Returns a merged MockScript if any matching config files exist, else None.
    """
    found: dict[str, MockSystemDef] = {}

    all_text: list[str] = []
    for obj in tc.objects:
        all_text.extend(obj.skills)
        all_text.extend(obj.event_sources)
        if obj.behavior:
            all_text.append(obj.behavior)
        if obj.role:
            all_text.append(obj.role)

    # Also scan step and event expects so that systems demanded by expects are
    # covered even when object text doesn't explicitly name the system.
    for step in tc.steps:
        if step.expect and step.expect.action:
            all_text.append(step.expect.action)
    for event in tc.events:
        if event.expect and event.expect.action:
            all_text.append(event.expect.action)

    combined = " ".join(all_text).lower()

    for keyword, filename in _SYSTEM_KEYWORDS.items():
        if keyword in combined:
            config_path = _MOCKS_DIR / filename
            if config_path.exists() and keyword not in found:
                with open(config_path) as f:
                    data = yaml.safe_load(f)
                sys_def = MockSystemDef(**data)
                found[sys_def.system] = sys_def

    if not found:
        return None

    return MockScript(systems=list(found.values()))


def merge_tc_mock_tools(
    script: Optional[MockScript],
    tc_mock_tools: list[MockToolDef],
) -> Optional[MockScript]:
    """Merge per-TC MockToolDef entries into a MockScript for the baseline mock server.

    MockToolDef (LNL in-process mock) and MockMethodDef (baseline HTTP mock) share
    the same response data; only the wrapper schema differs. This converts each
    MockToolDef to a MockMethodDef (response_template → immediate.template) and
    injects them into a synthetic system group in the script.

    Per-TC tool definitions WIN over same-named entries from the static YAML configs,
    ensuring the seeded reference data (org directories, approval policies, etc.) is
    used consistently by both the LNL and baseline evaluations.
    """
    if not tc_mock_tools:
        return script

    tc_methods: dict[str, MockMethodDef] = {
        t.tool_name: MockMethodDef(
            method=t.tool_name,
            immediate=MockImmediateResponse(template=t.response_template),
        )
        for t in tc_mock_tools
    }

    if script is None:
        return MockScript(systems=[MockSystemDef(system="tc_mock_tools", tools=list(tc_methods.values()))])

    overridden: set[str] = set()
    new_systems = []
    for sys_def in script.systems:
        new_tools = []
        for m in sys_def.tools:
            if m.method in tc_methods:
                new_tools.append(tc_methods[m.method])
                overridden.add(m.method)
            else:
                new_tools.append(m)
        new_systems.append(MockSystemDef(system=sys_def.system, tools=new_tools))

    remaining = [m for name, m in tc_methods.items() if name not in overridden]
    if remaining:
        new_systems.append(MockSystemDef(system="tc_mock_tools", tools=remaining))

    return MockScript(systems=new_systems)
