"""Tests for MockService, MockRegistry, and MockInProcessExecutor."""
import pytest

from src.lnl.mocks import MockRegistry, MockService


class TestMockService:
    def test_scripted_response(self):
        svc = MockService(name="email")
        svc.script_response("send", {"status": "sent"})

        result = svc.handle_call("send", {"to": "user@example.com"})

        assert result == {"status": "sent"}
        assert len(svc.recordings) == 1
        assert svc.recordings[0].method == "send"
        assert svc.recordings[0].args == {"to": "user@example.com"}

    def test_multiple_scripted_responses_consumed_in_order(self):
        svc = MockService(name="api")
        svc.script_response("get", {"page": 1})
        svc.script_response("get", {"page": 2})

        r1 = svc.handle_call("get")
        r2 = svc.handle_call("get")

        assert r1 == {"page": 1}
        assert r2 == {"page": 2}

    def test_fallback_returns_state(self):
        svc = MockService(name="db")
        svc.set_state("count", 42)

        result = svc.handle_call("query")

        assert result == {"count": 42}

    def test_state_operations(self):
        svc = MockService(name="store")
        svc.set_state("key", "value")
        assert svc.get_state("key") == "value"
        assert svc.get_state("missing", "default") == "default"

    def test_clear_recordings(self):
        svc = MockService(name="svc")
        svc.handle_call("method")
        assert len(svc.recordings) == 1
        svc.clear_recordings()
        assert len(svc.recordings) == 0


class TestMockRegistry:
    def test_add_and_get_service(self):
        reg = MockRegistry()
        svc = reg.add_service("email")
        assert reg.get_service("email") is svc
        assert reg.get_service("missing") is None

    def test_handle_call_routes_to_service(self):
        reg = MockRegistry()
        svc = reg.add_service("api")
        svc.script_response("get", {"data": 1})

        result = reg.handle_call("api", "get")
        assert result == {"data": 1}

    def test_unknown_service_raises(self):
        reg = MockRegistry()
        import pytest
        with pytest.raises(KeyError, match="Unknown service"):
            reg.handle_call("nonexistent", "method")

    def test_scheduled_events(self):
        reg = MockRegistry()
        reg.schedule_event(step=2, target="sensor", content="temperature=30")
        reg.schedule_event(step=2, target="alarm", content="check")
        reg.schedule_event(step=3, target="sensor", content="temperature=35")

        events_1 = reg.advance()  # step 1
        assert len(events_1) == 0

        events_2 = reg.advance()  # step 2
        assert len(events_2) == 2
        targets = {e.target for e in events_2}
        assert targets == {"sensor", "alarm"}

        events_3 = reg.advance()  # step 3
        assert len(events_3) == 1
        assert events_3[0].target == "sensor"

    def test_all_recordings(self):
        reg = MockRegistry()
        reg.add_service("a")
        reg.add_service("b")
        reg.handle_call("a", "method1")
        reg.handle_call("b", "method2")

        recs = reg.all_recordings()
        assert len(recs["a"]) == 1
        assert len(recs["b"]) == 1


# ── MockInProcessExecutor scripted_match_responses tests ──────────────────────

class TestMockInProcessExecutorMatchResponses:
    def _make_def(self, **kwargs):
        from src.data.schema import MockToolDef
        return MockToolDef(
            tool_name="slack.send_message",
            description="Send a Slack message.",
            arguments_schema={"type": "object", "properties": {"channel": {"type": "string"}}},
            response_template="fallback response",
            **kwargs,
        )

    def _make_call(self, id="t1", args=None):
        from src.lnl.types import ToolCall
        return ToolCall(id=id, tool="slack.send_message", arguments=args or {"channel": "general"})

    def test_arg_match_takes_priority_over_response_template(self):
        from src.data.schema import ScriptedMatchResponse
        from src.lnl.tools import MockInProcessExecutor
        executor = MockInProcessExecutor(self._make_def(
            scripted_match_responses=[
                ScriptedMatchResponse(match={"channel": "urgent"}, response="URGENT handled"),
            ],
        ))
        result = executor.execute(self._make_call(args={"channel": "urgent-alerts"}), {})
        assert result.output == "URGENT handled"

    def test_index_scripted_takes_priority_over_match(self):
        from src.data.schema import ScriptedMatchResponse
        from src.lnl.tools import MockInProcessExecutor
        executor = MockInProcessExecutor(self._make_def(
            scripted_responses=["scripted #1"],
            scripted_match_responses=[
                ScriptedMatchResponse(match={"channel": ".*"}, response="match response"),
            ],
        ))
        result = executor.execute(self._make_call(args={"channel": "general"}), {})
        assert result.output == "scripted #1"

    def test_falls_back_to_response_template_when_no_match(self):
        from src.data.schema import ScriptedMatchResponse
        from src.lnl.tools import MockInProcessExecutor
        executor = MockInProcessExecutor(self._make_def(
            scripted_match_responses=[
                ScriptedMatchResponse(match={"channel": "urgent"}, response="URGENT"),
            ],
        ))
        result = executor.execute(self._make_call(args={"channel": "general"}), {})
        assert result.output == "fallback response"

    def test_first_matching_entry_wins(self):
        from src.data.schema import ScriptedMatchResponse
        from src.lnl.tools import MockInProcessExecutor
        executor = MockInProcessExecutor(self._make_def(
            scripted_match_responses=[
                ScriptedMatchResponse(match={"channel": "deals"}, response="deals match"),
                ScriptedMatchResponse(match={"channel": ".*"}, response="catch-all"),
            ],
        ))
        result = executor.execute(self._make_call(args={"channel": "deals"}), {})
        assert result.output == "deals match"

    def test_match_response_interpolation(self):
        from src.data.schema import ScriptedMatchResponse
        from src.lnl.tools import MockInProcessExecutor
        executor = MockInProcessExecutor(self._make_def(
            scripted_match_responses=[
                ScriptedMatchResponse(match={"channel": ".*"}, response="Sent to #{channel}"),
            ],
        ))
        result = executor.execute(self._make_call(args={"channel": "deals"}), {})
        assert result.output == "Sent to #deals"


