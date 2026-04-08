"""MD parser — parse and serialize LLM-object definitions in markdown format."""
from __future__ import annotations

import re
from pathlib import Path

from .types import ObjectDefinition, PeerDeclaration


def slugify(name: str) -> str:
    """Convert a heading like 'Guest Manager' to 'guest-manager'."""
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def parse_object_text(text: str) -> ObjectDefinition:
    """Parse markdown text into an ObjectDefinition."""
    lines = text.strip().split("\n")

    # Find H1 heading
    object_id = None
    h1_line = None
    for i, line in enumerate(lines):
        m = re.match(r"^#\s+(.+)$", line)
        if m:
            object_id = slugify(m.group(1))
            h1_line = i
            break

    if object_id is None:
        raise ValueError("Missing H1 heading (# Object Name)")

    # Parse H2 sections
    sections: dict[str, str] = {}
    current_section = None
    section_lines: list[str] = []

    for line in lines[h1_line + 1 :]:
        m = re.match(r"^##\s+(.+)$", line)
        if m:
            if current_section is not None:
                sections[current_section] = "\n".join(section_lines).strip()
            current_section = m.group(1).strip().lower()
            section_lines = []
        elif current_section is not None:
            section_lines.append(line)

    if current_section is not None:
        sections[current_section] = "\n".join(section_lines).strip()

    # Role is required
    role = sections.get("role", "")
    if not role:
        raise ValueError("Missing required '## Role' section")

    # Parse peers
    peers: list[PeerDeclaration] = []
    if "peers" in sections:
        for line in sections["peers"].split("\n"):
            m = re.match(r"^\s*-\s*(\S+):\s*(.+)$", line)
            if m:
                peers.append(PeerDeclaration(object_id=m.group(1), relationship=m.group(2).strip()))

    # Parse bullet lists
    def _parse_bullets(text: str) -> list[str]:
        items = []
        for line in text.split("\n"):
            m = re.match(r"^\s*-\s+(.+)$", line)
            if m:
                items.append(m.group(1).strip())
        return items

    skills = _parse_bullets(sections.get("skills", ""))
    subscriptions = _parse_bullets(sections.get("subscriptions", ""))
    event_sources = _parse_bullets(sections.get("event sources", ""))

    return ObjectDefinition(
        object_id=object_id,
        role=role,
        behavior=sections.get("behavior", ""),
        peers=peers,
        skills=skills,
        subscriptions=subscriptions,
        event_sources=event_sources,
        initial_state=sections.get("state", ""),
    )


def parse_object_file(path: str | Path) -> ObjectDefinition:
    """Parse an MD file into an ObjectDefinition."""
    return parse_object_text(Path(path).read_text())


def serialize_object(defn: ObjectDefinition) -> str:
    """Serialize an ObjectDefinition back to markdown."""
    # Convert object_id back to title case
    title = defn.object_id.replace("-", " ").title()
    parts = [f"# {title}"]

    parts.append(f"\n## Role\n\n{defn.role}")

    if defn.behavior:
        parts.append(f"\n## Behavior\n\n{defn.behavior}")

    if defn.peers:
        peer_lines = [f"- {p.object_id}: {p.relationship}" for p in defn.peers]
        parts.append(f"\n## Peers\n\n" + "\n".join(peer_lines))

    if defn.skills:
        parts.append(f"\n## Skills\n\n" + "\n".join(f"- {s}" for s in defn.skills))

    if defn.subscriptions:
        parts.append(f"\n## Subscriptions\n\n" + "\n".join(f"- {s}" for s in defn.subscriptions))

    if defn.event_sources:
        parts.append(f"\n## Event Sources\n\n" + "\n".join(f"- {s}" for s in defn.event_sources))

    return "\n".join(parts) + "\n"
