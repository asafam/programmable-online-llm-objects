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
