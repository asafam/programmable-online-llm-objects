"""EventGateway — dispatches external events to Runtime-managed event sources."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

from .types import Message, MessageType

if TYPE_CHECKING:
    from .runtime import Runtime
    from .types import ProcessingResult


class EventGateway:
    """Dispatches external events to Runtime-managed event sources.

    Sits between the external world (test harness, webhooks, adapters) and
    the Runtime. Resolves which event source to fire into based on the
    object's declared event_sources.
    """

    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    def dispatch(
        self,
        recipient: str,
        content: str,
        descriptor: str | None = None,
        source: str = "__external__",
    ) -> list[ProcessingResult]:
        """Route an external event to the recipient's event source and process.

        Resolution order:
        1. If descriptor provided, use that specific event source
        2. Otherwise, use the object's first declared event source
        3. Fall back to inject_event if no event source exists
        """
        event_source = None

        if descriptor is not None:
            event_source = self._rt.get_event_source(recipient, descriptor)
        else:
            # Look up first declared event source for this object
            registry = self._rt.event_registry
            descriptors = registry.get(recipient)
            if descriptors:
                event_source = self._rt.get_event_source(recipient, descriptors[0])

        if event_source is not None:
            event_source.fire(content, source=source)
            return self._rt.process_pending()

        return self._rt.inject_event(recipient, content, source=source)

    def dispatch_many(
        self,
        items: list[tuple[str, str, str]],
        on_result: Optional[Callable[[ProcessingResult], None]] = None,
    ) -> list[ProcessingResult]:
        """Dispatch multiple external events simultaneously in one transaction.

        Each item is (recipient, content, source). All EVENT-type messages are
        built upfront (with known IDs) and dispatched in a single transaction —
        true concurrent dispatch without serialization between items.

        Args:
            on_result: optional callback fired for each direct result of an input
                message (filtered by source_message_id; cascades are excluded).
        """
        rt = self._rt
        messages = [
            Message(
                sender=source,
                recipient=recipient,
                type=MessageType.EVENT,
                content=content,
                depth_remaining=rt._max_chain_depth,
                id=rt._next_msg_id(source),
            )
            for recipient, content, source in items
        ]
        if on_result is not None:
            input_ids = {m.id for m in messages}
            def _filtered(result: ProcessingResult, _ids: set = input_ids, _cb = on_result) -> None:
                if result.source_message_id in _ids:
                    _cb(result)
            return rt._dispatch(messages, on_result=_filtered)
        return rt._dispatch(messages)
