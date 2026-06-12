"""
OpenClaw export — convert LNL workflow definitions into a wired OpenClaw
multi-agent configuration directory.

Usage:
    python -m src.lnl.openclaw_export \\
        --input scenarios/service-query/objects/ \\
        --output ~/.openclaw/
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .parser import parse_object_file, slugify
from .types import ObjectDefinition


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slug_to_name(object_id: str) -> str:
    """Convert 'guest-manager' → 'Guest Manager'."""
    return object_id.replace("-", " ").title()


def _workspace_dir(output_dir: Path, object_id: str) -> Path:
    return output_dir / f"workspace-{object_id}"


def _agent_dir(output_dir: Path, object_id: str) -> Path:
    return output_dir / "agents" / object_id / "agent"


# ---------------------------------------------------------------------------
# Event source classification
# ---------------------------------------------------------------------------

@dataclass
class EventSourceBinding:
    descriptor: str
    kind: str           # "webhook" | "cron" | "unknown"
    cron_expr: str = ""
    webhook_name: str = ""
    warning: str = ""


_CRON_PATTERNS = [
    # "daily at 9am" / "daily at 10pm"
    (re.compile(r"daily\s+at\s+(\d+)\s*am", re.I), lambda m: f"0 {int(m.group(1))} * * *"),
    (re.compile(r"daily\s+at\s+(\d+)\s*pm", re.I), lambda m: f"0 {int(m.group(1)) + 12} * * *"),
    (re.compile(r"daily\s+at\s+(\d+):(\d+)\s*am", re.I), lambda m: f"{int(m.group(2))} {int(m.group(1))} * * *"),
    (re.compile(r"daily\s+at\s+(\d+):(\d+)\s*pm", re.I), lambda m: f"{int(m.group(2))} {int(m.group(1)) + 12} * * *"),
    # "every N minutes"
    (re.compile(r"every\s+(\d+)\s+minutes?", re.I), lambda m: f"*/{m.group(1)} * * * *"),
    # "hourly"
    (re.compile(r"^hourly$", re.I), lambda _: "0 * * * *"),
    # "weekly on <day>"
    (re.compile(r"weekly\s+on\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", re.I),
     lambda m: f"0 9 * * {['monday','tuesday','wednesday','thursday','friday','saturday','sunday'].index(m.group(1).lower())}"),
]


def _parse_cron_schedule(nl_schedule: str) -> tuple[str, str]:
    """Return (cron_expr, warning). warning is non-empty if unresolved."""
    for pattern, formatter in _CRON_PATTERNS:
        m = pattern.search(nl_schedule)
        if m:
            return formatter(m), ""
    return "", f"unresolved cron schedule: {nl_schedule!r}"


def _classify_event_source(descriptor: str) -> EventSourceBinding:
    """Classify an event_sources descriptor string."""
    # Cron: explicit prefix
    m = re.match(r"^cron:\s+(.+)$", descriptor, re.I)
    if m:
        cron_expr, warning = _parse_cron_schedule(m.group(1))
        return EventSourceBinding(
            descriptor=descriptor,
            kind="cron",
            cron_expr=cron_expr,
            warning=warning,
        )
    # Webhook: substring match
    if re.search(r"webhook", descriptor, re.I):
        return EventSourceBinding(
            descriptor=descriptor,
            kind="webhook",
            webhook_name=descriptor,
        )
    # Unknown
    return EventSourceBinding(
        descriptor=descriptor,
        kind="unknown",
        warning=f"unresolved event source: {descriptor!r}",
    )


# ---------------------------------------------------------------------------
# File content builders
# ---------------------------------------------------------------------------

def _agents_md(
    obj: ObjectDefinition,
    session_name: str = "main",
    peer_message_timeout: float = 0.0,
    leaf_peer_ids: "frozenset[str]" = frozenset(),
) -> str:
    name = _slug_to_name(obj.object_id)
    behavior = obj.behavior or "(No specific behavior defined.)"
    # Render timeout as int when whole (0, 90) to keep the prompt clean.
    _t = peer_message_timeout
    timeout_str = str(int(_t)) if float(_t).is_integer() else str(_t)
    if obj.peers:
        peers_block = "\n".join(f"- **{p.object_id}**: {p.relationship}" for p in obj.peers)
        peer_ids = [p.object_id for p in obj.peers]

        # Per-peer timeout: leaf write-service peers (no peers of their own) use
        # timeoutSeconds=0 so the cascade doesn't compound.  Coordinator peers
        # (those with their own downstream peers) keep peer_message_timeout so
        # the caller waits for their full sub-cascade to complete.
        def _pt(pid: str) -> float:
            return 0.0 if pid in leaf_peer_ids else _t

        def _pt_str(pid: str) -> str:
            t = _pt(pid)
            return str(int(t)) if float(t).is_integer() else str(t)

        # Peer session keys always use "main" — the gateway's default mainKey.
        # The gateway auto-creates sessions on demand for the mainKey when
        # sessions_send targets a peer. Custom session names (e.g. "eval-ma-6")
        # do NOT get auto-created and cause "pairing required" errors.
        peer_examples = "\n".join(
            f'  - To message `{pid}`: `sessions_send(sessionKey="agent:{pid}:main", message="<your message>", timeoutSeconds={_pt_str(pid)})`'
            for pid in peer_ids
        )

        has_ff = any(_pt(pid) == 0 for pid in peer_ids)
        has_wait = any(_pt(pid) > 0 for pid in peer_ids)
        if has_ff and has_wait:
            timeout_instruction = (
                f"**Use the exact timeoutSeconds shown for each peer below.** "
                f"Peers with `timeoutSeconds=0` are write services — fire-and-forget: send the "
                f"message and continue immediately without waiting for a reply. "
                f"Peers with `timeoutSeconds={timeout_str}` are coordinators — they orchestrate "
                f"further work; always wait for their reply.\n\n"
            )
        elif has_ff:
            timeout_instruction = (
                f"**ALWAYS include `timeoutSeconds=0`** — this is fire-and-forget: the message is "
                f"enqueued and you return immediately without waiting for a reply. Do not wait for "
                f"or expect a response from peers; continue with your remaining work.\n\n"
            )
        else:
            timeout_instruction = (
                f"**ALWAYS include `timeoutSeconds={timeout_str}`** — peers may need to make multiple "
                f"tool calls before responding. Never omit this parameter.\n\n"
            )
        comm_section = (
            f"## Communication\n\n"
            f"To send a message to a peer agent, use the `sessions_send` tool with the exact sessionKey below.\n"
            f"**Do NOT use the `message` tool** — that is for external channels (Slack, email, etc.).\n"
            f"{timeout_instruction}"
            f"Exact calls for each peer:\n\n"
            f"{peer_examples}\n\n"
            f"**For actions YOUR behavior defines** (writing a record, sending a notification, etc.),\n"
            f"call the tool in this response — don't forward that responsibility to a peer.\n"
            f"If your behavior says to write to Zapier Tables AND then notify a peer, do both:\n"
            f"call `zapier_tables_create_record` yourself, then `sessions_send` to the peer.\n"
            f"Only delegate to a peer the actions that peer's behavior owns.\n"
        )
    else:
        peers_block = "(No peers defined.)"
        comm_section = (
            f"## Communication\n\n"
            f"This agent has no peers. Call the available external tools directly for all actions.\n"
            f"Never describe an action without calling the tool — if your behavior says to write a\n"
            f"record or send a message, do it now by calling the tool.\n"
        )

    tools_section = (
        "## Available Tools\n\n"
        "Use these tools for any external system action. Call them directly — do not describe them:\n"
        "- `slack_send_message(channel, message)` — post a message to a Slack channel\n"
        "- `slack_list_channels()` — list Slack channels\n"
        "- `slack_add_reaction(message_id, emoji)` — add a reaction to a Slack message\n"
        "- `slack_get_user(user)` — get Slack user info\n"
        "- `zapier_tables_create_record(table, data)` — write a record to a Zapier Table\n"
        "- `zapier_tables_list_records(table, filter)` — read records from a Zapier Table\n"
        "- `email_send(to, subject, body)` — send an email\n"
        "- `email_list_inbox(folder)` — list emails in inbox\n"
        "- `email_read(message_id)` — read an email\n"
        "- `jira_create_issue(project, summary, description)` — create a Jira issue\n"
        "- `jira_update_issue(issue_id, status)` — update a Jira issue\n"
        "- `jira_get_issue(issue_id)` — get a Jira issue\n"
        "- `jira_list_issues(project, status)` — list Jira issues\n"
        "- `webhook_post(url, payload)` — call an external webhook\n"
        "- `calendar_create_event(title, start, end, attendees, description)` — create a calendar event\n"
        "- `calendar_update_event(event_id, title, start, end)` — update a calendar event\n"
        "- `calendar_get_event(event_id)` — get a calendar event\n"
        "- `calendar_list_events(calendar_id, time_min, time_max)` — list calendar events\n"
        "- `stripe_create_charge(amount, currency, customer, description)` — create a Stripe charge\n"
        "- `stripe_get_charge(charge_id)` — get a Stripe charge\n"
        "- `stripe_list_charges(customer, limit)` — list Stripe charges\n"
        "- `stripe_refund_charge(charge_id, amount)` — refund a Stripe charge\n"
        "- `monday_create_item(board_id, item_name, column_values)` — create a Monday.com item\n"
        "- `monday_update_item(item_id, column_values)` — update a Monday.com item\n"
        "- `monday_get_item(item_id)` — get a Monday.com item\n"
        "- `monday_list_items(board_id)` — list Monday.com items\n"
        "- `salesforce_create_record(object_type, fields)` — create a Salesforce record\n"
        "- `salesforce_update_record(object_type, record_id, fields)` — update a Salesforce record\n"
        "- `salesforce_get_record(object_type, record_id)` — get a Salesforce record\n"
        "- `salesforce_list_records(object_type, filter)` — list Salesforce records\n"
        "- `airtable_create_record(base_id, table, fields)` — create an Airtable record\n"
        "- `airtable_update_record(base_id, table, record_id, fields)` — update an Airtable record\n"
        "- `airtable_get_record(base_id, table, record_id)` — get an Airtable record\n"
        "- `airtable_list_records(base_id, table, filter)` — list Airtable records\n"
        "- `hubspot_create_contact(email, first_name, last_name, properties)` — create a HubSpot contact\n"
        "- `hubspot_update_contact(contact_id, properties)` — update a HubSpot contact\n"
        "- `hubspot_create_deal(deal_name, amount, stage, contact_id)` — create a HubSpot deal\n"
        "- `hubspot_update_deal(deal_id, properties)` — update a HubSpot deal\n"
        "- `hubspot_get_deal(deal_id)` — get a HubSpot deal\n"
        "- `github_create_issue(repo, title, body, labels)` — create a GitHub issue\n"
        "- `github_update_issue(repo, issue_number, title, state, body)` — update a GitHub issue\n"
        "- `github_get_issue(repo, issue_number)` — get a GitHub issue\n"
        "- `github_list_issues(repo, state)` — list GitHub issues\n"
        "- `sheets_create_row(spreadsheet_id, sheet, values)` — append a row to Google Sheets\n"
        "- `sheets_update_row(spreadsheet_id, row, values, sheet)` — update a row in Google Sheets\n"
        "- `sheets_get_row(spreadsheet_id, row, sheet)` — get a row from Google Sheets\n"
        "- `sheets_list_rows(spreadsheet_id, sheet, max_rows)` — list rows from Google Sheets\n"
        "- `asana_create_task(project_id, name, notes, assignee, due_on)` — create an Asana task\n"
        "- `asana_update_task(task_id, name, completed, notes)` — update an Asana task\n"
        "- `asana_get_task(task_id)` — get an Asana task\n"
        "- `asana_list_tasks(project_id, completed)` — list Asana tasks\n"
        "- `notion_create_page(parent_id, title, content, properties)` — create a Notion page\n"
        "- `notion_update_page(page_id, title, properties)` — update a Notion page\n"
        "- `notion_get_page(page_id)` — get a Notion page\n"
        "- `notion_query_database(database_id, filter)` — query a Notion database\n"
        "- `twilio_send_sms(to, message, from)` — send an SMS via Twilio\n"
        "- `twilio_send_message(to, message, channel)` — send a message via Twilio\n\n"
    )

    return (
        f"# Agent: {name}\n\n"
        f"## Role\n\n{obj.role}\n\n"
        f"## Behavior\n\n{behavior}\n\n"
        f"## Peers\n\n{peers_block}\n\n"
        f"## State\n\n"
        f"Your current operational state is tracked in `state.md` in this workspace.\n"
        f"Read it at the start of each interaction to restore context.\n"
        f"After each interaction, write your updated state back to `state.md`.\n\n"
        + tools_section
        + comm_section
    )


def _soul_md(obj: ObjectDefinition) -> str:
    name = _slug_to_name(obj.object_id)
    first_sentence = re.split(r"[.!?]", obj.role)[0].strip()
    return (
        f"# {name}\n\n"
        f"You are {name}, a specialized AI agent in a multi-agent workflow.\n\n"
        f"Your core purpose: {first_sentence}\n\n"
        f"Act with precision, stay within your defined responsibilities, and collaborate\n"
        f"with your peers as declared in AGENTS.md.\n\n"
        f"**INIT PROTOCOL (highest priority — overrides everything below):**\n"
        f"If the entire incoming message is exactly `[SYSTEM:INIT]`, reply with the single\n"
        f"word `ready` and nothing else. Do not read state.md, do not call any tools,\n"
        f"do not write anything. Just reply `ready`.\n\n"
        f"**EXECUTION RULE:** For every action YOUR behavior defines (write a record, send a\n"
        f"message, create a task, etc.), call the corresponding tool in this response — don't\n"
        f"describe it. 'I have logged the record' without calling `zapier_tables_create_record`\n"
        f"is wrong. Actions owned by a downstream peer belong to that peer; call `sessions_send`\n"
        f"to delegate them, not the peer's tool directly.\n"
    )


def _state_md(obj: ObjectDefinition) -> str:
    if obj.initial_state:
        return f"# State\n\n{obj.initial_state}\n"
    return "# State\n\n_Empty. This file is updated at runtime by the agent._\n"


def _skill_stub_md(skill: str, object_id: str) -> str:
    name = skill.replace("-", " ").title()
    return f"# {name}\n\n_Skill definition for {object_id}. Fill in the implementation details._\n"


def _combined_agents_md(objects: list[ObjectDefinition]) -> str:
    """Build a single AGENTS.md covering all objects for single-agent mode."""
    lines = [
        "# Multi-Object Workflow Agent\n\n"
        "You handle a multi-object workflow where each component has a defined role and behavior. "
        "Each message identifies the target object, but a message is never just for that one "
        "object: it STARTS THE WHOLE WORKFLOW. Begin as the target object, then keep executing "
        "every downstream component's behavior in sequence — policy decisions, state reads and "
        "updates in `state.md`, and every write-service tool call — until the event is fully "
        "processed end to end. Never stop after the entry component's logging step: 'forward for "
        "processing' means YOU continue the processing yourself, in this same turn.\n\n"
        "**CRITICAL:** Do NOT use agentToAgent or attempt to message other agents. "
        "You are the only agent. When an object's behavior says to 'forward', 'send', or "
        "'notify' another component, perform that component's behavior yourself and use the "
        "available tools to take its external actions directly "
        "(e.g. call `slack_send_message`, `zapier_tables_create_record`, etc.).\n\n"
        "**STATE:** Each object's state lives in its section of `state.md` — that IS the "
        "canonical store. When a behavior says to read state from a component (a desk, "
        "window, tracker, ledger), read that component's `state.md` section; when it says "
        "to commit/record/increment state, rewrite that section in your updated `state.md`. "
        "NEVER look for component state behind a tool — tools are only for the external "
        "systems listed under Available Tools. If a state section is empty or uninitialized, "
        "bootstrap it exactly as the behavior describes (e.g. fetch the roster via its read "
        "tool, seed the rotation, zero the counts) and proceed — an empty section is a fresh "
        "start, not an error.\n\n"
        "**COMPLETION CONTRACT:** There is no later. Nothing you defer ever happens — no "
        "queue, no background processing, no follow-up turn will finish it. A reply that "
        "says work is 'queued', 'started', 'in progress', or 'being processed' is a FAILED "
        "event. Before ending your reply, verify: every decision the behaviors require for "
        "THIS event has been made, every required write-service tool call has actually been "
        "made in this reply, and every affected state section in `state.md` shows the final "
        "post-event values (updated counts, updated order, recorded outcome) — not a note "
        "that processing is underway.\n\n"
        "## Objects\n",
    ]
    for obj in objects:
        name = _slug_to_name(obj.object_id)
        behavior = obj.behavior or "(No specific behavior defined.)"
        lines.append(f"\n### {name} (`{obj.object_id}`)\n\n")
        lines.append(f"**Role:** {obj.role}\n\n")
        lines.append(f"**Behavior:** {behavior}\n\n")
        if obj.skills:
            lines.append(f"**Skills:** {', '.join(obj.skills)}\n\n")
        lines.append("---\n")

    lines.append(
        "\n## Available Tools\n\n"
        "Use these tools for any external system action:\n"
        "- `slack_send_message(channel, message)` — post a message to a Slack channel\n"
        "- `slack_list_channels()` — list Slack channels\n"
        "- `slack_add_reaction(message_id, emoji)` — add a reaction to a Slack message\n"
        "- `slack_get_user(user)` — get Slack user info\n"
        "- `zapier_tables_create_record(table, data)` — write a record to a Zapier Table\n"
        "- `zapier_tables_list_records(table, filter)` — read records from a Zapier Table\n"
        "- `email_send(to, subject, body)` — send an email\n"
        "- `email_list_inbox(folder)` — list emails in inbox\n"
        "- `email_read(message_id)` — read an email\n"
        "- `jira_create_issue(project, summary, description)` — create a Jira issue\n"
        "- `jira_update_issue(issue_id, status)` — update a Jira issue\n"
        "- `jira_get_issue(issue_id)` — get a Jira issue\n"
        "- `jira_list_issues(project, status)` — list Jira issues\n"
        "- `webhook_post(url, payload)` — call an external webhook\n"
        "- `calendar_create_event(title, start, end, attendees, description)` — create a calendar event\n"
        "- `calendar_update_event(event_id, title, start, end)` — update a calendar event\n"
        "- `calendar_get_event(event_id)` — get a calendar event\n"
        "- `calendar_list_events(calendar_id, time_min, time_max)` — list calendar events\n"
        "- `stripe_create_charge(amount, currency, customer, description)` — create a Stripe charge\n"
        "- `stripe_get_charge(charge_id)` — get a Stripe charge\n"
        "- `stripe_list_charges(customer, limit)` — list Stripe charges\n"
        "- `stripe_refund_charge(charge_id, amount)` — refund a Stripe charge\n"
        "- `monday_create_item(board_id, item_name, column_values)` — create a Monday.com item\n"
        "- `monday_update_item(item_id, column_values)` — update a Monday.com item\n"
        "- `monday_get_item(item_id)` — get a Monday.com item\n"
        "- `monday_list_items(board_id)` — list Monday.com items\n"
        "- `salesforce_create_record(object_type, fields)` — create a Salesforce record\n"
        "- `salesforce_update_record(object_type, record_id, fields)` — update a Salesforce record\n"
        "- `salesforce_get_record(object_type, record_id)` — get a Salesforce record\n"
        "- `salesforce_list_records(object_type, filter)` — list Salesforce records\n"
        "- `airtable_create_record(base_id, table, fields)` — create an Airtable record\n"
        "- `airtable_update_record(base_id, table, record_id, fields)` — update an Airtable record\n"
        "- `airtable_get_record(base_id, table, record_id)` — get an Airtable record\n"
        "- `airtable_list_records(base_id, table, filter)` — list Airtable records\n"
        "- `hubspot_create_contact(email, first_name, last_name, properties)` — create a HubSpot contact\n"
        "- `hubspot_update_contact(contact_id, properties)` — update a HubSpot contact\n"
        "- `hubspot_create_deal(deal_name, amount, stage, contact_id)` — create a HubSpot deal\n"
        "- `hubspot_update_deal(deal_id, properties)` — update a HubSpot deal\n"
        "- `hubspot_get_deal(deal_id)` — get a HubSpot deal\n"
        "- `github_create_issue(repo, title, body, labels)` — create a GitHub issue\n"
        "- `github_update_issue(repo, issue_number, title, state, body)` — update a GitHub issue\n"
        "- `github_get_issue(repo, issue_number)` — get a GitHub issue\n"
        "- `github_list_issues(repo, state)` — list GitHub issues\n"
        "- `sheets_create_row(spreadsheet_id, sheet, values)` — append a row to Google Sheets\n"
        "- `sheets_update_row(spreadsheet_id, row, values, sheet)` — update a row in Google Sheets\n"
        "- `sheets_get_row(spreadsheet_id, row, sheet)` — get a row from Google Sheets\n"
        "- `sheets_list_rows(spreadsheet_id, sheet, max_rows)` — list rows from Google Sheets\n"
        "- `asana_create_task(project_id, name, notes, assignee, due_on)` — create an Asana task\n"
        "- `asana_update_task(task_id, name, completed, notes)` — update an Asana task\n"
        "- `asana_get_task(task_id)` — get an Asana task\n"
        "- `asana_list_tasks(project_id, completed)` — list Asana tasks\n"
        "- `notion_create_page(parent_id, title, content, properties)` — create a Notion page\n"
        "- `notion_update_page(page_id, title, properties)` — update a Notion page\n"
        "- `notion_get_page(page_id)` — get a Notion page\n"
        "- `notion_query_database(database_id, filter)` — query a Notion database\n"
        "- `twilio_send_sms(to, message, from)` — send an SMS via Twilio\n"
        "- `twilio_send_message(to, message, channel)` — send a message via Twilio\n\n"
        "## State\n\n"
        "Your combined state for all objects is in `state.md`.\n"
        "Read it at the start of each interaction and update it after.\n\n"
        "## Instructions\n\n"
        "- Identify the target object from the message prefix\n"
        "- Act as that object: execute its responsibilities\n"
        "- **NEVER use agentToAgent.** You are the only agent.\n"
        "- When behavior says to 'forward', 'send to', or 'notify' another component: "
        "trace through that component's behavior yourself right now, "
        "and call its external tools directly. "
        "Example: if zapier-tables forwards to content-curator which then sends to Slack, "
        "you must call `slack_send_message` with the right channel in this same response.\n"
        "- Update the relevant state sections in `state.md` after each interaction\n"
    )
    return "".join(lines)


def _bootstrap_stub_md() -> str:
    """Stub BOOTSTRAP.md that suppresses the gateway's onboarding flow.

    The gateway creates BOOTSTRAP.md for agents with empty IDENTITY.md, making
    them ask "Who am I?" and override SOUL.md.  Writing our own stub + a
    populated IDENTITY.md prevents that trigger entirely.
    """
    return (
        "# BOOTSTRAP.md\n\n"
        "Identity and behavior are fully configured in SOUL.md.\n"
        "Skip onboarding — follow SOUL.md instructions directly.\n"
    )


def _identity_md(obj: ObjectDefinition) -> str:
    """Populated IDENTITY.md so the gateway does not trigger its onboarding flow.

    The gateway creates BOOTSTRAP.md only for agents whose IDENTITY.md has an
    empty Name field.  Pre-populating name + vibe prevents that trigger.
    """
    name = _slug_to_name(obj.object_id)
    return (
        f"# IDENTITY.md - Who Am I?\n\n"
        f"- **Name:** {name}\n"
        f"- **Creature:** AI workflow agent\n"
        f"- **Vibe:** precise, reliable, task-focused\n"
        f"- **Emoji:** 🤖\n"
        f"- **Avatar:** \n"
    )


def _combined_soul_md(objects: list[ObjectDefinition]) -> str:
    names = ", ".join(_slug_to_name(obj.object_id) for obj in objects)
    return (
        "# Multi-Object Workflow Agent\n\n"
        f"You are a unified agent managing a workflow with these components: {names}.\n\n"
        "Your purpose: correctly handle events addressed to each component, "
        "acting as that component, using the available tools, and maintaining its state.\n\n"
        "You are the only agent — do NOT use agentToAgent.\n\n"
        "When a component's behavior says to forward to or notify another component, "
        "trace through that component's behavior immediately and call its external tools "
        "(slack_send_message, email_send, jira_create_issue, webhook_post, etc.) in this same response. "
        "Never stop at 'I would forward this' — complete the full chain.\n\n"
        "Act with precision. The target object is always identified in the message prefix.\n"
    )


def _combined_state_md(objects: list[ObjectDefinition]) -> str:
    lines = ["# Combined State\n"]
    for obj in objects:
        name = _slug_to_name(obj.object_id)
        lines.append(f"\n## {name} (`{obj.object_id}`)\n")
        if obj.initial_state:
            lines.append(f"\n{obj.initial_state}\n")
        else:
            lines.append("\n_No initial state._\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# openclaw.json builder
# ---------------------------------------------------------------------------

def _build_openclaw_json(objects: list[ObjectDefinition], output_dir: Path) -> dict:
    agent_list = []
    for obj in objects:
        name = _slug_to_name(obj.object_id)
        agent_list.append({
            "id": obj.object_id,
            "name": name,
            "workspace": str(output_dir / f"workspace-{obj.object_id}"),
            "agentDir": str(output_dir / "agents" / obj.object_id / "agent"),
        })

    all_ids = [obj.object_id for obj in objects]

    return {
        "agents": {"list": agent_list},
        "tools": {
            "agentToAgent": {
                "enabled": True,
                "allow": all_ids,
            }
        },
        "gateway": {
            # Trust loopback + Docker bridge so inter-agent sessions_send routing
            # isn't blocked by the WS handshake hardening (issue #21236).
            "trustedProxies": ["127.0.0.1", "::1", "172.16.0.0/12"],
        },
    }


# ---------------------------------------------------------------------------
# File writer (handles dry_run / force)
# ---------------------------------------------------------------------------

def _write_file(
    path: Path,
    content: str,
    written: list[str],
    *,
    force: bool,
    dry_run: bool,
) -> None:
    if not dry_run:
        if path.exists() and not force:
            raise FileExistsError(
                f"{path} already exists. Use --force to overwrite."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        # Pool mode: workspace files live in a bind-mount the container's
        # `node` user (UID 1000) updates in place (state.md after each agent
        # turn). Without world-write, the container hits EACCES and the agent
        # turn fails.
        try:
            import os as _os
            _os.chmod(path, 0o666)
            _os.chmod(path.parent, 0o777)
        except OSError:
            pass
    written.append(str(path))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_workflow(
    object_dir: str | Path,
    output_dir: str | Path,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> list[str]:
    """Read all .md files from object_dir, generate OpenClaw config in output_dir.

    Args:
        object_dir: Directory containing LNL .md object files.
        output_dir: Root output directory (e.g. ~/.openclaw/).
        force: Overwrite existing files. Default raises on conflict.
        dry_run: Return what would be written without touching disk.

    Returns:
        List of file paths written (or that would be written in dry_run mode).

    Raises:
        FileNotFoundError: If object_dir does not exist.
        ValueError: If no .md files found or any file fails to parse.
        FileExistsError: If output conflicts and force=False.
    """
    object_dir = Path(object_dir)
    if not object_dir.exists():
        raise FileNotFoundError(f"object_dir does not exist: {object_dir}")
    objects = _load_objects(object_dir)
    return _export_objects_to_dir(objects, Path(output_dir), str(object_dir.resolve()), force=force, dry_run=dry_run)


def export_workflow_from_objects(
    objects: list,
    output_dir: str | Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    write_config: bool = True,
) -> list[str]:
    """Export from in-memory objects (list[ObjectDef] from schema.py or ObjectDefinition).

    Accepts either Pydantic ObjectDef instances (from Sample.objects) or
    dataclass ObjectDefinition instances. Converts automatically.

    Args:
        objects: List of object definitions.
        output_dir: Root output directory (e.g. ~/.openclaw/).
        force: Overwrite existing files. Default raises on conflict.
        dry_run: Return what would be written without touching disk.

    Returns:
        List of file paths written (or that would be written in dry_run mode).
    """
    from src.data.schema import ObjectDef, to_lnl_definition
    obj_defs = [
        to_lnl_definition(o) if isinstance(o, ObjectDef) else o
        for o in objects
    ]
    return _export_objects_to_dir(obj_defs, Path(output_dir), "in-memory", force=force, dry_run=dry_run, write_config=write_config)


def reset_agent_state(object_id: str, initial_state: str, output_dir: str | Path) -> None:
    """Reset state.md for an agent to its initial state before a test run.

    Args:
        object_id: Target agent slug (e.g., "guest-manager").
        initial_state: Initial state text (from ObjectDef.state_description).
        output_dir: Root OpenClaw output directory.

    Raises:
        FileNotFoundError: If the workspace directory does not exist.
    """
    output_dir = Path(output_dir)
    ws = _workspace_dir(output_dir, object_id)
    if not ws.exists():
        raise FileNotFoundError(f"Workspace not found: {ws}")
    state_file = ws / "state.md"
    if initial_state:
        state_file.write_text(f"# State\n\n{initial_state}\n")
    else:
        state_file.write_text("# State\n\n_Empty. This file is updated at runtime by the agent._\n")
    try:
        import os as _os
        _os.chmod(state_file, 0o666)
    except OSError:
        pass


def rewrite_agents_md(
    objects: list,
    output_dir: "str | Path",
    session_name: str,
    *,
    slot_suffix: str = "",
    peer_message_timeout: float = 0.0,
) -> None:
    """Rewrite only AGENTS.md for each object workspace with the given session_name.

    Called before each multi-agent TC run so peer sessionKey refs in AGENTS.md match
    the actual session name opened by the evaluator. Does NOT touch state.md or SOUL.md.

    Args:
        objects: list[ObjectDef] (Pydantic) or list[ObjectDefinition] (dataclass).
        output_dir: Root OpenClaw home directory (e.g. ~/.openclaw).
        session_name: Unique session name for this run (e.g. "eval-ma-42").
        slot_suffix: Appended to workspace dir name and peer agent IDs for concurrent
                     slots (e.g. "-c1"). Empty string for slot 0 (default workspaces).
        peer_message_timeout: timeoutSeconds value embedded in the sessions_send peer
                     examples. 0 = fire-and-forget (enqueue and return immediately).
    """
    from dataclasses import replace
    from pathlib import Path as _Path
    output_dir = _Path(output_dir)

    try:
        from src.data.schema import ObjectDef, to_lnl_definition
        obj_defs = [to_lnl_definition(o) if isinstance(o, ObjectDef) else o for o in objects]
    except ImportError:
        obj_defs = list(objects)

    # Leaf peers are write-service objects that have no peers of their own.
    # They are called fire-and-forget (timeoutSeconds=0) so the cascade does
    # not compound: if a brain agent calls two leaf write services sequentially
    # with timeoutSeconds=150 each, the chain takes 300s — exceeding the 150s
    # budget of the parent that called the brain agent.
    leaf_ids_base: set[str] = {o.object_id for o in obj_defs if not o.peers}
    leaf_ids: frozenset[str] = frozenset(
        f"{oid}{slot_suffix}" for oid in leaf_ids_base
    )

    for obj in obj_defs:
        ws = output_dir / f"workspace-{obj.object_id}{slot_suffix}"
        target = ws / "AGENTS.md"
        if not ws.exists():
            continue
        if slot_suffix:
            # Rewrite peer IDs with slot suffix so sessionKey refs are slot-correct
            slotted_peers = [
                replace(p, object_id=f"{p.object_id}{slot_suffix}")
                for p in obj.peers
            ]
            slotted_obj = replace(obj, peers=slotted_peers)
        else:
            slotted_obj = obj
        target.write_text(_agents_md(
            slotted_obj,
            session_name=session_name,
            peer_message_timeout=peer_message_timeout,
            leaf_peer_ids=leaf_ids,
        ))


def _export_objects_to_dir(
    objects: list[ObjectDefinition],
    output_dir: Path,
    source_label: str,
    *,
    force: bool,
    dry_run: bool,
    write_config: bool = True,
) -> list[str]:
    """Internal: write all OpenClaw workspace files for a list of ObjectDefinitions."""
    written: list[str] = []

    if write_config:
        config = _build_openclaw_json(objects, output_dir)
        _write_file(
            output_dir / "openclaw.json",
            json.dumps(config, indent=2) + "\n",
            written,
            force=force,
            dry_run=dry_run,
        )

    for obj in objects:
        ws = _workspace_dir(output_dir, obj.object_id)

        _write_file(ws / "AGENTS.md", _agents_md(obj), written, force=force, dry_run=dry_run)
        _write_file(ws / "SOUL.md", _soul_md(obj), written, force=force, dry_run=dry_run)
        _write_file(ws / "state.md", _state_md(obj), written, force=force, dry_run=dry_run)
        # Always overwrite IDENTITY.md and BOOTSTRAP.md (force=True).
        # The gateway creates BOOTSTRAP.md for agents whose IDENTITY.md has an empty
        # Name field, making them say "Hey, who am I?" instead of executing.
        # A populated IDENTITY.md + our stub BOOTSTRAP.md together suppress that trigger.
        _write_file(ws / "IDENTITY.md", _identity_md(obj), written, force=True, dry_run=dry_run)
        _write_file(ws / "BOOTSTRAP.md", _bootstrap_stub_md(), written, force=True, dry_run=dry_run)

        for skill in obj.skills:
            skill_slug = slugify(skill)
            _write_file(
                ws / "skills" / f"{skill_slug}.md",
                _skill_stub_md(skill, obj.object_id),
                written,
                force=force,
                dry_run=dry_run,
            )

        ad = _agent_dir(output_dir, obj.object_id)
        if not dry_run:
            try:
                ad.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                pass  # container owns agents/; gateway creates agentDir on first use
        written.append(str(ad) + "/")

    return written


def export_single_agent_workspace(
    objects: list,
    output_dir: str | Path,
    agent_id: str = "lnl-eval",
    *,
    force: bool = False,
) -> str:
    """Export all objects as a single combined agent workspace.

    Writes AGENTS.md, SOUL.md, and state.md to a shared workspace directory
    so one OpenClaw agent can handle messages for any object.

    Args:
        objects: List of ObjectDef or ObjectDefinition instances.
        output_dir: Root OpenClaw directory (e.g. ~/.openclaw/).
        agent_id: ID for the combined agent (default: "lnl-eval").
        force: Overwrite existing workspace files.

    Returns:
        Workspace directory path.
    """
    from src.data.schema import ObjectDef, to_lnl_definition
    obj_defs = [
        to_lnl_definition(o) if isinstance(o, ObjectDef) else o
        for o in objects
    ]
    output_dir = Path(output_dir)
    ws = output_dir / f"workspace-{agent_id}"
    ws.mkdir(parents=True, exist_ok=True)

    def _write(path: Path, content: str) -> None:
        if path.exists() and not force:
            pass  # skip — caller uses force=True for eval runs
        path.write_text(content)
        try:
            import os as _os
            _os.chmod(path, 0o666)
        except OSError:
            pass

    _write(ws / "AGENTS.md", _combined_agents_md(obj_defs))
    _write(ws / "SOUL.md", _combined_soul_md(obj_defs))
    _write(ws / "state.md", _combined_state_md(obj_defs))

    # Ensure the agentDir exists (OpenClaw needs it for auth profiles etc.)
    ad = output_dir / "agents" / agent_id / "agent"
    try:
        ad.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        pass  # container owns agents/; gateway creates agentDir on first use

    return str(ws)


def reset_single_agent_state(
    objects: list,
    output_dir: str | Path,
    agent_id: str = "lnl-eval",
) -> None:
    """Reset state.md for the combined single agent to all objects' initial states."""
    from src.data.schema import ObjectDef, to_lnl_definition
    obj_defs = [
        to_lnl_definition(o) if isinstance(o, ObjectDef) else o
        for o in objects
    ]
    ws = Path(output_dir) / f"workspace-{agent_id}"
    ws.mkdir(parents=True, exist_ok=True)
    state_file = ws / "state.md"
    state_file.write_text(_combined_state_md(obj_defs))
    try:
        import os as _os
        _os.chmod(state_file, 0o666)
    except OSError:
        pass


