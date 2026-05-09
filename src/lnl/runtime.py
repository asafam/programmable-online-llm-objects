"""Runtime — library API tying together parser, objects, bus, and brain."""
from __future__ import annotations

import datetime
import logging
import queue
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import yaml

from .brain import LLMBrain
from .bus import BusMetrics, MessageBus
from .events import EventSourceRegistry
from .object import LLMObject
from .parser import parse_object_file, parse_object_text, serialize_object
from .tools import CreateObjectExecutor, ToolRegistry
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


class _Transaction:
    """Tracks in-flight execute tasks for one dispatch transaction.

    An transaction begins when messages are dispatched and ends when the system
    commits — all scheduled executions have completed and no new ones are
    pending. Uses a reference count: increment() when scheduling an object,
    decrement() in its completion callback; wait() blocks until count reaches 0.
    """

    def __init__(self, on_result: Optional[Callable[[ProcessingResult], None]] = None) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self._done = threading.Event()
        self._done.set()  # starts committed since count=0
        self.on_result = on_result  # optional per-result callback for this transaction

    def increment(self) -> None:
        with self._lock:
            self._count += 1
            self._done.clear()

    def decrement(self) -> None:
        with self._lock:
            self._count -= 1
            if self._count == 0:
                self._done.set()

    def wait(self) -> None:
        """Block until the transaction commits (all executions complete)."""
        self._done.wait()


_SYSTEM_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "lnl" / "system.yaml"


@dataclass
class SystemConfig:
    """Runtime configuration loaded from config/lnl/system.yaml."""
    # Heartbeat
    heartbeat_enabled: bool = False
    heartbeat_interval_seconds: float = 30.0
    pending_timeout_seconds: float = 90.0  # wall-clock seconds before abandoning a pending reply
    # Limits — analogous to call-stack depth settings
    max_tool_rounds: int = 5     # ReAct tool steps per object invocation (per-frame depth)
    max_chain_depth: int = 10    # message hops across objects (call-stack depth)
    max_history: int = 6         # history window per object
    # Cross-agent ReAct: include the Peer Interaction Loop section in system prompts
    react_cross_objects: bool = True
    # Knowledge gaps: auto-track in state and ask peers
    auto_track_knowledge_gaps: bool = True
    auto_ask_peers_on_gap: bool = True

    @staticmethod
    def load(path: Path | None = None) -> "SystemConfig":
        """Load from config/lnl/system.yaml (or a custom path). Returns defaults on missing file."""
        p = path or _SYSTEM_CONFIG_PATH
        try:
            with open(p) as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            return SystemConfig()
        hb = data.get("heartbeat", {})
        lim = data.get("limits", {})
        kg = data.get("knowledge_gaps", {})
        return SystemConfig(
            heartbeat_enabled=bool(hb.get("enabled", False)),
            heartbeat_interval_seconds=float(hb.get("interval_seconds", 30.0)),
            pending_timeout_seconds=float(hb.get("pending_timeout_seconds", 90.0)),
            max_tool_rounds=int(lim.get("max_tool_rounds", 5)),
            max_chain_depth=int(lim.get("max_chain_depth", 10)),
            max_history=int(lim.get("max_history", 6)),
            react_cross_objects=bool(data.get("react_cross_objects", True)),
            auto_track_knowledge_gaps=bool(kg.get("enabled", True)),
            auto_ask_peers_on_gap=bool(kg.get("ask_peers", True)),
        )


# Keep the old name as an alias for backward compatibility
HeartbeatConfig = SystemConfig


