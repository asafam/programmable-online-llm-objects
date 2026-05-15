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
    """Tests for the cross-object event injection mechanism on MockInProcessExecutor.

    When a mock tool is called, MockToolTrigger entries dispatch events to other
    LNL objects via inject_event in the tool context — simulating real-world
    callbacks like "email sent → Slack message arrives in a channel."
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

    def test_trigger_fires_inject_event_with_correct_target_message_and_source(self):
        """When the tool is called, inject_event is called with the declared target, interpolated message, and source."""
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

        assert len(injected) == 1
        target, message, source = injected[0]
        assert target == "slack-monitor"
        assert "alice@company.com" in message
        assert "Q2 report" in message
        assert source == "slack"

    def test_trigger_message_template_interpolates_all_arg_fields(self):
        """All {arg_name} placeholders in message_template are replaced with tool call argument values."""
        from src.data.schema import MockToolTrigger
        from src.lnl.tools import MockInProcessExecutor

        injected = []
        executor = MockInProcessExecutor(self._make_def(triggers=[
            MockToolTrigger(
                target_object_id="notification-handler",
                message_template="New message in #deal-alerts: {subject} from {to}, body: {body}",
                source="slack",
            ),
        ]))

        executor.execute(
            self._make_call(args={"to": "bob@company.com", "subject": "Deal closed", "body": "Acme Corp signed"}),
            {"inject_event": lambda t, m, s: injected.append((t, m, s))},
        )

        _, message, _ = injected[0]
        assert "bob@company.com" in message
        assert "Deal closed" in message
        assert "Acme Corp signed" in message
        assert "#deal-alerts" in message

    def test_trigger_message_template_includes_call_index(self):
        """{call_index} is available in message_template and increments per call."""
        from src.data.schema import MockToolTrigger
        from src.lnl.tools import MockInProcessExecutor

        injected = []

        def record(t, m, s):
            injected.append(m)

        executor = MockInProcessExecutor(self._make_def(triggers=[
            MockToolTrigger(
                target_object_id="audit-log",
                message_template="Email #{call_index} dispatched to {to}",
                source="audit",
            ),
        ]))

        executor.execute(self._make_call(args={"to": "alice@company.com", "subject": "First"}), {"inject_event": record})
        executor.execute(self._make_call(args={"to": "bob@company.com", "subject": "Second"}), {"inject_event": record})

        assert "Email #1 dispatched to alice@company.com" in injected[0]
        assert "Email #2 dispatched to bob@company.com" in injected[1]

    def test_trigger_fires_when_tool_level_match_passes(self):
        """Trigger fires when the tool-level match condition is satisfied."""
        from src.data.schema import MockToolTrigger
        from src.lnl.tools import MockInProcessExecutor

        injected = []
        executor = MockInProcessExecutor(self._make_def(
            match={"to": r"@company\.com$"},
            triggers=[MockToolTrigger(
                target_object_id="internal-notifier",
                message_template="Internal email sent to {to}",
                source="internal",
            )],
        ))

        executor.execute(
            self._make_call(args={"to": "alice@company.com", "subject": "Internal memo"}),
            {"inject_event": lambda t, m, s: injected.append((t, m, s))},
        )

        assert len(injected) == 1
        assert "alice@company.com" in injected[0][1]

    def test_trigger_suppressed_when_tool_level_match_fails(self):
        """Trigger does not fire when the tool-level match condition is not met."""
        from src.data.schema import MockToolTrigger
        from src.lnl.tools import MockInProcessExecutor

        injected = []
        executor = MockInProcessExecutor(self._make_def(
            match={"to": r"@company\.com$"},
            triggers=[MockToolTrigger(
                target_object_id="internal-notifier",
                message_template="Internal email sent to {to}",
                source="internal",
            )],
        ))

        # External address — match fails, trigger must not fire
        executor.execute(
            self._make_call(args={"to": "vendor@external.com", "subject": "Order confirm"}),
            {"inject_event": lambda t, m, s: injected.append((t, m, s))},
        )

        assert len(injected) == 0

    def test_multiple_triggers_all_fire_on_single_call(self):
        """All MockToolTrigger entries fire when a single tool call occurs."""
        from src.data.schema import MockToolTrigger
        from src.lnl.tools import MockInProcessExecutor

        injected = []
        executor = MockInProcessExecutor(self._make_def(triggers=[
            MockToolTrigger(target_object_id="slack-monitor",  message_template="Email to {to}", source="slack"),
            MockToolTrigger(target_object_id="crm-updater",    message_template="Contact {to} emailed", source="crm"),
            MockToolTrigger(target_object_id="audit-log",      message_template="Outbound: {to}", source="audit"),
        ]))

        executor.execute(self._make_call(), {"inject_event": lambda t, m, s: injected.append((t, m, s))})

        targets = [t for t, _, _ in injected]
        assert len(injected) == 3
        assert "slack-monitor" in targets
        assert "crm-updater" in targets
        assert "audit-log" in targets

    def test_trigger_graceful_when_inject_event_absent_from_context(self):
        """No exception is raised when inject_event is not provided in the tool context."""
        from src.data.schema import MockToolTrigger
        from src.lnl.tools import MockInProcessExecutor

        executor = MockInProcessExecutor(self._make_def(triggers=[
            MockToolTrigger(target_object_id="slack-monitor", message_template="Email to {to}", source="slack"),
        ]))

        # Must not raise; tool still returns a response
        result = executor.execute(self._make_call(), {})
        assert result.output is not None

    def test_trigger_dispatch_recorded_in_call_log(self):
        """Each trigger dispatch is captured in the executor's call_log for traceability."""
        from src.data.schema import MockToolTrigger
        from src.lnl.tools import MockInProcessExecutor

        executor = MockInProcessExecutor(self._make_def(triggers=[
            MockToolTrigger(target_object_id="slack-monitor", message_template="Email to {to}", source="slack"),
        ]))

        executor.execute(self._make_call(), {"inject_event": lambda *a: None})

        assert len(executor.call_log) == 1
        log = executor.call_log[0]
        assert "triggered" in log
        assert log["triggered"][0]["target"] == "slack-monitor"
        assert "alice@company.com" in log["triggered"][0]["message"]

    def test_no_trigger_log_entry_when_match_fails(self):
        """call_log has no 'triggered' key when the tool-level match suppresses the trigger."""
        from src.data.schema import MockToolTrigger
        from src.lnl.tools import MockInProcessExecutor

        executor = MockInProcessExecutor(self._make_def(
            match={"to": r"@company\.com$"},
            triggers=[MockToolTrigger(target_object_id="notifier", message_template="Hi {to}", source="slack")],
        ))

        executor.execute(
            self._make_call(args={"to": "external@other.com", "subject": "Hi"}),
            {"inject_event": lambda *a: None},
        )

        assert "triggered" not in executor.call_log[0]


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

    def test_remote_mode_still_fires_triggers_in_process(self, monkeypatch):
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
        # Triggers fire in-process even though the response arrived over HTTP.
        assert injected == [("slack-mon", "sent to bob@x.com", "slack")]

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
