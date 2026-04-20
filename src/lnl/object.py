"""LLMObject — the single runtime entity in the LNL system."""
from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import asdict
from typing import Callable, Optional

from .brain import (
    LLMBrain,
    _build_chat_messages,
    build_system_prompt,
)
from .tools import ToolRegistry
from .types import (
    InferenceMetrics,
    Message,
    ObjectDefinition,
    PeerDeclaration,
    ProcessingResult,
    ReactFinish,
    StateDelta,
    ToolResult,
)


class LLMObject:
    """An LLM-object: definition + brain + mutable NL state."""

    def __init__(
        self,
        definition: ObjectDefinition,
        brain: LLMBrain,
        tool_registry: ToolRegistry | None = None,
        tool_context_factory: object = None,
        max_tool_rounds: int = 5,
        max_history: int = 6,
        react_cross_objects: bool = True,
        pending_timeout_seconds: float = 90.0,
        heartbeat_interval_seconds: float = 30.0,
    ) -> None:
        self._definition = definition
        self._brain = brain
        self._state = ""  # mutable runtime state (str from LLM; dict from mock scripts)
        self._history: list[Message] = []
        self._mailbox: deque[Message] = deque()
        self._tool_registry = tool_registry
        self._tool_context_factory = tool_context_factory
        self._lock = threading.Lock()   # guards _mailbox and _active
        self._active = False            # True while scheduled or running on pool
        self._max_tool_rounds = max_tool_rounds
        self._max_history = max_history
        self._react_cross_objects = react_cross_objects
        self._pending_timeout_seconds = pending_timeout_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds

    # --- Properties ---

    @property
    def object_id(self) -> str:
        return self._definition.object_id

    @property
    def state(self):
        """Return state: a dict if parseable, otherwise the raw string (or {} if empty)."""
        return _coerce_state(self._state)

    @property
    def definition(self) -> ObjectDefinition:
        return self._definition

    @property
    def peer_ids(self) -> list[str]:
        return [p.object_id for p in self._definition.peers]

    @property
    def subscriptions(self) -> list[str]:
        return list(self._definition.subscriptions)

    @property
    def history(self) -> list[Message]:
        return list(self._history)

    # --- Mailbox ---

    @property
    def has_pending(self) -> bool:
        """True if the mailbox has messages waiting to be processed."""
        return bool(self._mailbox)

    @property
    def mailbox(self) -> deque[Message]:
        return self._mailbox

    def deliver(self, message: Message, schedule_callback: Optional[Callable] = None) -> None:
        """Put a message in this object's mailbox.

        If a schedule_callback is provided and the object is not already active,
        marks the object active and calls the callback to schedule it on the pool.
        """
        with self._lock:
            self._mailbox.append(message)
            if not self._active:
                self._active = True
                if schedule_callback:
                    schedule_callback(self)

    def read(self, on_result: Callable[[ProcessingResult], None]) -> None:
        """Execute pending messages until the mailbox is empty, then yield.

        Designed to run on a thread pool. The object owns its execution:
        it dequeues messages one at a time and calls on_result after each,
        releasing its active flag only when the mailbox is confirmed empty.
        """
        while True:
            with self._lock:
                if not self._mailbox:
                    self._active = False
                    return
                message = self._mailbox.popleft()
            result = self.process_message(message)  # LLM call outside lock
            on_result(result)

    def process_next(self) -> ProcessingResult | None:
        """Process the next message from the mailbox (batch/test helper)."""
        if not self._mailbox:
            return None
        message = self._mailbox.popleft()
        return self.process_message(message)

    # --- Core Processing (ReAct loop) ---

    def process_message(self, message: Message) -> ProcessingResult:
        """Process an incoming message via a ReAct loop: think → act → observe → repeat."""
        state_before = self._state  # snapshot — state only committed after successful loop

        tools_desc = self._tool_registry.describe() if self._tool_registry else ""
        sys_prompt = build_system_prompt(
            self._definition, self._state,
            tools=tools_desc,
            react_cross_objects=self._react_cross_objects,
            pending_timeout_seconds=self._pending_timeout_seconds,
            heartbeat_interval_seconds=self._heartbeat_interval_seconds,
        )
        messages = _build_chat_messages(sys_prompt, self._history, message)

        total_metrics = InferenceMetrics(model="")
        finish: ReactFinish | None = None
        tool_rounds = 0
        pending_deltas: list[StateDelta] = []

        while True:
            step, metrics = self._brain.react_call(messages, object_id=self.object_id)
            total_metrics = _accumulate_metrics(total_metrics, metrics)

            if step.state_update:
                pending_deltas.append(step.state_update)

            if step.action == "finish":
                finish = step.finish
                break

            # action == "tool_call"
            if tool_rounds >= self._max_tool_rounds:
                # Hard stop — manufacture an empty finish to avoid infinite loops.
                finish = ReactFinish(reply="", updated_state=self._state)
                break

            tc = step.tool_call
            if not self._tool_registry or tc is None:
                # No registry — tell the LLM tools are unavailable and let it finish.
                messages.append({"role": "assistant", "content": json.dumps({
                    "thought": step.thought,
                    "action": "tool_call",
                    "tool_call": {"id": tc.id if tc else "", "tool": tc.tool if tc else "", "arguments": {}},
                })})
                messages.append({"role": "user", "content": "[Tool execution unavailable — no tool registry is configured. Please provide your final answer.]"})
                continue

            tool_rounds += 1
            ctx = self._tool_context_factory(self) if self._tool_context_factory else {}
            try:
                result = self._tool_registry.execute(tc, ctx)
            except Exception as exc:
                result = ToolResult(id=tc.id, output="", error=f"Tool execution raised an exception: {exc}")

            messages.append({"role": "assistant", "content": json.dumps({
                "thought": step.thought,
                "action": "tool_call",
                "tool_call": {"id": tc.id, "tool": tc.tool, "arguments": tc.arguments},
            })})
            messages.append({"role": "user", "content": f"[Tool result for {tc.id}]: {result.output}" + (f"\nError: {result.error}" if result.error else "")})

        if finish is None:
            finish = ReactFinish(reply="")

        if pending_deltas:
            current = _coerce_state(self._state)
            if not isinstance(current, dict):
                current = {}
            for delta in pending_deltas:
                current = _apply_delta(current, delta)
            self._state = json.dumps(current)
        elif finish.updated_state:
            # Backward compat: MockBrain / test scripts that set updated_state directly
            self._state = finish.updated_state
        # else: no deltas, no updated_state → state unchanged

        if finish.updated_definition:
            self._apply_definition_update(finish.updated_definition)
        self._history.append(message)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        return ProcessingResult(
            object_id=self.object_id,
            reply=finish.reply,
            outgoing_messages=finish.outgoing_messages,
            state_before=_coerce_state(state_before),
            state_after=_coerce_state(self._state),
            metrics=total_metrics,
            in_reply_to=message.sender,
            source_message_type=message.type,
            depth_remaining=message.depth_remaining,
            source_message_id=message.id,
        )

    # --- Live Modification ---

    def modify_definition(self, **updates: object) -> None:
        """Change definition fields WITHOUT resetting state."""
        for key, value in updates.items():
            if not hasattr(self._definition, key):
                raise AttributeError(f"ObjectDefinition has no field '{key}'")
            setattr(self._definition, key, value)

    _PATCHABLE_DEFINITION_FIELDS = {"role", "behavior"}

    def _apply_definition_update(self, patch: dict) -> None:
        """Apply a definition patch from the LLM (admin-driven self-modification)."""
        updates = {k: v for k, v in patch.items() if k in self._PATCHABLE_DEFINITION_FIELDS}
        if "peers" in patch and isinstance(patch["peers"], list):
            updates["peers"] = [
                PeerDeclaration(object_id=p["object_id"], relationship=p["relationship"])
                for p in patch["peers"]
                if isinstance(p, dict)
            ]
        if updates:
            self.modify_definition(**updates)

    # --- Testing / Debugging ---

    def set_state(self, state: str | dict) -> None:
        """Set state directly (for testing). Accepts str or dict (dict is JSON-encoded)."""
        if isinstance(state, dict):
            import json as _json
            self._state = _json.dumps(state)
        else:
            self._state = state

    def snapshot(self) -> dict:
        """Return a debug snapshot of the object."""
        return {
            "object_id": self.object_id,
            "state": _coerce_state(self._state),
            "definition": asdict(self._definition),
            "history_length": len(self._history),
        }


def _coerce_state(s):
    """Return state as dict if possible, otherwise the raw string (or {} if empty)."""
    if isinstance(s, dict):
        return s
    if not s:
        return {}
    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return s


def _apply_delta(state: dict, delta: StateDelta) -> dict:
    """Apply a single state delta to a state dict in-place and return it."""
    if delta.op == "set":
        state[delta.key] = delta.value
    elif delta.op == "delete":
        state.pop(delta.key, None)
    elif delta.op == "append":
        lst = state.get(delta.key, [])
        if not isinstance(lst, list):
            lst = [lst]
        lst.append(delta.value)
        state[delta.key] = lst
    return state


def _accumulate_metrics(base: InferenceMetrics, add: InferenceMetrics) -> InferenceMetrics:
    """Combine metrics from multiple LLM calls."""
    return InferenceMetrics(
        input_tokens=base.input_tokens + add.input_tokens,
        output_tokens=base.output_tokens + add.output_tokens,
        latency_ms=base.latency_ms + add.latency_ms,
        model=base.model or add.model,
    )
