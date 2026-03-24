"""Tool execution for agent LLM-objects."""
from __future__ import annotations

import contextlib
import io
from typing import Any, Protocol

from .types import ToolCall, ToolResult


class ToolExecutor(Protocol):
    """Executes a single tool call and returns a result."""

    def execute(self, call: ToolCall, context: dict[str, Any]) -> ToolResult: ...


class CodeExecutor:
    """Executes Python code in a restricted namespace with a push_event callback."""

    def execute(self, call: ToolCall, context: dict[str, Any]) -> ToolResult:
        code = call.arguments.get("code", "")
        stdout = io.StringIO()

        namespace: dict[str, Any] = {
            "push_event": context.get("push_event"),
            "connect": context.get("connect"),
            "print": lambda *a, **kw: print(*a, file=stdout, **kw),
        }

        try:
            with contextlib.redirect_stdout(stdout):
                exec(code, namespace)
            return ToolResult(id=call.id, output=stdout.getvalue())
        except Exception as e:
            return ToolResult(id=call.id, output=stdout.getvalue(), error=str(e))


class MockToolExecutor:
    """Scripted tool executor for testing."""

    def __init__(self) -> None:
        self._responses: list[ToolResult] = []
        self.call_log: list[ToolCall] = []

    def script(self, output: str, error: str = "") -> None:
        """Add a scripted result to the queue."""
        self._responses.append(ToolResult(id="", output=output, error=error))

    def execute(self, call: ToolCall, context: dict[str, Any]) -> ToolResult:
        self.call_log.append(call)
        if self._responses:
            result = self._responses.pop(0)
            result.id = call.id
            return result
        return ToolResult(id=call.id, output="(mock: no scripted response)")


class ToolRegistry:
    """Maps tool names to executors."""

    def __init__(self) -> None:
        self._executors: dict[str, ToolExecutor] = {}

    def register(self, name: str, executor: ToolExecutor) -> None:
        self._executors[name] = executor

    def execute(self, call: ToolCall, context: dict[str, Any]) -> ToolResult:
        executor = self._executors.get(call.tool)
        if executor is None:
            return ToolResult(id=call.id, output="", error=f"Unknown tool: {call.tool}")
        return executor.execute(call, context)

    @property
    def tool_names(self) -> list[str]:
        return list(self._executors.keys())
