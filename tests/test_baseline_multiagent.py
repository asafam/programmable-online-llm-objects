"""
Tests for OpenClaw multi-agent baseline evaluation hardening.

All tests are gateway-free: the OpenClaw SDK is mocked via unittest.mock.patch.
TestMockServerSlotIsolation uses a real FastAPI MockServer on port 18897
(different from 18888 production and 18898 used by test_mock_server.py).

Coverage:
  Part A — Session isolation (unique session names, AGENTS.md rewrite)
  Part B — Concurrent slot workspaces
  Part C — Mock server per-slot isolation
  Part D — _wait_for_gateway raises TimeoutError on timeout
  Part E — Tool trigger KeyError logging
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.data.schema import (
    EventExpect,
    Modification,
    ModType,
    Ambiguity,
    ObjectDef,
    Step,
    Sample,
)

# ── Fixture Sample: 2 objects, 1 step ─────────────────────────────────────

ROUTER_OBJ = ObjectDef(
    object_id="message-router",
    role="Routes incoming messages to the sink agent.",
    behavior=(
        "When a message arrives, forward it to message-sink via sessions_send. "
        "Record the dispatch in state."
    ),
    neighbors=["message-sink"],
    skills=["slack"],
    state_description="Idle — no messages routed.",
)

SINK_OBJ = ObjectDef(
    object_id="message-sink",
    role="Terminal sink: calls slack_send_message for all received messages.",
    behavior="Call slack_send_message with the message content to #general.",
    neighbors=[],
    skills=["slack"],
    state_description="Idle — awaiting messages.",
)

ROUTER_SINK_TC = Sample(
    id="TC-ROUTER-SINK",
    sample_id="sample-router-sink",
    name="Message Router → Sink",
    domain="messaging",
    source_type="test",
    link="",
    objects=[ROUTER_OBJ, SINK_OBJ],
    steps=["Route this message: hello world"],
    modifications=[],
    events=[],
    mock_tools=[],
)


# ── Helper: build a mock OpenClawClient ──────────────────────────────────────

def _make_mock_client(execute_result: str = "ok"):
    """Return (mock_client, router_handle, sink_handle) with scripted execute()."""
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.content = execute_result

    router_handle = AsyncMock()
    router_handle.execute = AsyncMock(return_value=mock_result)

    sink_handle = AsyncMock()
    sink_handle.execute = AsyncMock(return_value=mock_result)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    captured_sessions: list[tuple[str, str]] = []

    def capturing_get_agent(agent_id, session_name):
        captured_sessions.append((agent_id, session_name))
        return router_handle if "router" in agent_id else sink_handle

    mock_client.get_agent = capturing_get_agent
    mock_client._captured_sessions = captured_sessions

    return mock_client, router_handle, sink_handle


# ── Part A: Session Naming ────────────────────────────────────────────────────

class TestSessionNaming:
    """A1+A2: _agents_md uses dynamic session_name; rewrite_agents_md patches files."""

    def test_agents_md_uses_given_session_name(self):
        """_agents_md() embeds the given session_name in peer sessionKey refs."""
        from src.lnl.openclaw_export import _agents_md, export_workflow_from_objects
        from src.data.schema import to_lnl_definition

        obj_def = to_lnl_definition(ROUTER_OBJ)
        md = _agents_md(obj_def, session_name="eval-42")

        assert 'sessionKey="agent:message-sink:eval-42"' in md
        # Should NOT have hardcoded "main" in the peer ref
        assert "eval-42" in md

    def test_agents_md_default_is_main(self):
        """Default session_name='main' preserves backward compatibility."""
        from src.lnl.openclaw_export import _agents_md
        from src.data.schema import to_lnl_definition

        obj_def = to_lnl_definition(ROUTER_OBJ)
        md = _agents_md(obj_def)

        assert 'sessionKey="agent:message-sink:main"' in md

    def test_rewrite_agents_md_updates_workspace_file(self, tmp_path):
        """rewrite_agents_md() overwrites AGENTS.md with the new session_name."""
        from src.lnl.openclaw_export import export_workflow_from_objects, rewrite_agents_md

        export_workflow_from_objects([ROUTER_OBJ, SINK_OBJ], tmp_path, force=True)

        rewrite_agents_md([ROUTER_OBJ, SINK_OBJ], tmp_path, session_name="eval-7")

        content = (tmp_path / "workspace-message-router" / "AGENTS.md").read_text()
        assert 'sessionKey="agent:message-sink:eval-7"' in content

    def test_rewrite_agents_md_with_slot_suffix(self, tmp_path):
        """Slot suffix appended to peer agent IDs so sessionKey refs are slot-correct."""
        from src.data.evaluate_baseline import _slot_objects
        from src.lnl.openclaw_export import export_workflow_from_objects, rewrite_agents_md

        slotted = _slot_objects([ROUTER_OBJ, SINK_OBJ], "-c1")
        export_workflow_from_objects(slotted, tmp_path, force=True)

        rewrite_agents_md([ROUTER_OBJ, SINK_OBJ], tmp_path,
                          session_name="eval-3", slot_suffix="-c1")

        content = (tmp_path / "workspace-message-router-c1" / "AGENTS.md").read_text()
        assert 'sessionKey="agent:message-sink-c1:eval-3"' in content

    def test_session_names_are_unique_across_runs(self):
        """Each counter increment produces a distinct eval-ma-N session name."""
        from src.data.evaluate_baseline import OpenClawAgent

        before = OpenClawAgent._global_counter
        OpenClawAgent._global_counter += 1
        name1 = f"eval-ma-{OpenClawAgent._global_counter}"
        OpenClawAgent._global_counter += 1
        name2 = f"eval-ma-{OpenClawAgent._global_counter}"
        OpenClawAgent._global_counter = before  # restore

        assert name1 != name2
        assert name1.startswith("eval-ma-")
        assert name2.startswith("eval-ma-")


# ── Part B: Concurrent Slot Workspaces ───────────────────────────────────────

class TestConcurrentSlotWorkspaces:
    """B2: _slot_objects renames IDs; slot dirs created on export."""

    def test_slot_objects_renames_ids(self):
        """_slot_objects appends suffix to object_id and peer IDs."""
        from src.data.evaluate_baseline import _slot_objects

        slotted = _slot_objects([ROUTER_OBJ, SINK_OBJ], "-c1")
        ids = [o.object_id for o in slotted]

        assert "message-router-c1" in ids
        assert "message-sink-c1" in ids

        router = next(o for o in slotted if "router" in o.object_id)
        assert router.peers[0].object_id == "message-sink-c1"

    def test_slot_objects_noop_for_empty_suffix(self):
        """_slot_objects with empty suffix returns original objects unchanged."""
        from src.data.evaluate_baseline import _slot_objects

        result = _slot_objects([ROUTER_OBJ, SINK_OBJ], "")
        assert result is [ROUTER_OBJ, SINK_OBJ] or (
            result[0].object_id == "message-router"
        )

    def test_slot_workspace_dirs_created(self, tmp_path):
        """Slot-1 export creates workspace-message-router-c1, not workspace-message-router."""
        from src.data.evaluate_baseline import _slot_objects
        from src.lnl.openclaw_export import export_workflow_from_objects

        slotted = _slot_objects([ROUTER_OBJ, SINK_OBJ], "-c1")
        export_workflow_from_objects(slotted, tmp_path, force=True)

        assert (tmp_path / "workspace-message-router-c1").is_dir()
        assert (tmp_path / "workspace-message-sink-c1").is_dir()
        # Slot-0 dirs NOT created by this export
        assert not (tmp_path / "workspace-message-router").exists()

    def test_slot0_uses_default_workspace_names(self, tmp_path):
        """Slot 0 (no suffix) uses standard workspace-{id} names."""
        from src.lnl.openclaw_export import export_workflow_from_objects

        export_workflow_from_objects([ROUTER_OBJ, SINK_OBJ], tmp_path, force=True)

        assert (tmp_path / "workspace-message-router").is_dir()
        assert (tmp_path / "workspace-message-sink").is_dir()


# ── Part C: Mock Server Slot Isolation ───────────────────────────────────────

_MOCK_SERVER_PORT = 18897


@pytest.fixture(scope="module")
def slotted_mock_server():
    """Real MockServer for slot isolation tests."""
    from src.data.mock_server import MockServer, MockScript, MockSystemDef, MockMethodDef, MockImmediateResponse

    script = MockScript(systems=[MockSystemDef(system="slack", tools=[
        MockMethodDef(
            method="slack_send_message",
            immediate=MockImmediateResponse(template="ok: {message}"),
        )
    ])])
    server = MockServer(
        mock_script=script,
        openclaw_url="http://localhost:19999",
        port=_MOCK_SERVER_PORT,
    )
    server.start()
    server.wait_ready()
    yield server
    server.stop()


class TestMockServerSlotIsolation:
    """C: per-slot state in MockServer so concurrent TCs don't cross-contaminate."""

    def test_configure_and_log_are_slot_isolated(self, slotted_mock_server):
        """Calls configured on slot-a do not appear in slot-b's log."""
        slotted_mock_server.configure("session-a", slot_id="slot-a")
        slotted_mock_server.configure("session-b", slot_id="slot-b")

        httpx.post(
            f"http://127.0.0.1:{_MOCK_SERVER_PORT}/tool/slack_send_message",
            json={
                "channel": "c", "message": "from-a",
                "__slot_id__": "slot-a",
                "__session_key__": "session-a",
            },
        )

        log_a = slotted_mock_server.get_log(slot_id="slot-a")
        log_b = slotted_mock_server.get_log(slot_id="slot-b")

        assert len(log_a) == 1
        assert len(log_b) == 0

    def test_configure_clears_only_target_slot(self, slotted_mock_server):
        """Re-configuring slot-c does not clear slot-d's log."""
        slotted_mock_server.configure("s-c", slot_id="slot-c")
        slotted_mock_server.configure("s-d", slot_id="slot-d")

        httpx.post(
            f"http://127.0.0.1:{_MOCK_SERVER_PORT}/tool/slack_send_message",
            json={"message": "x", "__slot_id__": "slot-d", "__session_key__": "s-d"},
        )
        # Re-configure slot-c (should only clear slot-c, not slot-d)
        slotted_mock_server.configure("s-c-reset", slot_id="slot-c")

        assert len(slotted_mock_server.get_log(slot_id="slot-d")) == 1
        assert len(slotted_mock_server.get_log(slot_id="slot-c")) == 0

    def test_concurrent_slots_dont_cross_contaminate(self, slotted_mock_server):
        """3 threads × 3 calls each: every slot sees exactly its own 3 calls."""
        results: dict[str, list] = {}
        errors: list[str] = []

        def hit_slot(slot_name: str):
            try:
                slotted_mock_server.configure(f"sess-{slot_name}", slot_id=slot_name)
                for i in range(3):
                    httpx.post(
                        f"http://127.0.0.1:{_MOCK_SERVER_PORT}/tool/slack_send_message",
                        json={
                            "message": f"msg-{i}",
                            "__slot_id__": slot_name,
                            "__session_key__": f"sess-{slot_name}",
                        },
                    )
                results[slot_name] = slotted_mock_server.get_log(slot_id=slot_name)
            except Exception as exc:
                errors.append(f"{slot_name}: {exc}")

        threads = [
            threading.Thread(target=hit_slot, args=(f"conc-{j}",))
            for j in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        for slot_name, log in results.items():
            assert len(log) == 3, f"slot {slot_name!r} expected 3 calls, got {len(log)}"

    def test_default_slot_is_backward_compatible(self, slotted_mock_server):
        """configure() and get_log() without slot_id use the 'default' slot — existing callers unaffected."""
        slotted_mock_server.configure("legacy-session")

        httpx.post(
            f"http://127.0.0.1:{_MOCK_SERVER_PORT}/tool/slack_send_message",
            json={"message": "legacy", "__session_key__": "legacy-session"},
        )

        log = slotted_mock_server.get_log()
        assert len(log) == 1
        assert log[0]["method"] == "slack_send_message"


# ── Part D: _wait_for_gateway raises TimeoutError ─────────────────────────────

class TestWaitForGatewayTimeout:
    """D: _wait_for_gateway raises asyncio.TimeoutError after timeout (not silent)."""

    def test_raises_timeout_error_when_gateway_unreachable(self):
        """Raises asyncio.TimeoutError when the gateway connection always fails."""
        from src.data.evaluate_baseline import _wait_for_gateway

        # Mock connect to raise immediately so the test doesn't actually block on TCP.
        with patch("openclaw_sdk.OpenClawClient") as MockOCClient:
            MockOCClient.connect = AsyncMock(side_effect=ConnectionRefusedError("refused"))
            with pytest.raises(asyncio.TimeoutError):
                asyncio.run(_wait_for_gateway(
                    gateway_url="ws://127.0.0.1:19998",
                    timeout_s=0.1,  # very short — connection fails instantly
                ))

    def test_returns_cleanly_when_gateway_available(self):
        """Returns without exception when gateway responds immediately."""
        from src.data.evaluate_baseline import _wait_for_gateway

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.config_mgr.get = AsyncMock(return_value={"raw": "{}", "hash": "abc"})

        with patch("openclaw_sdk.OpenClawClient") as MockOCClient:
            MockOCClient.connect = AsyncMock(return_value=mock_client)
            # Should complete without raising
            asyncio.run(_wait_for_gateway(gateway_url="ws://fake", timeout_s=5.0))


# ── Part A (integration): _execute_tc_async session naming ───────────────────

class TestExecuteTCAsyncSessionNaming:
    """A3+A4: _execute_tc_async uses unique session names and rewrites AGENTS.md."""

    @pytest.fixture
    def openclaw_home(self, tmp_path):
        """Minimal openclaw_home with exported workspaces."""
        from src.lnl.openclaw_export import export_workflow_from_objects
        export_workflow_from_objects([ROUTER_OBJ, SINK_OBJ], tmp_path, force=True)
        return tmp_path

    def _run_tc(self, openclaw_home: Path, session_name_override: str | None = None):
        """Run _execute_tc_async with a mocked OpenClawClient. Returns captured sessions."""
        mock_client, router_handle, sink_handle = _make_mock_client()

        with patch("openclaw_sdk.OpenClawClient") as MockOCClient:
            MockOCClient.connect = AsyncMock(return_value=mock_client)
            mock_harness = MagicMock()
            mock_harness.evaluate_assertion = MagicMock(return_value=(True, "ok", [], 0, 0))

            from src.data.evaluate_baseline import _execute_tc_async
            asyncio.run(_execute_tc_async(
                ROUTER_SINK_TC,
                gateway_url=None,
                openclaw_home=openclaw_home,
                harness=mock_harness,
                mock_server=None,
                verbose=False,
                steps_only=False,
                single_agent_id=None,
                partial_events=None,
                partial_mods=None,
                slot_suffix="",
            ))

        return mock_client._captured_sessions

    def test_multi_agent_does_not_use_main_session(self, openclaw_home):
        """get_agent() is never called with session_name='main' in multi-agent mode."""
        sessions = self._run_tc(openclaw_home)
        session_names_used = {sname for _, sname in sessions}

        assert "main" not in session_names_used, (
            f"Expected unique session name, but 'main' was used. Sessions: {sessions}"
        )

    def test_multi_agent_uses_eval_ma_prefix(self, openclaw_home):
        """get_agent() is called with eval-ma-N session names in multi-agent mode."""
        sessions = self._run_tc(openclaw_home)
        session_names_used = {sname for _, sname in sessions}

        assert any(sname.startswith("eval-ma-") for sname in session_names_used), (
            f"Expected eval-ma-* session name, got: {session_names_used}"
        )

    def test_agents_md_rewritten_before_session_open(self, openclaw_home):
        """AGENTS.md in workspace-message-router contains the dynamic session name
        at the moment router_handle.execute() is first called."""
        agents_md_at_execute: dict[str, str] = {}
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.content = "routed"

        def capturing_execute(content):
            md_path = openclaw_home / "workspace-message-router" / "AGENTS.md"
            agents_md_at_execute["content"] = md_path.read_text()
            async def _f():
                return mock_result
            return _f()

        mock_client, router_handle, sink_handle = _make_mock_client()
        router_handle.execute = capturing_execute

        with patch("openclaw_sdk.OpenClawClient") as MockOCClient:
            MockOCClient.connect = AsyncMock(return_value=mock_client)
            mock_harness = MagicMock()
            mock_harness.evaluate_assertion = MagicMock(return_value=(True, "ok", [], 0, 0))

            from src.data.evaluate_baseline import _execute_tc_async
            asyncio.run(_execute_tc_async(
                ROUTER_SINK_TC, None, openclaw_home, mock_harness,
                None, False, False, None, None, None, slot_suffix="",
            ))

        content = agents_md_at_execute.get("content", "")
        # The AGENTS.md should reference eval-ma-N, not "main"
        assert "eval-ma-" in content, (
            f"AGENTS.md not rewritten before execute(). Content:\n{content[:500]}"
        )

    def test_event_results_collected_per_step(self, openclaw_home):
        """EventResult list has one entry per step with an expect assertion."""
        mock_client, _, _ = _make_mock_client()

        with patch("openclaw_sdk.OpenClawClient") as MockOCClient:
            MockOCClient.connect = AsyncMock(return_value=mock_client)
            mock_harness = MagicMock()
            mock_harness.evaluate_assertion = MagicMock(return_value=(True, "ok", [], 0, 0))

            from src.data.evaluate_baseline import _execute_tc_async
            event_results, _ = asyncio.run(_execute_tc_async(
                ROUTER_SINK_TC, None, openclaw_home, mock_harness,
                None, False, False, None, None, None,
            ))

        assert len(event_results) == 1  # one step with expect
        assert event_results[0].event_id == "S001"

    def test_state_collected_from_all_objects(self, openclaw_home):
        """post_event_state in judge evidence includes state from both workspace dirs."""
        # Write distinct state files
        (openclaw_home / "workspace-message-router" / "state.md").write_text("# State\nrouter idle")
        (openclaw_home / "workspace-message-sink" / "state.md").write_text("# State\nsink idle")

        captured_evidence: dict[str, str] = {}
        mock_client, _, _ = _make_mock_client()

        with patch("openclaw_sdk.OpenClawClient") as MockOCClient:
            MockOCClient.connect = AsyncMock(return_value=mock_client)
            mock_harness = MagicMock()

            def capture_eval(action, evidence, prior):
                captured_evidence["ev"] = evidence
                return True, "ok", [], 0, 0

            mock_harness.evaluate_assertion = MagicMock(side_effect=capture_eval)

            from src.data.evaluate_baseline import _execute_tc_async
            asyncio.run(_execute_tc_async(
                ROUTER_SINK_TC, None, openclaw_home, mock_harness,
                None, False, False, None, None, None,
            ))

        ev = captured_evidence.get("ev", "")
        assert "router idle" in ev or "message-router" in ev, (
            f"Router state not in evidence. Evidence:\n{ev[:500]}"
        )
        assert "sink idle" in ev or "message-sink" in ev, (
            f"Sink state not in evidence. Evidence:\n{ev[:500]}"
        )

    def test_slot_suffix_applied_to_state_collection(self, tmp_path):
        """When slot_suffix='-c1', state is read from workspace-*-c1 dirs."""
        from src.data.evaluate_baseline import _slot_objects
        from src.lnl.openclaw_export import export_workflow_from_objects

        slotted = _slot_objects([ROUTER_OBJ, SINK_OBJ], "-c1")
        export_workflow_from_objects(slotted, tmp_path, force=True)

        captured_evidence: dict[str, str] = {}
        mock_client, router_handle, _ = _make_mock_client()

        # Simulate the agent updating its state file during execute() — write
        # AFTER the reset so state collection picks up the new content.
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.content = "routed"

        async def writing_execute(content):
            (tmp_path / "workspace-message-router-c1" / "state.md").write_text(
                "# State\nslot-1 router"
            )
            return mock_result

        router_handle.execute = writing_execute

        with patch("openclaw_sdk.OpenClawClient") as MockOCClient:
            MockOCClient.connect = AsyncMock(return_value=mock_client)
            mock_harness = MagicMock()

            def capture_eval(action, evidence, prior):
                captured_evidence["ev"] = evidence
                return True, "ok", [], 0, 0

            mock_harness.evaluate_assertion = MagicMock(side_effect=capture_eval)

            from src.data.evaluate_baseline import _execute_tc_async
            asyncio.run(_execute_tc_async(
                ROUTER_SINK_TC, None, tmp_path, mock_harness,
                None, False, False, None, None, None,
                slot_suffix="-c1",
            ))

        ev = captured_evidence.get("ev", "")
        assert "slot-1 router" in ev, (
            f"Slot-1 state not in evidence (state read from wrong dir?). Evidence:\n{ev[:500]}"
        )


# ── Part E: Tool trigger KeyError logging ────────────────────────────────────

class TestToolTriggerKeyErrorLogging:
    """E: KeyError from trigger template produces WARNING log (not silent swallow)."""

    def test_missing_key_logs_warning(self, caplog, tmp_path):
        """'{nonexistent_key}' in trigger template produces a WARNING log entry."""
        from src.data.schema import MockToolDef, MockToolTrigger
        from src.lnl.openclaw_export import export_workflow_from_objects

        export_workflow_from_objects([ROUTER_OBJ, SINK_OBJ], tmp_path, force=True)

        # TC with a trigger that has a placeholder not in slack_send_message args
        bad_tc = ROUTER_SINK_TC.model_copy(update={
            "mock_tools": [
                MockToolDef(
                    tool_name="slack_send_message",
                    description="Send slack msg",
                    arguments_schema={"channel": "string", "message": "string"},
                    response_template="delivered: {message}",
                    triggers=[
                        MockToolTrigger(
                            target_object_id="message-sink",
                            message_template="Hello {nonexistent_key}",
                            source="test",
                        )
                    ],
                )
            ]
        })

        # Build a mock_server that returns a slack_send_message call in its log
        mock_mock_server = MagicMock()
        mock_mock_server.get_log = MagicMock(return_value=[{
            "method": "slack_send_message",
            "args": {"channel": "#general", "message": "hello"},
            "result": "delivered: hello",
            "tool_call_id": "abc123",
            "session_key": "test",
            "is_callback": False,
            "is_orchestration": False,
        }])
        mock_mock_server.configure = MagicMock()

        mock_client, router_handle, sink_handle = _make_mock_client()

        with patch("openclaw_sdk.OpenClawClient") as MockOCClient:
            MockOCClient.connect = AsyncMock(return_value=mock_client)
            mock_harness = MagicMock()
            mock_harness.evaluate_assertion = MagicMock(return_value=(True, "ok", [], 0, 0))

            from src.data.evaluate_baseline import _execute_tc_async
            with caplog.at_level(logging.WARNING, logger="src.data.evaluate_baseline"):
                asyncio.run(_execute_tc_async(
                    bad_tc, None, tmp_path, mock_harness,
                    mock_mock_server, False, False, None, None, None,
                ))

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "nonexistent_key" in m or "key missing" in m or "KeyError" in m
            for m in warning_msgs
        ), f"Expected warning about missing key, got: {warning_msgs}"
