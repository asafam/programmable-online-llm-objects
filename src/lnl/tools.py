"""Tool execution for agent LLM-objects."""
from __future__ import annotations

import contextlib
import io
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .types import ToolCall, ToolResult


class ToolExecutor(Protocol):
    """Executes a single tool call and returns a result."""

    def execute(self, call: ToolCall, context: dict[str, Any]) -> ToolResult: ...


@dataclass
class ToolSpec:
    """Metadata for a registered tool — used to build system prompt and JSON schema."""

    description: str
    arguments_schema: dict = field(default_factory=dict)  # JSON Schema for the arguments object


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


class MockInProcessExecutor:
    """In-process mock executor for evaluation.

    Returns scripted responses to the calling LLM-object (FIFO, falls back to
    ``response_template`` when exhausted) and dispatches cross-object events via
    ``inject_event`` in the tool context.

    Each call is assigned a 1-based ``call_index`` tracking position in the mock
    chain. Templates may use ``{call_index}`` alongside argument names:
        response: "Ticket #{call_index} created for {subject}"
    """

    def __init__(self, tool_def: Any) -> None:  # tool_def: MockToolDef (schema.py)
        self._tool_def = tool_def
        self._call_count: int = 0
        self.call_log: list[dict] = []

    @property
    def spec(self) -> "ToolSpec":
        return ToolSpec(
            description=self._tool_def.description,
            arguments_schema=self._tool_def.arguments_schema,
        )

    def execute(self, call: "ToolCall", context: dict[str, Any]) -> "ToolResult":
        args = call.arguments
        self._call_count += 1
        call_index = self._call_count  # 1-based, tracks position in mock chain

        log_entry: dict = {
            "tool": call.tool,
            "id": call.id,
            "call_index": call_index,
            "arguments": args,
        }
        self.call_log.append(log_entry)

        # Pick response (priority order):
        #   1. scripted_responses FIFO (index-based)
        #   2. scripted_match_responses (first arg-pattern match)
        #   3. response_template fallback
        interp_vars = {**args, "call_index": call_index}
        scripted = self._tool_def.scripted_responses
        if call_index <= len(scripted):
            template = scripted[call_index - 1]
        else:
            template = next(
                (smr.response for smr in self._tool_def.scripted_match_responses
                 if self._arg_matches(args, smr.match)),
                self._tool_def.response_template,
            )
        try:
            response_text = template.format(**interp_vars)
        except KeyError:
            response_text = template
        log_entry["response"] = response_text

        # Fire orchestration triggers when match conditions are satisfied
        if self._matches(args):
            inject = context.get("inject_event")
            if inject:
                for trigger in self._tool_def.triggers:
                    try:
                        msg = trigger.message_template.format(**interp_vars)
                    except KeyError:
                        msg = trigger.message_template
                    inject(trigger.target_object_id, msg, trigger.source)
                    log_entry.setdefault("triggered", []).append(
                        {"target": trigger.target_object_id, "message": msg}
                    )

        return ToolResult(id=call.id, output=response_text)

    def _matches(self, args: dict) -> bool:
        """Return True if all match conditions pass (empty match always passes)."""
        for key, pattern in self._tool_def.match.items():
            if not re.search(pattern, str(args.get(key, ""))):
                return False
        return True

    @staticmethod
    def _arg_matches(args: dict, match: dict) -> bool:
        for key, pattern in match.items():
            if not re.search(pattern, str(args.get(key, ""))):
                return False
        return True


class PassthroughExecutor:
    """Catch-all tool executor for evaluation.

    Registered as a ToolRegistry fallback so unknown tool calls succeed silently
    instead of returning an error. All calls are logged for judge evidence.
    """

    def __init__(self) -> None:
        self._call_counts: dict[str, int] = {}  # tool_name → call count
        self.call_log: list[dict] = []

    def execute(self, call: "ToolCall", context: dict[str, Any]) -> "ToolResult":
        self._call_counts[call.tool] = self._call_counts.get(call.tool, 0) + 1
        call_index = self._call_counts[call.tool]
        self.call_log.append({
            "tool": call.tool,
            "id": call.id,
            "call_index": call_index,
            "arguments": call.arguments,
        })
        # Data-lookup tools (by _data suffix convention) return an empty but valid
        # JSON object so the LLM can handle missing data gracefully rather than
        # treating the call as a hard failure.
        if call.tool.endswith("_data"):
            output = "{}"
        else:
            output = f"[mock] {call.tool} executed successfully."
        return ToolResult(id=call.id, output=output)


