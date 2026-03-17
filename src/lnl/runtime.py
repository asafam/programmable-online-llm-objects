"""Runtime — library API tying together parser, objects, bus, and brain."""
from __future__ import annotations

from pathlib import Path

from .brain import LLMBrain
from .bus import BusMetrics, MessageBus
from .object import LLMObject
from .parser import parse_object_file, parse_object_text, serialize_object
from .types import (
    Message,
    MessageLog,
    MessageType,
    ObjectDefinition,
    PeerDeclaration,
    ProcessingResult,
)


class Runtime:
    """Single entry point for the LNL runtime."""

    def __init__(
        self,
        brain: LLMBrain,
        max_chain_depth: int = 10,
        strict_peers: bool = True,
    ) -> None:
        self._brain = brain
        self._bus = MessageBus(
            max_chain_depth=max_chain_depth,
            strict_peers=strict_peers,
        )
        self._sources: dict[str, Path] = {}  # object_id -> file path
        self._modified: set[str] = set()  # object_ids with unsaved changes

    # --- Loading ---

    def load_file(self, path: str | Path) -> LLMObject:
        """Load an LLM-object from an MD file."""
        path = Path(path)
        defn = parse_object_file(path)
        obj = LLMObject(defn, self._brain)
        self._bus.register(obj)
        self._sources[obj.object_id] = path
        return obj

    def load_directory(self, path: str | Path) -> list[LLMObject]:
        """Load all .md files in a directory as LLM-objects."""
        path = Path(path)
        objects = []
        for md_file in sorted(path.glob("*.md")):
            objects.append(self.load_file(md_file))
        return objects

    def create_object(self, definition: ObjectDefinition) -> LLMObject:
        """Create an LLM-object from a definition."""
        obj = LLMObject(definition, self._brain)
        self._bus.register(obj)
        return obj

    def create_object_from_text(self, markdown: str) -> LLMObject:
        """Create an LLM-object from markdown text."""
        defn = parse_object_text(markdown)
        return self.create_object(defn)

    # --- Messaging ---

    def send(
        self,
        recipient: str,
        content: str,
        sender: str = "__user__",
    ) -> list[ProcessingResult]:
        """Send a message to a specific object."""
        msg = Message(
            sender=sender,
            recipient=recipient,
            type=MessageType.DOMAIN,
            content=content,
        )
        return self._bus.send(msg)

    def send_event(
        self,
        recipient: str,
        content: str,
        sender: str = "__system__",
    ) -> list[ProcessingResult]:
        """Send an event message."""
        msg = Message(
            sender=sender,
            recipient=recipient,
            type=MessageType.EVENT,
            content=content,
        )
        return self._bus.send(msg)

    def broadcast(
        self,
        content: str,
        sender: str = "__system__",
    ) -> list[ProcessingResult]:
        """Broadcast a message to all objects."""
        msg = Message(
            sender=sender,
            recipient="__broadcast__",
            type=MessageType.DOMAIN,
            content=content,
        )
        return self._bus.send(msg)

    def publish(
        self,
        topic: str,
        content: str,
        sender: str = "__system__",
    ) -> list[ProcessingResult]:
        """Publish a message to all subscribers of a topic."""
        msg = Message(
            sender=sender,
            recipient="",
            type=MessageType.EVENT,
            content=content,
            topic=topic,
        )
        return self._bus.send(msg)

    # --- Modification ---

    def modify(self, object_id: str, **updates: object) -> None:
        """Modify an object's definition in-memory (state preserved)."""
        obj = self._bus.objects.get(object_id)
        if obj is None:
            raise KeyError(f"Object '{object_id}' not found")
        obj.modify_definition(**updates)
        self._modified.add(object_id)

    def add_peer(self, object_id: str, peer_id: str, relationship: str) -> None:
        """Add a peer to an object's definition."""
        obj = self._bus.objects.get(object_id)
        if obj is None:
            raise KeyError(f"Object '{object_id}' not found")
        obj.definition.peers.append(PeerDeclaration(peer_id, relationship))
        self._modified.add(object_id)

    def remove_peer(self, object_id: str, peer_id: str) -> None:
        """Remove a peer from an object's definition."""
        obj = self._bus.objects.get(object_id)
        if obj is None:
            raise KeyError(f"Object '{object_id}' not found")
        obj.definition.peers = [
            p for p in obj.definition.peers if p.object_id != peer_id
        ]
        self._modified.add(object_id)

    # --- Querying ---

    def state(self, object_id: str) -> str:
        """Get the current state of an object."""
        obj = self._bus.objects.get(object_id)
        if obj is None:
            raise KeyError(f"Object '{object_id}' not found")
        return obj.state

    def snapshot(self, object_id: str) -> dict:
        """Get a debug snapshot of an object."""
        obj = self._bus.objects.get(object_id)
        if obj is None:
            raise KeyError(f"Object '{object_id}' not found")
        return obj.snapshot()

    def topology(self) -> dict[str, list[str]]:
        """Return the communication graph."""
        return self._bus.topology()

    # --- Persistence ---

    def save_object(self, object_id: str, path: str | Path | None = None) -> Path:
        """Save an object's definition to disk."""
        obj = self._bus.objects.get(object_id)
        if obj is None:
            raise KeyError(f"Object '{object_id}' not found")

        save_path = Path(path) if path else self._sources.get(object_id)
        if save_path is None:
            raise ValueError(
                f"No path specified and no source path known for '{object_id}'"
            )

        save_path.write_text(serialize_object(obj.definition))
        self._sources[object_id] = save_path
        self._modified.discard(object_id)
        return save_path

    def has_unsaved_modifications(self, object_id: str) -> bool:
        """Check if an object has unsaved definition changes."""
        return object_id in self._modified

    # --- Metrics ---

    @property
    def metrics(self) -> BusMetrics:
        return self._bus.metrics

    @property
    def message_log(self) -> list[MessageLog]:
        return self._bus.log
