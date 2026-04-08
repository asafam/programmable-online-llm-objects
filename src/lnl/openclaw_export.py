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

def _agents_md(obj: ObjectDefinition) -> str:
    name = _slug_to_name(obj.object_id)
    behavior = obj.behavior or "(No specific behavior defined.)"
    if obj.peers:
        peers_block = "\n".join(f"- **{p.object_id}**: {p.relationship}" for p in obj.peers)
    else:
        peers_block = "(No peers defined.)"

    return (
        f"# Agent: {name}\n\n"
        f"## Role\n\n{obj.role}\n\n"
        f"## Behavior\n\n{behavior}\n\n"
        f"## Peers\n\n{peers_block}\n\n"
        f"## State\n\n"
        f"Your current operational state is tracked in `state.md` in this workspace.\n"
        f"Read it at the start of each interaction to restore context.\n"
        f"After each interaction, write your updated state back to `state.md`.\n\n"
        f"## Communication\n\n"
        f"You may send messages to peers using the agentToAgent tool.\n"
        f"Send messages only to declared peers above.\n"
    )


def _soul_md(obj: ObjectDefinition) -> str:
    name = _slug_to_name(obj.object_id)
    first_sentence = re.split(r"[.!?]", obj.role)[0].strip()
    return (
        f"# {name}\n\n"
        f"You are {name}, a specialized AI agent in a multi-agent workflow.\n\n"
        f"Your core purpose: {first_sentence}\n\n"
        f"Act with precision, stay within your defined responsibilities, and collaborate\n"
        f"with your peers as declared in AGENTS.md.\n"
    )


def _state_md(obj: ObjectDefinition) -> str:
    if obj.initial_state:
        return f"# State\n\n{obj.initial_state}\n"
    return "# State\n\n_Empty. This file is updated at runtime by the agent._\n"


def _skill_stub_md(skill: str, object_id: str) -> str:
    name = skill.replace("-", " ").title()
    return f"# {name}\n\n_Skill definition for {object_id}. Fill in the implementation details._\n"


# ---------------------------------------------------------------------------
# openclaw.json builder
# ---------------------------------------------------------------------------

def _build_openclaw_json(objects: list[ObjectDefinition], source_dir: Path) -> dict:
    agents = []
    for obj in objects:
        name = _slug_to_name(obj.object_id)
        agents.append({
            "id": obj.object_id,
            "name": name,
            "workspace": f"~/.openclaw/workspace-{obj.object_id}",
            "agentDir": f"~/.openclaw/agents/{obj.object_id}/agent",
        })

    all_ids = [obj.object_id for obj in objects]

    event_bindings = []
    for obj in objects:
        for descriptor in obj.event_sources:
            binding = _classify_event_source(descriptor)
            entry: dict = {"agentId": obj.object_id, "type": binding.kind}
            if binding.kind == "webhook":
                entry["name"] = binding.webhook_name
                entry["channel"] = f"webhook-{slugify(binding.webhook_name)}"
            elif binding.kind == "cron":
                entry["schedule"] = binding.cron_expr
                entry["descriptor"] = descriptor
            else:
                entry["descriptor"] = descriptor
            if binding.warning:
                entry["_warning"] = binding.warning
            event_bindings.append(entry)

    # Collect unhandled subscriptions for meta warning
    unhandled_subs = []
    for obj in objects:
        for sub in obj.subscriptions:
            unhandled_subs.append(f"{obj.object_id}:{sub}")

    meta: dict = {
        "generated_by": "lnl-openclaw-export",
        "source_dir": str(source_dir),
        "object_count": len(objects),
    }
    if unhandled_subs:
        meta["unhandled_subscriptions"] = unhandled_subs

    config: dict = {
        "agents": agents,
        "tools": {
            "agentToAgent": {
                "enabled": True,
                "allow": all_ids,
            }
        },
        "_meta": meta,
    }
    if event_bindings:
        config["eventBindings"] = event_bindings

    return config


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
) -> list[str]:
    """Export from in-memory objects (list[ObjectDef] from schema.py or ObjectDefinition).

    Accepts either Pydantic ObjectDef instances (from TestCase.objects) or
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
    return _export_objects_to_dir(obj_defs, Path(output_dir), "in-memory", force=force, dry_run=dry_run)


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


def _export_objects_to_dir(
    objects: list[ObjectDefinition],
    output_dir: Path,
    source_label: str,
    *,
    force: bool,
    dry_run: bool,
) -> list[str]:
    """Internal: write all OpenClaw workspace files for a list of ObjectDefinitions."""
    written: list[str] = []

    config = _build_openclaw_json(objects, Path(source_label))
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
            ad.mkdir(parents=True, exist_ok=True)
        written.append(str(ad) + "/")

    return written


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
