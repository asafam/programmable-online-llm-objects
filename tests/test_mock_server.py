"""Unit tests for MockServer — no OpenClaw dependency required."""
from __future__ import annotations

import time

import httpx
import pytest

from src.data.mock_server import MockServer, resolve_mock_configs, resolve_orchestration, _trigger_matches
from src.data.schema import (
    MockCallback,
    MockImmediateResponse,
    MockMethodDef,
    MockScript,
    MockSystemDef,
    ObjectDef,
    Event,
    EventExpect,
    EventTrigger,
    OrchestratorReaction,
    OrchestratorScript,
    OrchestratorTrigger,
    Sample,
    Step,
    Modification,
    ModType,
    Ambiguity,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

SLACK_SCRIPT = MockScript(
    systems=[
        MockSystemDef(
            system="slack",
            tools=[
                MockMethodDef(
                    method="slack_send_message",
                    immediate=MockImmediateResponse(
                        template="message_id: {tool_call_id}, delivered to #{channel}"
                    ),
                    callback=MockCallback(
                        delay_seconds=0.1,
                        message_template="[Slack] Delivered to #{channel}: {message}",
                        source="slack",
                    ),
                ),
                MockMethodDef(
                    method="slack_list_channels",
                    immediate=MockImmediateResponse(template='["#general", "#deal-desk"]'),
                ),
            ],
        )
    ]
)


@pytest.fixture(scope="module")
def mock_server():
    """Start a MockServer for the duration of this module's tests."""
    server = MockServer(mock_script=SLACK_SCRIPT, openclaw_url="http://localhost:19999", port=18898)
    server.start()
    server.wait_ready(timeout=10.0)
    server.configure("test-session-1")
    yield server
    server.stop()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestMockServerHealth:
    def test_health_endpoint(self, mock_server):
        resp = httpx.get("http://127.0.0.1:18898/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestToolDispatch:
    def test_slack_send_message_returns_result(self, mock_server):
        resp = httpx.post(
            "http://127.0.0.1:18898/tool/slack_send_message",
            json={"channel": "deal-desk", "message": "Hello", "__session_key__": "test-session-1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "delivered to #deal-desk" in data["result"]

    def test_slack_list_channels_returns_result(self, mock_server):
        resp = httpx.post(
            "http://127.0.0.1:18898/tool/slack_list_channels",
            json={"__session_key__": "test-session-1"},
        )
        assert resp.status_code == 200
        assert "#general" in resp.json()["result"]

    def test_unknown_method_returns_fallback(self, mock_server):
        resp = httpx.post(
            "http://127.0.0.1:18898/tool/unknown_method",
            json={"__session_key__": "test-session-1"},
        )
        assert resp.status_code == 200
        assert "no script configured" in resp.json()["result"]


class TestCallLog:
    def test_log_records_tool_calls(self, mock_server):
        mock_server.configure("test-session-log")
        httpx.post(
            "http://127.0.0.1:18898/tool/slack_send_message",
            json={"channel": "test", "message": "hi", "__session_key__": "test-session-log"},
        )
        log = mock_server.get_log()
        assert any(entry["method"] == "slack_send_message" for entry in log)

    def test_configure_clears_log(self, mock_server):
        mock_server.configure("test-session-clear")
        log = mock_server.get_log()
        assert log == []

    def test_callback_appears_in_log_after_delay(self, mock_server):
        mock_server.configure("test-session-cb")
        httpx.post(
            "http://127.0.0.1:18898/tool/slack_send_message",
            json={"channel": "approvals", "message": "Approved?", "__session_key__": "test-session-cb"},
        )
        # Wait for callback (delay_seconds=0.1 in fixture)
        time.sleep(0.5)
        log = mock_server.get_log()
        callback_entries = [
            e for e in log
            if e.get("is_callback") and e.get("session_key") == "test-session-cb"
        ]
        assert len(callback_entries) >= 1
        assert "approvals" in callback_entries[0]["result"]


class TestResolveMockConfigs:
    def _make_tc(self, skills=None, event_sources=None) -> Sample:
        obj = ObjectDef(
            object_id="test-obj",
            role="test",
            behavior="",
            skills=skills or [],
            event_sources=event_sources or [],
        )
        return Sample(
            id="TC001",
            name="Test",
            domain="test",
            source_type="test",
            link="",
            objects=[obj],
            steps=[Step(text="do thing", target="test-obj")],
            modifications=[],
            events=[],
        )

    def test_resolves_slack_from_skills(self):
        tc = self._make_tc(skills=["Send Slack notifications to #deal-desk"])
        script = resolve_mock_configs(tc)
        assert script is not None
        systems = [s.system for s in script.systems]
        assert "slack" in systems

    def test_resolves_email_from_event_sources(self):
        tc = self._make_tc(event_sources=["Gmail webhook: new email received"])
        script = resolve_mock_configs(tc)
        assert script is not None
        systems = [s.system for s in script.systems]
        assert "email" in systems

    def test_resolves_multiple_systems(self):
        tc = self._make_tc(
            skills=["Send Slack messages", "Create Jira tickets"]
        )
        script = resolve_mock_configs(tc)
        assert script is not None
        systems = [s.system for s in script.systems]
        assert "slack" in systems
        assert "jira" in systems

    def test_returns_none_when_no_match(self):
        tc = self._make_tc(skills=["Analyze data", "Generate reports"])
        script = resolve_mock_configs(tc)
        assert script is None


class TestMockScriptGetMethod:
    def test_get_method_found(self):
        method_def = SLACK_SCRIPT.get_method("slack_send_message")
        assert method_def is not None
        assert method_def.method == "slack_send_message"

    def test_get_method_not_found(self):
        method_def = SLACK_SCRIPT.get_method("nonexistent_method")
        assert method_def is None


# ── Orchestration tests ───────────────────────────────────────────────────────

ORCH_SCRIPT = OrchestratorScript(
    name="test-orchestration",
    time_scale=1.0,
    triggers=[
        OrchestratorTrigger(
            tool="email_send",
            match={"subject": ".*[Aa]pproval.*"},
            reactions=[
                OrchestratorReaction(
                    after_seconds=0.1,
                    source="slack",
                    message="[Slack] @manager: Approved! Re: {subject}",
                )
            ],
        ),
        OrchestratorTrigger(
            tool="email_send",
            match={},  # match all emails
            reactions=[
                OrchestratorReaction(
                    after_seconds=0.05,
                    source="email",
                    message="[Email] Delivery confirmed to {to}",
                )
            ],
        ),
    ],
)


class TestResolveOrchestration:
    def _make_tc_with_events(self, events: list[Event]) -> Sample:
        obj = ObjectDef(
            object_id="test-obj",
            role="test",
            state_description="",
            behavior="",
            skills=["Send email notifications"],
        )
        return Sample(
            id="TC-ORCH",
            name="Orchestration Test",
            domain="test",
            source_type="test",
            link="",
            objects=[obj],
            steps=[Step(text="do thing", target="test-obj")],
            modifications=[],
            events=events,
        )

    def test_returns_none_when_no_triggered_by(self):
        tc = self._make_tc_with_events([
            Event(
                id="E001",
                call_type="send_event",
                source="slack",
                recipient="test-obj",
                input="Hello",
                when="W01-1T09:00",
                expect=EventExpect(action="something", reason="because"),
                triggered_by=None,
            )
        ])
        assert resolve_orchestration(tc) is None

    def test_builds_trigger_from_triggered_by(self):
        tc = self._make_tc_with_events([
            Event(
                id="E001",
                call_type="send_event",
                source="slack",
                recipient="quote-approvals",
                input="@manager: Approved!",
                when="W01-2T10:00",
                expect=EventExpect(action="mark quote approved", reason="manager approved"),
                triggered_by=EventTrigger(
                    tool="email_send",
                    match={"subject": ".*[Aa]pproval.*"},
                    after_minutes=2,
                ),
            )
        ])
        script = resolve_orchestration(tc)
        assert script is not None
        assert len(script.triggers) == 1
        trigger = script.triggers[0]
        assert trigger.tool == "email_send"
        assert trigger.match == {"subject": ".*[Aa]pproval.*"}
        assert len(trigger.reactions) == 1
        reaction = trigger.reactions[0]
        assert reaction.source == "slack"
        assert "Approved!" in reaction.message
        assert reaction.after_minutes == 2.0

    def test_merges_reactions_for_same_trigger(self):
        """Two events with the same tool+match should become one trigger with two reactions."""
        tc = self._make_tc_with_events([
            Event(
                id="E001", call_type="send_event", source="slack", recipient="obj",
                input="Approved!", when="W01-2T10:00",
                expect=EventExpect(action="a", reason="b"),
                triggered_by=EventTrigger(tool="email_send", match={}, after_minutes=2),
            ),
            Event(
                id="E002", call_type="send_event", source="email", recipient="obj",
                input="Delivery confirmed", when="W01-2T10:01",
                expect=EventExpect(action="c", reason="d"),
                triggered_by=EventTrigger(tool="email_send", match={}, after_seconds=30),
            ),
        ])
        script = resolve_orchestration(tc)
        assert script is not None
        assert len(script.triggers) == 1
        assert len(script.triggers[0].reactions) == 2

    def test_creates_separate_triggers_for_different_tools(self):
        tc = self._make_tc_with_events([
            Event(
                id="E001", call_type="send_event", source="slack", recipient="obj",
                input="Slack reply", when="W01-2T10:00",
                expect=EventExpect(action="a", reason="b"),
                triggered_by=EventTrigger(tool="email_send", match={}, after_minutes=1),
            ),
            Event(
                id="E002", call_type="send_event", source="jira", recipient="obj",
                input="Ticket assigned", when="W01-2T10:05",
                expect=EventExpect(action="c", reason="d"),
                triggered_by=EventTrigger(tool="jira_create_issue", match={}, after_minutes=1),
            ),
        ])
        script = resolve_orchestration(tc)
        assert script is not None
        assert len(script.triggers) == 2
        tools = {t.tool for t in script.triggers}
        assert tools == {"email_send", "jira_create_issue"}

    def test_time_scale_applied(self):
        tc = self._make_tc_with_events([
            Event(
                id="E001", call_type="send_event", source="slack", recipient="obj",
                input="Hi", when="W01-1T09:00",
                expect=EventExpect(action="a", reason="b"),
                triggered_by=EventTrigger(tool="email_send", match={}, after_minutes=5),
            )
        ])
        script = resolve_orchestration(tc, time_scale=0.01)
        assert script is not None
        assert script.time_scale == 0.01


class TestTriggerMatching:
    def test_matches_tool_and_pattern(self):
        trigger = ORCH_SCRIPT.triggers[0]
        assert _trigger_matches(trigger, "email_send", {"subject": "Approval needed"})

    def test_no_match_wrong_method(self):
        trigger = ORCH_SCRIPT.triggers[0]
        assert not _trigger_matches(trigger, "slack_send_message", {"subject": "Approval needed"})

    def test_no_match_pattern_mismatch(self):
        trigger = ORCH_SCRIPT.triggers[0]
        assert not _trigger_matches(trigger, "email_send", {"subject": "Monthly report"})

    def test_match_empty_pattern_matches_all(self):
        trigger = ORCH_SCRIPT.triggers[1]  # match: {}
        assert _trigger_matches(trigger, "email_send", {"subject": "Anything"})
        assert _trigger_matches(trigger, "email_send", {})


class TestOrchestrationIntegration:
    @pytest.fixture(scope="class")
    def orch_server(self):
        server = MockServer(
            mock_script=SLACK_SCRIPT,
            openclaw_url="http://localhost:19999",
            port=18899,
        )
        server.add_orchestration(ORCH_SCRIPT)
        server.start()
        server.wait_ready(timeout=10.0)
        server.configure("orch-session-1")
        yield server
        server.stop()

    def test_orchestration_reaction_fires_after_delay(self, orch_server):
        orch_server.configure("orch-test-1")
        httpx.post(
            "http://127.0.0.1:18899/tool/email_send",
            json={
                "to": "manager@example.com",
                "subject": "Approval needed for Q-123",
                "body": "Please approve",
                "__session_key__": "orch-test-1",
            },
        )
        time.sleep(0.5)  # wait for reactions (after_seconds=0.1 and 0.05)
        log = orch_server.get_log()
        orch_entries = [e for e in log if e.get("is_orchestration") and e.get("session_key") == "orch-test-1"]
        # Should have two reactions: slack approval + email delivery confirmation
        assert len(orch_entries) == 2
        sources = {e["method"].split(":")[0] for e in orch_entries}
        assert "slack" in sources
        assert "email" in sources

    def test_orchestration_reaction_content_interpolated(self, orch_server):
        orch_server.configure("orch-test-2")
        httpx.post(
            "http://127.0.0.1:18899/tool/email_send",
            json={
                "to": "alice@example.com",
                "subject": "Approval for deal X",
                "body": "Please review",
                "__session_key__": "orch-test-2",
            },
        )
        time.sleep(0.5)
        log = orch_server.get_log()
        slack_reaction = next(
            (e for e in log if e.get("is_orchestration") and "slack" in e["method"]),
            None,
        )
        assert slack_reaction is not None
        assert "Approval for deal X" in slack_reaction["result"]

    def test_non_matching_tool_fires_no_orchestration(self, orch_server):
        orch_server.configure("orch-test-3")
        httpx.post(
            "http://127.0.0.1:18899/tool/slack_send_message",
            json={"channel": "general", "message": "hello", "__session_key__": "orch-test-3"},
        )
        time.sleep(0.3)
        log = orch_server.get_log()
        orch_entries = [
            e for e in log
            if e.get("is_orchestration") and e.get("session_key") == "orch-test-3"
        ]
        assert len(orch_entries) == 0

    def test_load_orchestration_from_file(self, orch_server):
        from pathlib import Path
        path = Path("config/mocks/orchestration/quote-approval.yaml")
        if path.exists():
            orch_server.load_orchestration(path)
            assert orch_server._state.orchestration_script is not None
            assert orch_server._state.orchestration_script.name == "quote-approval-workflow"