# ── MockInProcessExecutor trigger tests ──────────────────────────────────────

class TestMockInProcessExecutorTriggers:
    """Tests for the trigger contract on MockInProcessExecutor.

    Trigger orchestration (dispatching cross-object events when a tool is called)
    is owned by the evaluator, not the executor. The executor's only job is to
    return a scripted response and log the call. Tests here verify that the
    executor stays clean of trigger dispatch, and that trigger metadata is
    accessible to the evaluator via tool_def.triggers.
    """

    def _make_def(self, triggers=None, match=None, **kwargs):
        from src.data.schema import MockToolDef
        return MockToolDef(
            tool_name="email.send",
            description="Send an email.",
            arguments_schema={"type": "object", "properties": {
                "to":      {"type": "string"},
                "subject": {"type": "string"},
                "body":    {"type": "string"},
            }},
            response_template="email_id: {call_index}",
            triggers=triggers or [],
            match=match or {},
            **kwargs,
        )

    def _make_call(self, args=None):
        from src.lnl.types import ToolCall
        return ToolCall(
            id="c1",
            tool="email.send",
            arguments=args or {"to": "alice@company.com", "subject": "Hello"},
        )

    def test_executor_never_fires_inject_event(self):
        """Executor does not call inject_event even when triggers are configured — that is the evaluator's job."""
        from src.data.schema import MockToolTrigger
        from src.lnl.tools import MockInProcessExecutor

        injected = []
        executor = MockInProcessExecutor(self._make_def(triggers=[
            MockToolTrigger(
                target_object_id="slack-monitor",
                message_template="Email sent to {to} — subject: {subject}",
                source="slack",
            ),
        ]))

        executor.execute(
            self._make_call(args={"to": "alice@company.com", "subject": "Q2 report"}),
            {"inject_event": lambda t, m, s: injected.append((t, m, s))},
        )

        assert len(injected) == 0

    def test_executor_returns_response_when_triggers_configured(self):
        """Executor still returns the correct scripted response when triggers are present."""
        from src.data.schema import MockToolTrigger
        from src.lnl.tools import MockInProcessExecutor

        executor = MockInProcessExecutor(self._make_def(triggers=[
            MockToolTrigger(target_object_id="slack-monitor", message_template="Email to {to}", source="slack"),
        ]))

        result = executor.execute(self._make_call(), {})
        assert result.output == "email_id: 1"

    def test_call_log_has_no_triggered_key(self):
        """call_log entries do not contain a 'triggered' key — the evaluator sets that annotation."""
        from src.data.schema import MockToolTrigger
        from src.lnl.tools import MockInProcessExecutor

        executor = MockInProcessExecutor(self._make_def(triggers=[
            MockToolTrigger(target_object_id="slack-monitor", message_template="Email to {to}", source="slack"),
        ]))

        executor.execute(self._make_call(), {"inject_event": lambda *a: None})

        assert "triggered" not in executor.call_log[0]

    def test_trigger_metadata_accessible_on_tool_def(self):
        """MockToolDef.triggers is preserved and available for the evaluator to read."""
        from src.data.schema import MockToolTrigger
        from src.lnl.tools import MockInProcessExecutor

        trigger = MockToolTrigger(
            target_object_id="slack-monitor",
            message_template="Email sent to {to} — subject: {subject}",
            source="slack",
        )
        executor = MockInProcessExecutor(self._make_def(triggers=[trigger]))
        assert executor._tool_def.triggers == [trigger]


# ── MockInProcessExecutor HTTP (remote) mode ─────────────────────────────────

