"""Tests for MockService and MockRegistry (Phase 5)."""
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
