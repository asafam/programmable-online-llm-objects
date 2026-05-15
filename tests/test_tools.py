"""Tests for tool execution infrastructure."""
import pytest

from src.lnl.tools import CodeExecutor, MockToolExecutor, ToolRegistry
from src.lnl.types import ToolCall, ToolResult


class TestCodeExecutor:
    def test_simple_code(self):
        executor = CodeExecutor()
        call = ToolCall(id="t1", tool="execute_code", arguments={"code": "print('hello')"})
        result = executor.execute(call, {})
        assert result.id == "t1"
        assert result.output.strip() == "hello"
        assert result.error == ""

    def test_exception_captured(self):
        executor = CodeExecutor()
        call = ToolCall(id="t2", tool="execute_code", arguments={"code": "raise ValueError('boom')"})
        result = executor.execute(call, {})
        assert "boom" in result.error

    def test_push_event_callback(self):
        events = []
        executor = CodeExecutor()
        call = ToolCall(id="t3", tool="execute_code", arguments={
            "code": "push_event('hello from code', 'my-source')"
        })
        result = executor.execute(call, {"push_event": lambda content, source: events.append((content, source))})
        assert result.error == ""
        assert events == [("hello from code", "my-source")]

    def test_empty_code(self):
        executor = CodeExecutor()
        call = ToolCall(id="t4", tool="execute_code", arguments={"code": ""})
        result = executor.execute(call, {})
        assert result.error == ""
        assert result.output == ""

    def test_partial_output_on_error(self):
        executor = CodeExecutor()
        call = ToolCall(id="t5", tool="execute_code", arguments={
            "code": "print('before')\nraise RuntimeError('after')"
        })
        result = executor.execute(call, {})
        assert "before" in result.output
        assert "after" in result.error

    def test_namespace_persists_across_calls(self):
        executor = CodeExecutor()
        ns: dict = {}
        ctx = {"repl_namespace": ns}
        r1 = executor.execute(ToolCall(id="t1", tool="python", arguments={"code": "x = 7"}), ctx)
        assert r1.error == ""
        r2 = executor.execute(ToolCall(id="t2", tool="python", arguments={"code": "print(x)"}), ctx)
        assert r2.error == ""
        assert r2.output.strip() == "7"

    def test_imports_persist_across_calls(self):
        executor = CodeExecutor()
        ns: dict = {}
        ctx = {"repl_namespace": ns}
        r1 = executor.execute(ToolCall(id="t1", tool="python", arguments={"code": "import json"}), ctx)
        assert r1.error == ""
        r2 = executor.execute(
            ToolCall(id="t2", tool="python", arguments={"code": "print(json.dumps({'a': 1}))"}),
            ctx,
        )
        assert r2.error == ""
        assert r2.output.strip() == '{"a": 1}'

    def test_function_defs_persist(self):
        executor = CodeExecutor()
        ns: dict = {}
        ctx = {"repl_namespace": ns}
        executor.execute(
            ToolCall(id="t1", tool="python", arguments={"code": "def double(n):\n    return n * 2"}),
            ctx,
        )
        r2 = executor.execute(
            ToolCall(id="t2", tool="python", arguments={"code": "double(21)"}),
            ctx,
        )
        assert r2.error == ""
        assert r2.output.strip() == "42"

    def test_last_expression_value_captured(self):
        executor = CodeExecutor()
        ctx = {"repl_namespace": {}}
        r = executor.execute(ToolCall(id="t1", tool="python", arguments={"code": "2 + 3"}), ctx)
        assert r.error == ""
        assert r.output.strip() == "5"

    def test_assignment_does_not_print(self):
        executor = CodeExecutor()
        ctx = {"repl_namespace": {}}
        r = executor.execute(ToolCall(id="t1", tool="python", arguments={"code": "x = 5"}), ctx)
        assert r.error == ""
        assert r.output == ""

    def test_stdout_combined_with_expression(self):
        executor = CodeExecutor()
        ctx = {"repl_namespace": {}}
        r = executor.execute(
            ToolCall(id="t1", tool="python", arguments={"code": "print('hi')\n1 + 1"}),
            ctx,
        )
        assert r.error == ""
        assert "hi" in r.output
        assert "2" in r.output

    def test_namespace_survives_exception(self):
        executor = CodeExecutor()
        ns: dict = {}
        ctx = {"repl_namespace": ns}
        executor.execute(ToolCall(id="t1", tool="python", arguments={"code": "y = 99"}), ctx)
        # An exception in a later call must not wipe the namespace
        executor.execute(
            ToolCall(id="t2", tool="python", arguments={"code": "raise ValueError('boom')"}),
            ctx,
        )
        r3 = executor.execute(ToolCall(id="t3", tool="python", arguments={"code": "y"}), ctx)
        assert r3.error == ""
        assert r3.output.strip() == "99"

    def test_push_event_seeded_in_repl_namespace(self):
        events = []
        executor = CodeExecutor()
        ctx = {
            "repl_namespace": {},
            "push_event": lambda content, source: events.append((content, source)),
        }
        r = executor.execute(
            ToolCall(id="t1", tool="python", arguments={"code": "push_event('hi', 's')"}),
            ctx,
        )
        assert r.error == ""
        assert events == [("hi", "s")]


class TestMockToolExecutor:
    def test_scripted_response(self):
        mock = MockToolExecutor()
        mock.script("result-1")
        call = ToolCall(id="t1", tool="test", arguments={})
        result = mock.execute(call, {})
        assert result.output == "result-1"
        assert result.id == "t1"

    def test_call_log(self):
        mock = MockToolExecutor()
        mock.script("ok")
        call = ToolCall(id="t1", tool="test", arguments={"code": "x"})
        mock.execute(call, {})
        assert len(mock.call_log) == 1
        assert mock.call_log[0].arguments == {"code": "x"}

    def test_exhausted_scripts(self):
        mock = MockToolExecutor()
        call = ToolCall(id="t1", tool="test", arguments={})
        result = mock.execute(call, {})
        assert "no scripted response" in result.output

    def test_fifo_order(self):
        mock = MockToolExecutor()
        mock.script("first")
        mock.script("second")
        call = ToolCall(id="t1", tool="test", arguments={})
        assert mock.execute(call, {}).output == "first"
        assert mock.execute(call, {}).output == "second"


class TestToolRegistry:
    def test_routes_to_executor(self):
        reg = ToolRegistry()
        mock = MockToolExecutor()
        mock.script("ok")
        reg.register("my_tool", mock)

        call = ToolCall(id="t1", tool="my_tool", arguments={})
        result = reg.execute(call, {})
        assert result.output == "ok"

    def test_unknown_tool(self):
        reg = ToolRegistry()
        call = ToolCall(id="t1", tool="nonexistent", arguments={})
        result = reg.execute(call, {})
        assert "Unknown tool" in result.error

    def test_tool_names(self):
        reg = ToolRegistry()
        mock = MockToolExecutor()
        reg.register("a", mock)
        reg.register("b", mock)
        assert set(reg.tool_names) == {"a", "b"}
