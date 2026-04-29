"""LLMObject — the single runtime entity in the LNL system."""
from __future__ import annotations

import datetime
import json
import logging
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
    PLAN_TERMINAL_STATUSES,
    STEP_TERMINAL_STATUSES,
    InferenceMetrics,
    KnowledgeGap,
    Message,
    MessageType,
    ObjectDefinition,
    OutgoingMessage,
    PeerDeclaration,
    Plan,
    PlanStep,
    PlanUpdate,
    ProcessingResult,
    ReactFinish,
    StateDelta,
    ToolResult,
)

logger = logging.getLogger(__name__)



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
        prompt_file: str = "object.yaml",
        auto_track_knowledge_gaps: bool = True,
        auto_ask_peers_on_gap: bool = True,
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
        self._prompt_file = prompt_file
        self._auto_track_knowledge_gaps = auto_track_knowledge_gaps
        self._auto_ask_peers_on_gap = auto_ask_peers_on_gap
        # Plan state — a single active plan per object at a time. The LLM
        # reasons about steps by position (0-based) — it never authors ids.
        self._active_plan: Optional[Plan] = None
        self._completed_plans: list[Plan] = []
        self._plans_lock = threading.Lock()
        # Pending inbound Asks: sender → (message_id, plan_step_index).
        # When the object eventually emits an outgoing to this sender, the
        # runtime stamps it as a reply and propagates the asker's plan
        # correlation. Survives across turns so a nested reply chain still
        # correlates back to the original asker's plan.
        self._pending_inbound_asks: dict[str, tuple[str, Optional[int]]] = {}
        self._pending_inbound_lock = threading.Lock()

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

    # --- Plan accessors ---

    @property
    def active_plan(self) -> Optional[Plan]:
        """Return the object's current active plan (or None)."""
        with self._plans_lock:
            return self._active_plan

    @property
    def completed_plans(self) -> list[Plan]:
        """Archive of completed/cancelled plans (most recent last)."""
        with self._plans_lock:
            return list(self._completed_plans)

    def clear_pending_inbound(self, sender: str) -> None:
        """Runtime hook: clear a pending-inbound entry when a reply to `sender`
        has been delivered via the `finish.reply` path."""
        with self._pending_inbound_lock:
            self._pending_inbound_asks.pop(sender, None)

    # --- Core Processing (ReAct loop) ---

    def process_message(self, message: Message) -> ProcessingResult:
        """Process an incoming message via a ReAct loop: think → act → observe → repeat."""
        state_before = self._state  # snapshot — state only committed after successful loop

        # Reply-driven auto-mark: if this message is a correlated reply to
        # a step in our active plan, mark that step done BEFORE the LLM runs,
        # so the rendered plan snapshot reflects reality.
        if message.plan_step_index is not None:
            self._auto_mark_step_on_reply(message.plan_step_index)

        # Record pending inbound Asks so a later reply-to-asker (possibly in
        # a different turn, e.g. nested A→B→C→B→A) auto-correlates back with
        # the original Ask's context.
        if (
            message.expects_reply
            and message.type == MessageType.DOMAIN
            and message.sender not in ("__user__", "__system__", "__external__", "__code__")
        ):
            with self._pending_inbound_lock:
                self._pending_inbound_asks[message.sender] = (message.id, message.plan_step_index)

        # Auto-close stale completed plans so LLM sees a fresh view.
        self._auto_close_plan_if_complete()

        tools_desc = self._tool_registry.describe() if self._tool_registry else ""
        sys_prompt = build_system_prompt(
            self._definition, self._state,
            tools=tools_desc,
            react_cross_objects=self._react_cross_objects,
            pending_timeout_seconds=self._pending_timeout_seconds,
            heartbeat_interval_seconds=self._heartbeat_interval_seconds,
            active_plan=self.active_plan,
            prompt_file=self._prompt_file,
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

        if finish.knowledge_gap is not None:
            extended_outgoing = list(finish.outgoing_messages or [])
            self._handle_knowledge_gap(finish.knowledge_gap, pending_deltas, extended_outgoing)
            finish = ReactFinish(
                reply=finish.reply,
                updated_state=finish.updated_state,
                outgoing_messages=extended_outgoing,
                updated_definition=finish.updated_definition,
                knowledge_gap=finish.knowledge_gap,
            )

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

        self._auto_create_plan_from_outgoing(finish.outgoing_messages or [], message)
        outgoing = self._correlate_outgoing(finish.outgoing_messages)
        self._auto_close_plan_if_complete()

        if finish.updated_definition:
            self._apply_definition_update(finish.updated_definition)
        self._history.append(message)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        return ProcessingResult(
            object_id=self.object_id,
            reply=finish.reply,
            outgoing_messages=outgoing,
            state_before=_coerce_state(state_before),
            state_after=_coerce_state(self._state),
            metrics=total_metrics,
            in_reply_to=message.sender,
            source_message_type=message.type,
            depth_remaining=message.depth_remaining,
            source_message_id=message.id,
            source_plan_step_index=message.plan_step_index,
        )

    # --- Plan application ---

    def _apply_plan_update(self, update: PlanUpdate) -> None:
        """Apply a plan update. Exactly one of three shapes per update:

        1. Create/replace: `goal` + `steps` — creates a new plan if none
           active, or replaces the active one. Existing step status from
           same-position steps is preserved when kind+target match.
        2. Incremental: `step_updates` / `add_steps` — modify the active plan.
        3. Close: `status = "complete" | "cancelled"` — terminate active.
        """
        # Shape 1: create or replace.
        if update.goal is not None and update.steps is not None:
            new_steps = [
                PlanStep(
                    kind=s.get("kind", ""),
                    description=s.get("description", ""),
                    target=s.get("target"),
                    status=s.get("status") or "planned",
                    result_summary=s.get("result_summary"),
                )
                for s in update.steps if isinstance(s, dict)
            ]
            # Drop invalid kinds.
            new_steps = [s for s in new_steps if s.kind in ("ask", "tell")]
            with self._plans_lock:
                if self._active_plan is not None:
                    # Replace: preserve status/result on same-position steps
                    # where kind+target still match (LLM giving a whole plan
                    # may intend to keep prior outcomes).
                    prev = self._active_plan
                    for i, ns in enumerate(new_steps):
                        if i < len(prev.steps):
                            ps = prev.steps[i]
                            if ps.kind == ns.kind and ps.target == ns.target and ns.status == "planned":
                                ns.status = ps.status
                                if ns.result_summary is None and ps.result_summary:
                                    ns.result_summary = ps.result_summary
                self._active_plan = Plan(goal=update.goal, steps=new_steps, status="active")
            return

        # Shape 3: close active plan.
        if update.status in PLAN_TERMINAL_STATUSES:
            with self._plans_lock:
                if self._active_plan is None:
                    logger.warning("Plan close for %s: no active plan — dropped", self.object_id)
                    return
                self._active_plan.status = update.status
                self._completed_plans.append(self._active_plan)
                self._active_plan = None
            return

        # Shape 2: incremental updates to active plan.
        with self._plans_lock:
            if self._active_plan is None:
                logger.warning("Plan incremental update for %s: no active plan — dropped", self.object_id)
                return
            plan = self._active_plan
            for su in update.step_updates or []:
                if not isinstance(su, dict):
                    continue
                idx = su.get("index")
                if not isinstance(idx, int) or idx < 0 or idx >= len(plan.steps):
                    logger.warning(
                        "Plan update for %s: step index %r out of range (%d steps) — dropped",
                        self.object_id, idx, len(plan.steps),
                    )
                    continue
                step = plan.steps[idx]
                status = su.get("status")
                if status in STEP_TERMINAL_STATUSES:
                    step.status = status
                elif status:
                    logger.warning(
                        "Plan update for %s step[%d]: status=%r not allowed (terminal only) — ignored",
                        self.object_id, idx, status,
                    )
                rs = su.get("result_summary")
                if rs is not None:
                    step.result_summary = rs
            for raw in update.add_steps or []:
                if not isinstance(raw, dict):
                    continue
                kind = raw.get("kind")
                if kind not in ("ask", "tell"):
                    continue
                plan.steps.append(PlanStep(
                    kind=kind,
                    description=raw.get("description", ""),
                    target=raw.get("target"),
                    status=raw.get("status") or "planned",
                    result_summary=raw.get("result_summary"),
                ))

    def _correlate_outgoing(self, outgoing):
        """Auto-stamp correlation on outgoing messages.

        Two matching paths, checked in order:
        1. Plan step match: first `planned` step of our active plan whose
           `target` equals the recipient AND whose `kind` matches
           `expects_reply`. Stamp plan_step_index. Tell steps → done on
           dispatch; Ask steps → dispatched.
        2. Pending inbound Ask: recipient has an outstanding Ask from us.
           Stamp `is_reply=True` and `in_reply_to` so the runtime delivers
           as a REPLY tied to the original Ask's correlation.
        """
        if not outgoing:
            return outgoing
        with self._plans_lock:
            plan = self._active_plan

        for out in outgoing:
            out.plan_step_index = None
            out.in_reply_to = None
            out.is_reply = False

            # Path 1 — plan step match.
            if plan is not None and plan.status == "active":
                wanted_kind = "ask" if out.expects_reply else "tell"
                recipient_lc = (out.recipient or "").strip().lower()
                matched_index = None
                with self._plans_lock:
                    for i, step in enumerate(plan.steps):
                        if step.status != "planned" or step.kind != wanted_kind:
                            continue
                        target_lc = (step.target or "").strip().lower()
                        if not target_lc:
                            continue
                        if (
                            target_lc == recipient_lc
                            or target_lc in recipient_lc
                            or recipient_lc in target_lc
                        ):
                            matched_index = i
                            break
                    # Fallback: single planned step of wanted kind in the plan.
                    if matched_index is None:
                        candidates = [
                            i for i, s in enumerate(plan.steps)
                            if s.status == "planned" and s.kind == wanted_kind
                        ]
                        if len(candidates) == 1:
                            matched_index = candidates[0]
                    if matched_index is not None:
                        step = plan.steps[matched_index]
                        out.plan_step_index = matched_index
                        # Tell steps are fire-and-forget — mark done on dispatch.
                        # Ask steps flip to 'dispatched' after bus send (runtime does this).
                        if step.kind == "tell":
                            step.status = "done"
                        continue

            # Path 2 — reply to a pending inbound Ask.
            with self._pending_inbound_lock:
                pending = self._pending_inbound_asks.pop(out.recipient, None)
            if pending is not None:
                mid, asker_step_index = pending
                out.in_reply_to = mid
                out.plan_step_index = asker_step_index
                out.is_reply = True
        return outgoing

    def _handle_knowledge_gap(
        self,
        gap: KnowledgeGap,
        pending_deltas: list[StateDelta],
        outgoing: list[OutgoingMessage],
    ) -> None:
        """Record a knowledge gap in state and optionally ask peers."""
        if self._auto_track_knowledge_gaps:
            pending_deltas.append(StateDelta(
                op="append",
                key="knowledge_gaps",
                value={"question": gap.question, "context": gap.context, "resolved": False},
            ))
        if self._auto_ask_peers_on_gap and self._definition.peers:
            for peer in self._definition.peers:
                outgoing.append(OutgoingMessage(
                    recipient=peer.object_id,
                    content=f"I don't know the answer to the following — do you? {gap.question}",
                    expects_reply=True,
                ))

    def _auto_mark_step_on_reply(self, step_index: int) -> None:
        """Runtime hook: when a correlated reply arrives tagged with a step
        index, mark that step done on the active plan (unless already terminal)."""
        with self._plans_lock:
            plan = self._active_plan
            if plan is None or step_index < 0 or step_index >= len(plan.steps):
                return
            step = plan.steps[step_index]
            if step.status not in STEP_TERMINAL_STATUSES:
                step.status = "done"
        self._auto_close_plan_if_complete()

    def _auto_create_plan_from_outgoing(self, outgoing: list, message: "Message") -> None:  # noqa: F821
        """Runtime-owned plan creation from outgoing messages.

        Creates (or extends) a plan only for new outgoing Ask messages.
        Skips recipients that are already in _pending_inbound_asks — those
        are replies and must go through path 2 in _correlate_outgoing, not
        be intercepted by a plan step.
        """
        # Snapshot pending inbound asks BEFORE acquiring plans lock to avoid
        # lock-ordering issues. The snapshot is a best-effort filter.
        with self._pending_inbound_lock:
            reply_recipients = set(self._pending_inbound_asks.keys())

        # Only create plan entries for messages that are genuine new outgoing
        # actions, not replies to inbound Asks.
        new_outgoing = [m for m in outgoing if m.recipient not in reply_recipients]
        ask_msgs = [m for m in new_outgoing if m.expects_reply]
        if not ask_msgs:
            return

        with self._plans_lock:
            if self._active_plan is None:
                steps = [
                    PlanStep(
                        kind="ask" if m.expects_reply else "tell",
                        description=f"{'Ask' if m.expects_reply else 'Tell'} {m.recipient}",
                        target=m.recipient,
                        status="planned",
                    )
                    for m in new_outgoing
                ]
                self._active_plan = Plan(
                    goal=f"Handle: {message.content[:60]}",
                    steps=steps,
                    status="active",
                )
            else:
                # Extend with steps for genuinely new targets (avoid duplicates).
                plan = self._active_plan
                existing = {
                    ((s.target or "").strip().lower(), s.kind)
                    for s in plan.steps
                }
                for m in new_outgoing:
                    key = ((m.recipient or "").strip().lower(), "ask" if m.expects_reply else "tell")
                    if key not in existing:
                        plan.steps.append(PlanStep(
                            kind=key[1],
                            description=f"{'Ask' if m.expects_reply else 'Tell'} {m.recipient}",
                            target=m.recipient,
                            status="planned",
                        ))
                        existing.add(key)

    def _auto_close_plan_if_complete(self) -> None:
        """If the active plan has at least one step and ALL steps are terminal,
        close the plan automatically (status='complete')."""
        with self._plans_lock:
            plan = self._active_plan
            if plan is None or not plan.steps:
                return
            all_terminal = all(s.status in STEP_TERMINAL_STATUSES for s in plan.steps)
            if all_terminal:
                plan.status = "complete"
                self._completed_plans.append(plan)
                self._active_plan = None

    def mark_step_dispatched(self, step_index: int) -> None:
        """Runtime hook: after a plan-tagged outgoing goes on the bus, flip
        the step from 'planned' to 'dispatched' (Ask steps only; Tell steps
        are already 'done' via auto-correlation)."""
        now = datetime.datetime.now(datetime.timezone.utc)  # noqa: F841 (future use)
        with self._plans_lock:
            plan = self._active_plan
            if plan is None or step_index < 0 or step_index >= len(plan.steps):
                return
            step = plan.steps[step_index]
            if step.status == "planned":
                step.status = "dispatched"

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
        with self._plans_lock:
            plan_snap = asdict(self._active_plan) if self._active_plan else None
            completed_snap = [asdict(p) for p in self._completed_plans]
        return {
            "object_id": self.object_id,
            "state": _coerce_state(self._state),
            "definition": asdict(self._definition),
            "history_length": len(self._history),
            "active_plan": plan_snap,
            "completed_plans": completed_snap,
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
