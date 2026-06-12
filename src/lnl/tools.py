"""Tool execution for agent LLM-objects."""
from __future__ import annotations

import ast
import contextlib
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .types import ToolCall, ToolResult

logger = logging.getLogger(__name__)


class ToolExecutor(Protocol):
    """Executes a single tool call and returns a result."""

    def execute(self, call: ToolCall, context: dict[str, Any]) -> ToolResult: ...


@dataclass
class ToolSpec:
    """Metadata for a registered tool — used to build system prompt and JSON schema."""

    description: str
    arguments_schema: dict = field(default_factory=dict)  # JSON Schema for the arguments object


class CodeExecutor:
    """Executes Python code in a per-object persistent REPL namespace.

    When ``context["repl_namespace"]`` is supplied, that dict is used as the
    namespace — variables, imports, and function defs persist across calls.
    Without it, a fresh namespace is built per call (legacy single-shot mode).
    Callback hooks (``push_event``, ``connect``, ``inject_event``) are seeded
    on first use only; user code may rebind them.

    Output capture is Jupyter-like: stdout is collected, and if the source
    ends with an expression, ``repr(value)`` of that expression is appended
    when the value is not ``None``.
    """

    _CALLBACK_KEYS = ("push_event", "connect", "inject_event")

    def execute(self, call: ToolCall, context: dict[str, Any]) -> ToolResult:
        code = call.arguments.get("code", "")
        stdout = io.StringIO()

        namespace = context.get("repl_namespace")
        if namespace is None:
            namespace = {}
        self._seed_namespace(namespace, context, stdout)

        try:
            with contextlib.redirect_stdout(stdout):
                tail_value = self._exec_with_expression_capture(code, namespace)
            output = stdout.getvalue()
            if tail_value is not None:
                if output and not output.endswith("\n"):
                    output += "\n"
                output += repr(tail_value)
            return ToolResult(id=call.id, output=output)
        except Exception as e:
            return ToolResult(id=call.id, output=stdout.getvalue(), error=str(e))

    def _seed_namespace(self, ns: dict[str, Any], context: dict[str, Any], stdout: io.StringIO) -> None:
        for key in self._CALLBACK_KEYS:
            if key not in ns and context.get(key) is not None:
                ns[key] = context.get(key)
        # Always rebind ``print`` to the call-local stdout buffer so output
        # capture works regardless of any prior rebinding by user code.
        ns["print"] = lambda *a, **kw: print(*a, file=stdout, **kw)

    @staticmethod
    def _exec_with_expression_capture(code: str, ns: dict[str, Any]) -> Any:
        """Exec ``code`` in ``ns``; if it ends with an expression, return its value."""
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError:
            exec(code, ns)
            return None
        if not tree.body:
            return None
        if isinstance(tree.body[-1], ast.Expr):
            body = ast.Module(body=tree.body[:-1], type_ignores=[])
            tail = ast.Expression(body=tree.body[-1].value)
            if body.body:
                exec(compile(body, "<repl>", "exec"), ns)
            return eval(compile(tail, "<repl>", "eval"), ns)
        exec(compile(tree, "<repl>", "exec"), ns)
        return None


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
    """Mock executor for evaluation.

    Returns scripted responses to the calling LLM-object (FIFO, falls back to
    ``response_template`` when exhausted).

    Two response modes:
      - In-process (default): the response is computed locally from
        ``scripted_responses`` / ``scripted_match_responses`` / ``response_template``.
      - Remote (``remote_url`` set): the response is served by the HTTP mock
        server — the same ``POST /tool/{method}`` path the OpenClaw baseline
        uses — so LNL and the baseline exercise an identical tool-call surface.

    Each call is assigned a 1-based ``call_index`` tracking position in the mock
    chain. Templates may use ``{call_index}`` alongside argument names:
        response: "Ticket #{call_index} created for {subject}"
    """

    def __init__(
        self,
        tool_def: Any,  # tool_def: MockToolDef (schema.py)
        remote_url: "str | None" = None,
        slot_id: str = "default",
    ) -> None:
        self._tool_def = tool_def
        self._call_count: int = 0
        self.call_log: list[dict] = []
        # When set, responses are fetched from the HTTP mock server instead of
        # computed in-process. slot_id isolates concurrent (tc, run) pairs.
        self._remote_url = remote_url.rstrip("/") if remote_url else None
        self._slot_id = slot_id

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

        # Reject GARBAGE values in provided fields (a real API would): emails to "unknown",
        # empty identifiers. Presence is NOT enforced — forcing optional fields (hold_reason on
        # an assigned lead) caused blank-fill/reject ping-pong loops. Only fields the agent
        # chose to send are validated.
        # "" is allowed: an empty optional field is a natural "not applicable" (e.g.
        # hold_reason on an assigned lead) — rejecting it forced junk-fill retries.
        _BLANK = {"unknown", "unknown@company.example", "n/a", "none", "null", "tbd",
                  "placeholder"}
        bad = [k for k, v in args.items()
               if isinstance(v, str) and v.strip().lower() in _BLANK]
        if bad:
            return ToolResult(
                id=call.id, output="",
                error=f"invalid arguments: field(s) have blank/placeholder values: "
                      f"{', '.join(bad)}. Provide concrete values (from your state, the message, "
                      f"or your read-service peers) — or omit fields you cannot fill — and call "
                      f"the tool again.")

        interp_vars = {**args, "call_index": call_index}

        if self._remote_url is not None:
            # Remote mode: the HTTP mock server picks and interpolates the
            # response (same path as the OpenClaw baseline).
            response_text = self._fetch_remote(call.tool, args)
        else:
            # In-process mode: pick response (priority order):
            #   1. scripted_responses FIFO (index-based)
            #   2. scripted_match_responses (first arg-pattern match)
            #   3. response_template fallback
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
        return ToolResult(id=call.id, output=response_text)

    def _fetch_remote(self, method: str, args: dict) -> str:
        """Fetch the tool response from the HTTP mock server's POST /tool/{method}
        endpoint. Raises on failure — the ReAct loop's tool-execution handler
        turns that into a visible error result rather than masking it."""
        import httpx

        url = f"{self._remote_url}/tool/{method}"
        resp = httpx.post(
            url, json={**args, "__slot_id__": self._slot_id}, timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json().get("result", "")

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

    def __init__(self, object_ids: "set[str] | None" = None) -> None:
        self._call_counts: dict[str, int] = {}  # tool_name → call count
        self.call_log: list[dict] = []
        # Object ids in the running graph: a "tool call" naming one of these is a
        # peer interaction the model mis-routed. Silently succeeding lies to the
        # model — the peer never hears anything — and the wave dies spinning
        # between fake tool successes and evaluator FAILs.
        self._object_ids = set(object_ids or ())

    def execute(self, call: "ToolCall", context: dict[str, Any]) -> "ToolResult":
        self._call_counts[call.tool] = self._call_counts.get(call.tool, 0) + 1
        call_index = self._call_counts[call.tool]
        self.call_log.append({
            "tool": call.tool,
            "id": call.id,
            "call_index": call_index,
            "arguments": call.arguments,
        })
        if call.tool in self._object_ids:
            return ToolResult(
                id=call.id, output="",
                error=(f"'{call.tool}' is a peer OBJECT, not a tool — this call reached "
                       f"nobody. To interact with it, send it a message "
                       f"(outgoing_messages / ask {call.tool}: ...) and act on its reply."))
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

    def has_fallback(self) -> bool:
        """True when a catch-all fallback executor is registered."""
        return self._fallback is not None

    def names(self) -> list:
        """Registered tool names."""
        return list(self._specs.keys())

    def describe(self, allowed: "set[str] | None" = None) -> str:
        """Return a text description of tools for injection into the system prompt.

        allowed: when given, only these tools are described and the open-ended
        fallback hint is omitted — objects see ONLY the tools their skills
        grant (every object seeing every tool let entry services freelance the
        write-sinks' jobs with naive arguments).
        """
        lines = []
        for name, spec in self._specs.items():
            if allowed is not None and name not in allowed:
                continue
            lines.append(f"- {name}: {spec.description}")
            props = spec.arguments_schema.get("properties", {})
            if props:
                args_str = ", ".join(
                    f"{k} ({v.get('type', 'any')})" for k, v in props.items()
                )
                lines.append(f"  Arguments: {args_str}")
        if self._fallback is not None and allowed is None:
            lines.append(
                "- <any other tool name>: Call any domain-specific tool by name "
                "(e.g. send_email, create_deal, post_slack_message, append_row). "
                "All tool calls succeed — use the name that matches the action you need to take."
            )
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
