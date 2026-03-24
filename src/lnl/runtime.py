"""Runtime — library API tying together parser, objects, bus, and brain."""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .brain import LLMBrain
from .bus import BusMetrics, MessageBus
from .events import EventSourceRegistry
from .object import LLMObject
from .parser import parse_object_file, parse_object_text, serialize_object
from .tools import ToolRegistry
from .types import (
    Message,
    MessageLog,
    MessageType,
    ObjectDefinition,
    PeerDeclaration,
    ProcessingResult,
)

logger = logging.getLogger(__name__)


@dataclass
class _WorkItem:
    """Unit of work submitted to the live run-loop."""
    message: Message | None = None
    event_inject: tuple[str, str, str] | None = None  # (recipient, content, source)
    done: threading.Event = field(default_factory=threading.Event)
    results: list[ProcessingResult] = field(default_factory=list)


class Runtime:
    """Single entry point for the LNL runtime."""

    def __init__(
        self,
        brain: LLMBrain,
        max_chain_depth: int = 10,
        strict_peers: bool = True,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._brain = brain
        self._bus = MessageBus(strict_peers=strict_peers)
        self._max_chain_depth = max_chain_depth
        self._sources: dict[str, Path] = {}  # object_id -> file path
        self._modified: set[str] = set()  # object_ids with unsaved changes
        self._event_sources = EventSourceRegistry()
        self._tool_registry = tool_registry

        # Live mode state
        self._work_queue: queue.Queue[_WorkItem] = queue.Queue()
        self._running = threading.Event()
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None

    # --- Loading ---

    def load_file(self, path: str | Path) -> LLMObject:
        """Load an LLM-object from an MD file."""
        path = Path(path)
        defn = parse_object_file(path)
        obj = self._register_object(defn)
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
        return self._register_object(definition)

    def create_object_from_text(self, markdown: str) -> LLMObject:
        """Create an LLM-object from markdown text."""
        defn = parse_object_text(markdown)
        return self._register_object(defn)

    def _register_object(self, definition: ObjectDefinition) -> LLMObject:
        """Create, register on bus, and bind event sources to providers."""
        # Build tool context factory that provides push_event for domain logic
        tool_context_factory = None
        if self._tool_registry:
            def _make_context(obj: LLMObject) -> dict:
                def push_event(content: str, source: str = "__code__") -> None:
                    self._event_sources.inject(obj.object_id, content, source)
                    if self._running.is_set():
                        self._work_queue.put(_WorkItem())  # wake run-loop
                return {"push_event": push_event}
            tool_context_factory = _make_context

        obj = LLMObject(
            definition, self._brain,
            tool_registry=self._tool_registry,
            tool_context_factory=tool_context_factory,
        )
        self._bus.register(obj)
        if definition.event_sources:
            self._event_sources.bind_object(obj.object_id, definition.event_sources)

        return obj

    # --- Messaging ---

    def send(
        self,
        recipient: str,
        content: str,
        sender: str = "__user__",
    ) -> list[ProcessingResult]:
        """Send a message to a specific object.

        In live mode, enqueues work and blocks until processed.
        """
        msg = Message(
            sender=sender,
            recipient=recipient,
            type=MessageType.DOMAIN,
            content=content,
        )
        if self._running.is_set():
            item = _WorkItem(message=msg)
            self._work_queue.put(item)
            item.done.wait()
            return item.results
        self._bus.deliver(msg)
        return self._run_until_quiescent()

    def process_pending(self) -> list[ProcessingResult]:
        """Process all pending mailbox messages and polled events until quiescent."""
        if self._running.is_set():
            item = _WorkItem()
            self._work_queue.put(item)
            item.done.wait()
            return item.results
        return self._run_until_quiescent()

    def inject_event(
        self,
        recipient: str,
        content: str,
        source: str = "__external__",
    ) -> list[ProcessingResult]:
        """Inject an external event through the event source registry.

        In live mode, enqueues work and blocks until processed.
        """
        if self._running.is_set():
            item = _WorkItem(event_inject=(recipient, content, source))
            self._work_queue.put(item)
            item.done.wait()
            return item.results
        self._event_sources.inject(recipient, content, source)
        return self._run_until_quiescent()

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
        if self._running.is_set():
            item = _WorkItem(message=msg)
            self._work_queue.put(item)
            item.done.wait()
            return item.results
        self._bus.deliver(msg)
        return self._run_until_quiescent()

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
        if self._running.is_set():
            item = _WorkItem(message=msg)
            self._work_queue.put(item)
            item.done.wait()
            return item.results
        self._bus.deliver(msg)
        return self._run_until_quiescent()

    # --- Processing Loop ---

    def _run_until_quiescent(self) -> list[ProcessingResult]:
        """Process all pending mailbox messages until the system is quiescent."""
        results: list[ProcessingResult] = []
        total = 0

        while total < self._max_chain_depth:
            # Poll event sources and deliver to mailboxes
            for object_id, envelope in self._event_sources.poll_all():
                msg = Message(
                    sender=envelope.source_id,
                    recipient=object_id,
                    type=MessageType.EVENT,
                    content=envelope.content,
                )
                self._bus.deliver(msg)

            # Find next object with pending messages (deterministic: registration order)
            obj = next(
                (o for o in self._bus.objects.values() if o.has_pending),
                None,
            )
            if obj is None:
                break  # quiescent

            result = obj.process_next()
            if result is None:
                continue
            total += 1
            results.append(result)
            self._bus.record_processing(result)

            # Deliver outgoing peer messages
            for out in result.outgoing_messages:
                chained = Message(
                    sender=obj.object_id,
                    recipient=out.recipient,
                    type=MessageType.DOMAIN,
                    content=out.content,
                )
                self._bus.deliver(chained)

        return results

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

    @property
    def event_registry(self) -> dict[str, list[str]]:
        """Return object_id → list of bound source descriptors."""
        return self._event_sources.bindings_summary()

    def get_event_source(self, object_id: str, descriptor: str):
        """Get the event source provider for an object's declared source.

        Returns an InjectableEventSource (or custom provider) that the
        test harness or external adapter can fire events into.
        """
        return self._event_sources.get_source(object_id, descriptor)

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

    # --- Live Mode ---

    @property
    def is_running(self) -> bool:
        """True when the live run-loop is active."""
        return self._running.is_set()

    def run(
        self,
        poll_interval: float = 0.1,
        on_result: Callable[[ProcessingResult], None] | None = None,
    ) -> None:
        """Start the live run-loop. Blocks until stop() is called.

        The run-loop continuously polls for work (submitted messages and
        event source activity) and processes until quiescent, then waits.
        """
        self._shutdown.clear()
        self._running.set()
        try:
            self._run_loop(poll_interval, on_result)
        finally:
            self._running.clear()

    def start(
        self,
        poll_interval: float = 0.1,
        on_result: Callable[[ProcessingResult], None] | None = None,
    ) -> None:
        """Start the runtime. Runs the processing loop in a background thread."""
        self._thread = threading.Thread(
            target=self.run,
            args=(poll_interval, on_result),
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the run-loop to stop and wait for it to finish."""
        self._shutdown.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def submit(
        self,
        recipient: str,
        content: str,
        sender: str = "__user__",
    ) -> _WorkItem:
        """Submit a message for processing by the run-loop. Non-blocking.

        Returns a _WorkItem whose `done` event is set when processing completes.
        Results are available in `item.results`.
        """
        msg = Message(
            sender=sender,
            recipient=recipient,
            type=MessageType.DOMAIN,
            content=content,
        )
        item = _WorkItem(message=msg)
        self._work_queue.put(item)
        return item

    def kill_object(self, object_id: str) -> None:
        """Remove an object from the runtime permanently."""
        self._bus.unregister(object_id)
        self._event_sources.unbind_object(object_id)
        self._sources.pop(object_id, None)
        self._modified.discard(object_id)

    def _run_loop(
        self,
        poll_interval: float,
        on_result: Callable[[ProcessingResult], None] | None,
    ) -> None:
        """Internal run-loop: drain work queue, process, repeat."""
        while not self._shutdown.is_set():
            # Block until work arrives or poll interval elapses
            items: list[_WorkItem] = []
            try:
                items.append(self._work_queue.get(timeout=poll_interval))
            except queue.Empty:
                pass
            # Drain any additional queued items
            while True:
                try:
                    items.append(self._work_queue.get_nowait())
                except queue.Empty:
                    break

            # Deliver messages / inject events from work items
            for item in items:
                if item.message is not None:
                    self._bus.deliver(item.message)
                if item.event_inject is not None:
                    recipient, content, source = item.event_inject
                    self._event_sources.inject(recipient, content, source)

            # Process everything until quiescent
            try:
                results = self._run_until_quiescent()
            except Exception:
                logger.exception("Error in run-loop processing")
                results = []

            # Fire callbacks and signal completion
            if on_result:
                for r in results:
                    on_result(r)
            for item in items:
                item.results = results
                item.done.set()

    # --- Metrics ---

    @property
    def metrics(self) -> BusMetrics:
        return self._bus.metrics

    @property
    def message_log(self) -> list[MessageLog]:
        return self._bus.log