class CreateObjectExecutor:
    """Executor for the built-in create_object tool.

    Holds a reference to the runtime so it can call runtime.spawn() directly,
    without going through the tool context.
    """

    SPEC = ToolSpec(
        description="Instantiate a new object from a registered llm-class",
        arguments_schema={
            "type": "object",
            "properties": {
                "object_id": {"type": "string", "description": "Unique ID for the new object (e.g. 'truck-001')"},
                "class_id": {"type": "string", "description": "Name of the registered llm-class to instantiate (e.g. 'truck')"},
                "params": {"type": "object", "description": "Parameter values substituted into the class template placeholders", "additionalProperties": True},
            },
            "required": ["object_id", "class_id"],
            "additionalProperties": False,
        },
    )

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def execute(self, call: ToolCall, context: dict[str, Any]) -> ToolResult:
        args = call.arguments
        object_id = args.get("object_id", "")
        class_id = args.get("class_id", "")
        params = args.get("params") or {}
        try:
            self._runtime.spawn(object_id, class_id, params)
            # Fire an init event so the object executes its "upon creation" behavior,
            # analogous to a constructor running when `new` is called.
            inject = context.get("inject_event")
            if inject:
                params_desc = ", ".join(f"{k}={v}" for k, v in params.items()) if params else ""
                init_msg = f"[system] You have been created from class '{class_id}'."
                if params_desc:
                    init_msg += f" Params: {params_desc}."
                init_msg += " Begin your initialization behavior now."
                inject(object_id, init_msg, "__system__")
            return ToolResult(id=call.id, output=f"Created {object_id} from class {class_id}")
        except Exception as exc:
            return ToolResult(id=call.id, output="", error=str(exc))


class ToolRegistry:
    """Maps tool names to executors and their specs."""

    def __init__(self) -> None:
        self._executors: dict[str, ToolExecutor] = {}
        self._specs: dict[str, ToolSpec] = {}
        self._fallback: ToolExecutor | None = None

    def register(self, name: str, executor: ToolExecutor, spec: ToolSpec | None = None) -> None:
        self._executors[name] = executor
        self._specs[name] = spec or ToolSpec(description=name)

    def register_fallback(self, executor: ToolExecutor) -> None:
        """Register a catch-all executor used when a named tool is not found."""
        self._fallback = executor

    def execute(self, call: ToolCall, context: dict[str, Any]) -> ToolResult:
        executor = self._executors.get(call.tool)
        if executor is None:
            if self._fallback is not None:
                return self._fallback.execute(call, context)
            return ToolResult(id=call.id, output="", error=f"Unknown tool: {call.tool}")
        return executor.execute(call, context)

    def describe(self) -> str:
        """Return a text description of tools for injection into the system prompt."""
        lines = []
        for name, spec in self._specs.items():
            lines.append(f"- {name}: {spec.description}")
            props = spec.arguments_schema.get("properties", {})
            if props:
                args_str = ", ".join(
                    f"{k} ({v.get('type', 'any')})" for k, v in props.items()
                )
                lines.append(f"  Arguments: {args_str}")
        return "\n".join(lines)

    def calls_schema(self) -> dict:
        """Return a JSON Schema for a single tool_calls array item, derived from registered tools."""
        if not self._specs:
            return {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "tool": {"type": "string"},
                    "arguments": {"type": "object", "additionalProperties": True},
                },
                "required": ["id", "tool", "arguments"],
                "additionalProperties": False,
            }

        tool_variants = []
        for name, spec in self._specs.items():
            args_schema = spec.arguments_schema or {"type": "object", "additionalProperties": True}
            tool_variants.append({
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "tool": {"type": "string", "enum": [name]},
                    "arguments": args_schema,
                },
                "required": ["id", "tool", "arguments"],
                "additionalProperties": False,
            })

        if len(tool_variants) == 1:
            return tool_variants[0]

        return {"oneOf": tool_variants}

    @property
    def tool_names(self) -> list[str]:
        return list(self._executors.keys())
