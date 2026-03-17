"""Tests for Runtime (Phase 4)."""
import pytest
from pathlib import Path

from src.lnl import (
    LLMResponse,
    MockBrain,
    ObjectDefinition,
    PeerDeclaration,
)
from src.lnl.runtime import Runtime


@pytest.fixture
def brain():
    b = MockBrain()
    b.set_default(LLMResponse(updated_state="processed", reply="ok"))
    return b


@pytest.fixture
def rt(brain):
    return Runtime(brain, strict_peers=False)


class TestLoadDirectory:
    def test_loads_all_md_files(self, rt, tmp_path):
        (tmp_path / "a.md").write_text("# Alpha\n\n## Role\n\nDoes alpha work.\n")
        (tmp_path / "b.md").write_text("# Beta\n\n## Role\n\nDoes beta work.\n")
        (tmp_path / "not-md.txt").write_text("ignored")

        objects = rt.load_directory(tmp_path)

        assert len(objects) == 2
        ids = {o.object_id for o in objects}
        assert ids == {"alpha", "beta"}

    def test_load_file(self, rt, tmp_path):
        (tmp_path / "obj.md").write_text("# My Object\n\n## Role\n\nTest role.\n")

        obj = rt.load_file(tmp_path / "obj.md")

        assert obj.object_id == "my-object"


class TestSendRoutes:
    def test_send_routes_through_bus(self, rt):
        rt.create_object(ObjectDefinition(
            object_id="worker",
            role="Worker role.",
        ))

        results = rt.send("worker", "do work")

        assert len(results) == 1
        assert results[0].object_id == "worker"
        assert results[0].reply == "ok"

    def test_broadcast(self, rt):
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.create_object(ObjectDefinition(object_id="b", role="B"))

        results = rt.broadcast("hello all")

        assert len(results) == 2

    def test_publish_to_topic(self, rt):
        rt.create_object(ObjectDefinition(
            object_id="sub",
            role="Subscriber",
            subscriptions=["news"],
        ))
        rt.create_object(ObjectDefinition(object_id="other", role="Other"))

        results = rt.publish("news", "breaking")

        assert len(results) == 1
        assert results[0].object_id == "sub"


class TestModify:
    def test_modify_preserves_state(self, brain):
        rt = Runtime(brain, strict_peers=False)
        brain.script("worker", LLMResponse(
            updated_state="has state",
            reply="ok",
        ))
        rt.create_object(ObjectDefinition(object_id="worker", role="Original role."))

        rt.send("worker", "init")
        assert rt.state("worker") == "has state"

        rt.modify("worker", role="New role.")

        assert rt.state("worker") == "has state"
        assert rt.has_unsaved_modifications("worker")

    def test_add_remove_peer(self, rt):
        rt.create_object(ObjectDefinition(object_id="a", role="A"))

        rt.add_peer("a", "b", "helper")
        topo = rt.topology()
        assert "b" in topo["a"]

        rt.remove_peer("a", "b")
        topo = rt.topology()
        assert "b" not in topo["a"]


class TestTopology:
    def test_reflects_structure(self, rt):
        rt.create_object(ObjectDefinition(
            object_id="a",
            role="A",
            peers=[PeerDeclaration("b", "peer")],
        ))
        rt.create_object(ObjectDefinition(object_id="b", role="B"))

        topo = rt.topology()
        assert topo == {"a": ["b"], "b": []}


class TestPersistence:
    def test_save_reload_roundtrip(self, brain, tmp_path):
        rt = Runtime(brain, strict_peers=False)
        rt.create_object(ObjectDefinition(
            object_id="worker",
            role="Worker role.",
            state_description="Track tasks.",
        ))

        path = rt.save_object("worker", tmp_path / "worker.md")
        assert path.exists()

        # Reload in a new runtime
        rt2 = Runtime(brain, strict_peers=False)
        obj = rt2.load_file(path)
        assert obj.object_id == "worker"
        assert obj.definition.role == "Worker role."

    def test_save_clears_modified(self, rt, tmp_path):
        rt.create_object(ObjectDefinition(object_id="x", role="X"))
        rt.modify("x", role="Updated")
        assert rt.has_unsaved_modifications("x")

        rt.save_object("x", tmp_path / "x.md")
        assert not rt.has_unsaved_modifications("x")


class TestMetrics:
    def test_metrics_after_send(self, rt):
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.send("a", "hello")

        assert rt.metrics.messages_routed == 1

    def test_message_log(self, rt):
        rt.create_object(ObjectDefinition(object_id="a", role="A"))
        rt.send("a", "hello")

        assert len(rt.message_log) == 1
        assert rt.message_log[0].delivered is True


class TestCreateFromText:
    def test_create_from_markdown(self, rt):
        obj = rt.create_object_from_text("# Worker\n\n## Role\n\nDoes work.\n")
        assert obj.object_id == "worker"
        results = rt.send("worker", "hi")
        assert len(results) == 1