def apply_modification(
    object_id: str,
    field: str,
    value: str | list,
    output_dir: str | Path,
) -> str:
    """Patch a single section in an already-exported agent's AGENTS.md.

    Args:
        object_id: Target agent slug (e.g., "guest-manager").
        field: One of "role", "behavior", "peers".
        value: New value — str for role/behavior, list[str] for peers.
        output_dir: Root OpenClaw output directory.

    Returns:
        Path to the updated AGENTS.md file.

    Raises:
        FileNotFoundError: If workspace or AGENTS.md does not exist.
        ValueError: If field is not supported.
    """
    supported = {"role": "Role", "behavior": "Behavior", "peers": "Peers"}
    if field not in supported:
        raise ValueError(f"Unsupported field {field!r}. Must be one of: {sorted(supported)}")

    output_dir = Path(output_dir)
    agents_md_path = _workspace_dir(output_dir, object_id) / "AGENTS.md"
    if not agents_md_path.exists():
        raise FileNotFoundError(f"AGENTS.md not found: {agents_md_path}")

    section_title = supported[field]
    if isinstance(value, list):
        new_content = "\n".join(value)
    else:
        new_content = str(value)

    text = agents_md_path.read_text()
    pattern = re.compile(
        rf"(## {re.escape(section_title)}\n)(.*?)(?=\n## |\Z)",
        re.DOTALL,
    )
    replacement = rf"\g<1>{new_content}\n"
    new_text, count = pattern.subn(replacement, text)
    if count == 0:
        raise ValueError(f"Section '## {section_title}' not found in {agents_md_path}")

    agents_md_path.write_text(new_text)
    return str(agents_md_path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_objects(object_dir: Path) -> list[ObjectDefinition]:
    """Parse all .md files in object_dir. Raises ValueError on any parse failure."""
    md_files = sorted(object_dir.glob("*.md"))
    if not md_files:
        raise ValueError(f"No .md files found in {object_dir}")
    objects = []
    errors = []
    for f in md_files:
        try:
            objects.append(parse_object_file(f))
        except (ValueError, Exception) as e:
            errors.append(f"{f.name}: {e}")
    if errors:
        raise ValueError("Parse errors:\n" + "\n".join(errors))
    return objects


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lnl-openclaw-export",
        description="Export LNL workflow definitions to OpenClaw multi-agent config",
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        metavar="DIR",
        help="Directory containing .md object files",
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        metavar="DIR",
        help="Output root directory (e.g. ~/.openclaw/)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing output files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without writing anything",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> None:
    args = _build_parser().parse_args(argv)
    input_dir = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()
    written = export_workflow(input_dir, output_dir, force=args.force, dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    for path in written:
        print(f"  {prefix}wrote: {path}")
    print(f"\nExported {len(written)} files.")


if __name__ == "__main__":
    main()