class _FakeResponse:
    """Minimal stand-in for an httpx.Response in unit tests."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class TestMockInProcessExecutorRemoteMode:
    """When remote_url is set, MockInProcessExecutor fetches the response from
    the HTTP mock server (POST /tool/{method}) — the same path the OpenClaw
    baseline uses — while still firing triggers in-process."""

    def _make_def(self, triggers=None, match=None, **kwargs):
        from src.data.schema import MockToolDef
        return MockToolDef(
            tool_name="email.send",
            description="Send an email.",
            arguments_schema={"type": "object", "properties": {"to": {"type": "string"}}},
            response_template="LOCAL fallback {to}",
            triggers=triggers or [],
            match=match or {},
            **kwargs,
        )

    def _make_call(self, args=None):
        from src.lnl.types import ToolCall
        return ToolCall(id="c1", tool="email.send", arguments=args or {"to": "alice@x.com"})

    def test_remote_mode_returns_server_result(self, monkeypatch):
        import httpx
        from src.lnl.tools import MockInProcessExecutor

        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _FakeResponse({"status": "ok", "result": "SERVER says hi"})

        monkeypatch.setattr(httpx, "post", fake_post)

        executor = MockInProcessExecutor(
            self._make_def(), remote_url="http://127.0.0.1:18888", slot_id="tc3-r1",
        )
        result = executor.execute(self._make_call(), {})
        # Response comes from the server, not the local response_template.
        assert result.output == "SERVER says hi"
        # Hits POST /tool/{method} with the slot id + args in the body.
        assert captured["url"] == "http://127.0.0.1:18888/tool/email.send"
        assert captured["json"]["__slot_id__"] == "tc3-r1"
        assert captured["json"]["to"] == "alice@x.com"

    def test_remote_mode_strips_trailing_slash_from_url(self, monkeypatch):
        import httpx
        from src.lnl.tools import MockInProcessExecutor

        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            return _FakeResponse({"result": "ok"})

        monkeypatch.setattr(httpx, "post", fake_post)

        executor = MockInProcessExecutor(
            self._make_def(), remote_url="http://127.0.0.1:18888/",
        )
        executor.execute(self._make_call(), {})
        assert captured["url"] == "http://127.0.0.1:18888/tool/email.send"

    def test_remote_mode_does_not_fire_triggers(self, monkeypatch):
        """In remote mode the executor still does not fire triggers — the evaluator owns that."""
        import httpx
        from src.data.schema import MockToolTrigger
        from src.lnl.tools import MockInProcessExecutor

        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse({"result": "server result"}))

        injected = []
        executor = MockInProcessExecutor(
            self._make_def(triggers=[
                MockToolTrigger(
                    target_object_id="slack-mon",
                    message_template="sent to {to}",
                    source="slack",
                ),
            ]),
            remote_url="http://127.0.0.1:18888",
        )
        executor.execute(
            self._make_call(args={"to": "bob@x.com"}),
            {"inject_event": lambda t, m, s: injected.append((t, m, s))},
        )
        assert injected == []

    def test_remote_mode_populates_call_log(self, monkeypatch):
        import httpx
        from src.lnl.tools import MockInProcessExecutor

        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse({"result": "server result"}))

        executor = MockInProcessExecutor(
            self._make_def(), remote_url="http://127.0.0.1:18888",
        )
        executor.execute(self._make_call(), {})
        assert len(executor.call_log) == 1
        assert executor.call_log[0]["response"] == "server result"

    def test_in_process_mode_unchanged_when_remote_url_none(self):
        from src.lnl.tools import MockInProcessExecutor

        # remote_url=None (default) → response computed locally, no HTTP call.
        executor = MockInProcessExecutor(self._make_def())
        result = executor.execute(self._make_call(args={"to": "carol@x.com"}), {})
        assert result.output == "LOCAL fallback carol@x.com"


@pytest.fixture(scope="module")
def _live_mock_server():
    """A real MockServer for the duration of this module's integration tests."""
    from src.data.mock_server import MockServer
    server = MockServer(openclaw_url="http://localhost:19999", port=18899)
    server.start()
    server.wait_ready(timeout=10.0)
    yield server
    server.stop()


class TestMockInProcessExecutorRemoteIntegration:
    """End-to-end: a real MockServer round-trip catches contract mismatches
    between the executor's HTTP calls and the server's actual endpoints."""

    def test_round_trip_through_real_mock_server(self, _live_mock_server):
        import httpx
        from src.data.mock_server import merge_tc_mock_tools
        from src.data.schema import MockToolDef
        from src.lnl.tools import MockInProcessExecutor
        from src.lnl.types import ToolCall

        tool_def = MockToolDef(
            tool_name="crm.lookup",
            description="Look up a CRM record.",
            arguments_schema={"type": "object", "properties": {"name": {"type": "string"}}},
            response_template="Record for {name}: status=active",
        )
        # Configure a slot on the real server (mirrors evaluate.py's --mock-server path).
        script = merge_tc_mock_tools(None, [tool_def])
        httpx.post(
            "http://127.0.0.1:18899/configure",
            json={
                "slot_id": "itest-1",
                "session_key": "itest-1",
                "mock_script": script.model_dump(),
            },
            timeout=10.0,
        )
        executor = MockInProcessExecutor(
            tool_def, remote_url="http://127.0.0.1:18899", slot_id="itest-1",
        )
        result = executor.execute(
            ToolCall(id="x1", tool="crm.lookup", arguments={"name": "Dana"}), {},
        )
        # The server interpolated the template and returned it over HTTP.
        assert result.output == "Record for Dana: status=active"