class Runtime:
    """Single entry point for the LNL runtime."""

    def __init__(
        self,
        brain: LLMBrain,
        max_chain_depth: int | None = None,
        tool_registry: ToolRegistry | None = None,
        pool_size: int = 4,
        heartbeat: "SystemConfig | None" = None,  # legacy param name; accepts SystemConfig
        system_config: "SystemConfig | None" = None,
    ) -> None:
        cfg = system_config or heartbeat or SystemConfig()
        self._brain = brain
        self._bus = MessageBus()
        self._max_chain_depth = max_chain_depth if max_chain_depth is not None else cfg.max_chain_depth
        self._max_tool_rounds = cfg.max_tool_rounds
        self._max_history = cfg.max_history
        self._react_cross_objects = cfg.react_cross_objects
        self._auto_track_knowledge_gaps = cfg.auto_track_knowledge_gaps
        self._auto_ask_peers_on_gap = cfg.auto_ask_peers_on_gap
        self._pending_timeout_seconds = cfg.pending_timeout_seconds
        self._heartbeat_interval_seconds = cfg.heartbeat_interval_seconds
        self._prompt_file: str = "object.yaml"
        self._sources: dict[str, Path] = {}  # object_id -> file path
        self._modified: set[str] = set()  # object_ids with unsaved changes
        self._classes: dict[str, ObjectDefinition] = {}  # class_id -> template definition
        self._event_sources = EventSourceRegistry()
        self._tool_registry = tool_registry
        self._heartbeat = cfg

        # Thread pool — shared across all objects; no per-object thread
        self._pool = ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="lnl-obj")
        self._active_futures: dict[str, Future] = {}
        self._futures_lock = threading.Lock()

        # Results accumulated during an transaction
        self._pending_results: list[ProcessingResult] = []
        self._results_lock = threading.Lock()
        self._sequence_counter = 0

        # Deterministic message ID counter
        self._msg_counter = 0
        self._msg_counter_lock = threading.Lock()

        # One transaction active at a time; _dispatch_lock serializes _dispatch calls
        self._current_transaction: Optional[_Transaction] = None
        self._dispatch_lock = threading.Lock()

        # Reply routing: object_id -> set of peer_ids whose reply is awaited
        self._awaiting_reply: dict[str, set[str]] = {}
        self._awaiting_lock = threading.Lock()

        # on_result callback for live mode
        self._on_result_callback: Optional[Callable[[ProcessingResult], None]] = None

        # Live mode state
        self._work_queue: queue.Queue[_WorkItem] = queue.Queue()
        self._running = threading.Event()
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None

        # Wire bus schedule callback — objects schedule themselves when mail arrives
        self._bus.set_schedule_callback(self._schedule_object)

        # Register create_object as a core tool available to all objects
        if self._tool_registry is not None:
            self._tool_registry.register("create_object", CreateObjectExecutor(self), CreateObjectExecutor.SPEC)

    def _next_msg_id(self, sender: str) -> str:
        """Return a deterministic message ID: '<sender>-<n>' with a monotonic counter."""
        with self._msg_counter_lock:
            n = self._msg_counter
            self._msg_counter += 1
        return f"{sender}-{n}"

    def set_prompt_file(self, prompt_file: str) -> None:
        """Set the object system-prompt template filename (relative to config/prompts/lnl/).
        Must be called before loading objects."""
        self._prompt_file = prompt_file

    def set_max_history(self, max_history: int) -> None:
        """Override the conversation history window per object. Must be called before loading objects."""
        self._max_history = max_history

    # --- Loading ---

    def load_file(self, path: str | Path) -> LLMObject | None:
        """Load an MD file: registers as an llm-class if type=class, instantiates otherwise.

        Returns None for class definitions (they are registered but not instantiated).
        """
        path = Path(path)
        defn, obj_type = parse_object_file(path)
        if obj_type == "class":
            self.register_class(defn.object_id, defn)
            return None
        obj = self._register_object(defn)
        self._sources[obj.object_id] = path
        return obj

    def load_directory(self, path: str | Path) -> list[LLMObject]:
        """Load all .md files in a directory. Class definitions are registered; objects are returned."""
        path = Path(path)
        objects = []
        for md_file in sorted(path.glob("*.md")):
            obj = self.load_file(md_file)
            if obj is not None:
                objects.append(obj)
        return objects

    def create_object(self, definition: ObjectDefinition) -> LLMObject:
        """Instantiate an llm-object from a definition."""
        return self._register_object(definition)

    def create_object_from_text(self, markdown: str) -> LLMObject:
        """Instantiate an llm-object from markdown text."""
        defn, _ = parse_object_text(markdown)
        return self._register_object(defn)

    def register_class(self, class_id: str, definition: ObjectDefinition) -> None:
        """Register an llm-class template. Does not create a live object."""
        self._classes[class_id] = definition

    def spawn(self, object_id: str, class_id: str, params: dict | None = None) -> LLMObject:
        """Instantiate a registered llm-class into a new live llm-object."""
        template = self._classes.get(class_id)
        if template is None:
            raise KeyError(f"llm-class '{class_id}' is not registered")
        defn = self._instantiate_class(template, object_id, params or {})
        return self._register_object(defn)

    def _instantiate_class(self, template: ObjectDefinition, object_id: str, params: dict) -> ObjectDefinition:
        """Clone a class template, substitute {param} placeholders, and set the instance object_id."""
        import copy
        defn = copy.deepcopy(template)
        defn.object_id = object_id
        for key, val in params.items():
            placeholder = "{" + key + "}"
            defn.role = defn.role.replace(placeholder, str(val))
            defn.behavior = defn.behavior.replace(placeholder, str(val))
        return defn

    def _register_object(self, definition: ObjectDefinition) -> LLMObject:
        """Create, register on bus, and bind event sources to providers."""
        tool_context_factory = None
        if self._tool_registry:
            def _make_context(obj: LLMObject) -> dict:
                def push_event(content: str, source: str = "__code__") -> None:
                    """Inject an event into the calling object's own mailbox."""
                    msg = Message(
                        sender=source,
                        recipient=obj.object_id,
                        type=MessageType.EVENT,
                        content=content,
                    )
                    self._bus.deliver(msg)

                def inject_event(recipient: str, content: str, source: str = "__external__") -> None:
                    """Inject an event to any object."""
                    msg = Message(
                        sender=source,
                        recipient=recipient,
                        type=MessageType.EVENT,
                        content=content,
                    )
                    self._bus.deliver(msg)

                return {"push_event": push_event, "inject_event": inject_event}
            tool_context_factory = _make_context

        obj = LLMObject(
            definition, self._brain,
            tool_registry=self._tool_registry,
            tool_context_factory=tool_context_factory,
            max_tool_rounds=self._max_tool_rounds,
            max_history=self._max_history,
            react_cross_objects=self._react_cross_objects,
            pending_timeout_seconds=self._pending_timeout_seconds,
            heartbeat_interval_seconds=self._heartbeat_interval_seconds,
            prompt_file=self._prompt_file,
            auto_track_knowledge_gaps=self._auto_track_knowledge_gaps,
            auto_ask_peers_on_gap=self._auto_ask_peers_on_gap,
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
        """Send a message to a specific object."""
        msg = Message(
            sender=sender,
            recipient=recipient,
            type=MessageType.DOMAIN,
            content=content,
            depth_remaining=self._max_chain_depth,
            id=self._next_msg_id(sender),
        )
        if self._running.is_set():
            item = _WorkItem(message=msg)
            self._work_queue.put(item)
            item.done.wait()
            return item.results
        return self._dispatch([msg])

    def send_many(
        self,
        items: list[tuple[str, str, str]],
        on_result: Optional[Callable[[ProcessingResult], None]] = None,
    ) -> list[ProcessingResult]:
        """Dispatch multiple messages simultaneously in one transaction.

        All messages are delivered to the bus before waiting for the wave to
        settle, achieving true concurrent processing within a single dispatch.

        Args:
            items: list of (recipient, content, sender) tuples.
            on_result: optional callback fired for each direct result of an input
                message (filtered by source_message_id; cascades are excluded).
        """
        messages = [
            Message(
                sender=sender,
                recipient=recipient,
                type=MessageType.DOMAIN,
                content=content,
                depth_remaining=self._max_chain_depth,
                id=self._next_msg_id(sender),
            )
            for recipient, content, sender in items
        ]
        if on_result is not None:
            input_ids = {m.id for m in messages}
            def _filtered(result: ProcessingResult, _ids: set = input_ids, _cb = on_result) -> None:
                if result.source_message_id in _ids:
                    _cb(result)
            return self._dispatch(messages, on_result=_filtered)
        return self._dispatch(messages)

    def process_pending(self) -> list[ProcessingResult]:
        """Process all pending mailbox messages and polled events until committed."""
        if self._running.is_set():
            item = _WorkItem()
            self._work_queue.put(item)
            item.done.wait()
            return item.results
        return self._dispatch([])

    def inject_event(
        self,
        recipient: str,
        content: str,
        source: str = "__external__",
    ) -> list[ProcessingResult]:
        """Inject an external event through the event source registry."""
        if self._running.is_set():
            item = _WorkItem(event_inject=(recipient, content, source))
            self._work_queue.put(item)
            item.done.wait()
            return item.results
        self._event_sources.inject(recipient, content, source)
        return self._dispatch([])

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
            depth_remaining=self._max_chain_depth,
            id=self._next_msg_id(sender),
        )
        if self._running.is_set():
            item = _WorkItem(message=msg)
            self._work_queue.put(item)
            item.done.wait()
            return item.results
        return self._dispatch([msg])

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
            depth_remaining=self._max_chain_depth,
            id=self._next_msg_id(sender),
        )
        if self._running.is_set():
            item = _WorkItem(message=msg)
            self._work_queue.put(item)
            item.done.wait()
            return item.results
        return self._dispatch([msg])

    # --- Virtual Actor Scheduling ---

    def _schedule_object(self, obj: LLMObject) -> None:
        """Schedule an object for execution on the thread pool.

        Called by LLMObject.deliver() when the object transitions from idle to active.
        Registers with the current transaction so the transaction waits for this execution to complete.
        """
        transaction = self._current_transaction  # read once; safe because _dispatch_lock guards writes
        if transaction:
            transaction.increment()

        try:
            future = self._pool.submit(obj.read, self._on_result)
        except RuntimeError:
            # Pool already shut down (e.g. a reply arrives just after the last
            # TC completes and the executor is torn down). Safe to ignore —
            # the evaluation is over and there is no transaction to decrement.
            if transaction:
                transaction.decrement()
            return

        with self._futures_lock:
            self._active_futures[obj.object_id] = future

        def _done(f: Future, _transaction: Optional[_Transaction] = transaction, _oid: str = obj.object_id) -> None:
            with self._futures_lock:
                self._active_futures.pop(_oid, None)
            if f.exception():
                exc = f.exception()
                if "content filter" in str(exc).lower():
                    logger.warning("Error reading object %s: %s", _oid, exc)
                else:
                    logger.exception("Error reading object %s", _oid, exc_info=exc)
            if _transaction:
                _transaction.decrement()

        future.add_done_callback(_done)

    def _on_result(self, result: ProcessingResult) -> None:
        """Called from pool threads after each message is processed by an object.

        Assigns a sequence number, records the result, then routes replies and
        outgoing messages through the bus — scheduling idle recipients and keeping
        the transaction open until all cascades commit.
        """
        with self._results_lock:
            result.sequence = self._sequence_counter
            self._sequence_counter += 1
            self._pending_results.append(result)

        self._bus.record_processing(result)

        if self._on_result_callback:
            self._on_result_callback(result)

        txn = self._current_transaction
        if txn is not None and txn.on_result is not None:
            txn.on_result(result)

        obj_id = result.object_id

        logger.debug(
            "[seq=%d] %s processed msg from %s (%s) → reply=%s, outgoing=%s",
            result.sequence, obj_id, result.in_reply_to,
            result.source_message_type.value if result.source_message_type else "?",
            repr(str(result.reply)[:80]) if result.reply else "(empty)",
            [o.recipient for o in result.outgoing_messages],
        )

        # Route reply back to sender ONLY if the sender is awaiting a reply from
        # this object (prevents ack storms from write-service objects).
        with self._awaiting_lock:
            should_reply = (
                bool(result.reply)
                and result.in_reply_to is not None
                and result.source_message_type != MessageType.REPLY
                and result.in_reply_to not in ("__user__", "__system__", "__external__", "__code__")
                and result.in_reply_to in self._bus.objects
                and obj_id in self._awaiting_reply.get(result.in_reply_to, set())
            )
            if should_reply:
                self._awaiting_reply[result.in_reply_to].discard(obj_id)

        if should_reply:
            next_depth = result.depth_remaining - 1
            if next_depth > 0:
                reply_msg = Message(
                    sender=obj_id,
                    recipient=result.in_reply_to,
                    type=MessageType.REPLY,
                    content=result.reply,
                    depth_remaining=next_depth,
                    id=self._next_msg_id(obj_id),
                    in_reply_to=result.source_message_id,
                    # Propagate the original message's plan step index so the
                    # asker's plan step auto-marks done when the reply arrives.
                    plan_step_index=result.source_plan_step_index,
                )
                self._bus.deliver(reply_msg)
                # Clear any pending-inbound entry for this recipient so later
                # outgoings don't double-stamp with a now-consumed Ask.
                sender_obj = self._bus.objects.get(obj_id)
                if sender_obj is not None and hasattr(sender_obj, "clear_pending_inbound"):
                    sender_obj.clear_pending_inbound(result.in_reply_to)
                logger.debug("  ↩ reply routed: %s → %s", obj_id, result.in_reply_to)
            else:
                logger.warning("Chain depth limit reached; dropping reply from %s to %s", obj_id, result.in_reply_to)

        # Deliver outgoing peer messages
        sender_obj = self._bus.objects.get(obj_id)
        for out in result.outgoing_messages:
            next_depth = result.depth_remaining - 1
            if next_depth <= 0:
                logger.warning(
                    "Chain depth limit reached; dropping message from %s to %s",
                    obj_id, out.recipient,
                )
                continue
            msg_id = self._next_msg_id(obj_id)
            # `is_reply` is set when the outgoing fulfills a pending inbound Ask.
            # Route it as MessageType.REPLY and release the asker's awaiting state.
            msg_type = MessageType.REPLY if out.is_reply else MessageType.DOMAIN
            chained = Message(
                sender=obj_id,
                recipient=out.recipient,
                type=msg_type,
                content=out.content,
                depth_remaining=next_depth,
                id=msg_id,
                in_reply_to=out.in_reply_to,
                expects_reply=out.expects_reply,
                plan_step_index=out.plan_step_index,
            )
            self._bus.deliver(chained)
            # If this outgoing dispatched one of our own plan steps, flip the
            # step from 'planned' to 'dispatched' on the bus send (Ask only;
            # Tells are already auto-marked 'done' by _correlate_outgoing).
            if (
                out.plan_step_index is not None
                and not out.is_reply
                and out.expects_reply
                and sender_obj is not None
                and hasattr(sender_obj, "mark_step_dispatched")
            ):
                sender_obj.mark_step_dispatched(out.plan_step_index)
            if out.is_reply:
                with self._awaiting_lock:
                    self._awaiting_reply.get(out.recipient, set()).discard(obj_id)
            logger.debug(
                "  → outgoing (%s): %s → %s: %s",
                "reply" if out.is_reply else ("ask" if out.expects_reply else "tell"),
                obj_id, out.recipient, repr(str(out.content)[:80]),
            )
            if out.expects_reply:
                with self._awaiting_lock:
                    self._awaiting_reply.setdefault(obj_id, set()).add(out.recipient)

    def _dispatch(
        self,
        messages: list[Message],
        on_result: Optional[Callable[[ProcessingResult], None]] = None,
    ) -> list[ProcessingResult]:
        """Dispatch messages into the network and block until the transaction commits.

        Delivers each message to the bus (scheduling idle recipients), polls any
        pending external events, then waits for all triggered executions to complete.
        Transactions are serialized via _dispatch_lock — only one is in-flight at a time.
        """
        with self._dispatch_lock:
            transaction = _Transaction(on_result=on_result)
            self._current_transaction = transaction
            with self._awaiting_lock:
                self._awaiting_reply.clear()

            self._poll_events_once()

            for msg in messages:
                self._bus.deliver(msg)

            transaction.wait()  # block until committed
            self._current_transaction = None

            with self._results_lock:
                results = sorted(self._pending_results, key=lambda r: r.sequence)
                self._pending_results.clear()

            return results

    def _poll_events_once(self) -> None:
        """Poll all event sources and deliver events to object mailboxes."""
        for object_id, envelope in self._event_sources.poll_all():
            msg = Message(
                sender=envelope.source_id,
                recipient=object_id,
                type=MessageType.EVENT,
                content=envelope.content,
                depth_remaining=self._max_chain_depth,
                id=self._next_msg_id(envelope.source_id),
            )
            self._bus.deliver(msg)

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
        """Get the event source provider for an object's declared source."""
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

        save_path.write_text(serialize_object(obj.definition, obj_type="object"))
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
        """Start the live run-loop. Blocks until stop() is called."""
        self._shutdown.clear()
        self._running.set()
        self._on_result_callback = on_result
        if self._heartbeat.heartbeat_enabled and self._heartbeat_thread is None:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                args=(self._heartbeat.heartbeat_interval_seconds,),
                daemon=True,
            )
            self._heartbeat_thread.start()
        try:
            self._run_loop(poll_interval)
        finally:
            self._running.clear()
            self._on_result_callback = None

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
        if self._heartbeat.heartbeat_enabled:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                args=(self._heartbeat.heartbeat_interval_seconds,),
                daemon=True,
            )
            self._heartbeat_thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the run-loop to stop and wait for it to finish."""
        self._shutdown.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2.0)
            self._heartbeat_thread = None
        self._pool.shutdown(wait=False)

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
            depth_remaining=self._max_chain_depth,
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

    def _heartbeat_loop(self, interval: float) -> None:
        """Broadcast a heartbeat to all objects at a fixed interval (live mode only)."""
        while not self._shutdown.wait(timeout=interval):
            if not self._running.is_set():
                continue
            msg = Message(
                sender="__system__",
                recipient="__broadcast__",
                type=MessageType.HEARTBEAT,
                content="Heartbeat",
                depth_remaining=1,  # heartbeats do not cascade to peer chains
            )
            self._work_queue.put(_WorkItem(message=msg))

    def _run_loop(self, poll_interval: float) -> None:
        """Internal run-loop: dequeue work, dispatch transaction, repeat."""
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

            # Collect messages and inject events from work items
            msgs: list[Message] = []
            for item in items:
                if item.message is not None:
                    msgs.append(item.message)
                if item.event_inject is not None:
                    recipient, content, source = item.event_inject
                    self._event_sources.inject(recipient, content, source)

            # Dispatch: deliver all messages and poll events; block until transaction commits
            try:
                results = self._dispatch(msgs)
            except Exception:
                logger.exception("Error in run-loop processing")
                results = []

            # Signal completion to all work items
            for item in items:
                item.results = results
                item.done.set()

    # --- Metrics ---

    def set_message_listener(self, callback) -> None:
        """Set a callback invoked on every message delivery: callback(Message)."""
        self._bus.on_message = callback

    @property
    def metrics(self) -> BusMetrics:
        return self._bus.metrics

    @property
    def message_log(self) -> list[MessageLog]:
        return self._bus.log
