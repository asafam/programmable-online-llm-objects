"""Tests for MD parser (Phase 3)."""
import pytest

from src.lnl.parser import parse_object_text, serialize_object, slugify
from src.lnl.types import ObjectDefinition, PeerDeclaration


FULL_MD = """\
# Guest Manager

## Role

Manages guest check-in and check-out at the hotel front desk.

## State

Track current guests, room assignments, and pending requests.

## Behavior

When a guest checks in, assign them a room and notify housekeeping.
When a guest checks out, update availability and process billing.

## Peers

- room-tracker: Knows room availability
- billing-system: Handles payments

## Skills

- check-in
- check-out
- room-lookup

## Subscriptions

- housekeeping-events
- billing-events

## Event Sources

- PMS webhook: new reservation created
- PMS webhook: guest check-out completed
"""

MINIMAL_MD = """\
# Simple Worker

## Role

Does simple work.
"""


class TestSlugify:
    def test_basic(self):
        assert slugify("Guest Manager") == "guest-manager"

    def test_with_special_chars(self):
        assert slugify("My (Cool) Object!") == "my-cool-object"

    def test_already_slug(self):
        assert slugify("guest-manager") == "guest-manager"


class TestParseObjectText:
    def test_full_definition(self):
        defn = parse_object_text(FULL_MD)
        assert defn.object_id == "guest-manager"
        assert "guest check-in" in defn.role
        assert "current guests" in defn.state_description
        assert "checks in" in defn.behavior
        assert len(defn.peers) == 2
        assert defn.peers[0].object_id == "room-tracker"
        assert defn.peers[0].relationship == "Knows room availability"
        assert defn.peers[1].object_id == "billing-system"
        assert defn.skills == ["check-in", "check-out", "room-lookup"]
        assert defn.subscriptions == ["housekeeping-events", "billing-events"]
        assert defn.event_sources == ["PMS webhook: new reservation created", "PMS webhook: guest check-out completed"]

    def test_minimal_definition(self):
        defn = parse_object_text(MINIMAL_MD)
        assert defn.object_id == "simple-worker"
        assert defn.role == "Does simple work."
        assert defn.state_description == ""
        assert defn.behavior == ""
        assert defn.peers == []
        assert defn.skills == []
        assert defn.subscriptions == []
        assert defn.event_sources == []

    def test_missing_h1_raises(self):
        with pytest.raises(ValueError, match="Missing H1"):
            parse_object_text("## Role\n\nSome role")

    def test_missing_role_raises(self):
        with pytest.raises(ValueError, match="Role"):
            parse_object_text("# No Role Object\n\n## Behavior\n\nDoes stuff")


class TestSerialize:
    def test_roundtrip(self):
        original = parse_object_text(FULL_MD)
        serialized = serialize_object(original)
        roundtripped = parse_object_text(serialized)

        assert roundtripped.object_id == original.object_id
        assert roundtripped.role == original.role
        assert roundtripped.state_description == original.state_description
        assert roundtripped.behavior == original.behavior
        assert len(roundtripped.peers) == len(original.peers)
        for a, b in zip(roundtripped.peers, original.peers):
            assert a.object_id == b.object_id
            assert a.relationship == b.relationship
        assert roundtripped.skills == original.skills
        assert roundtripped.subscriptions == original.subscriptions
        assert roundtripped.event_sources == original.event_sources

    def test_minimal_roundtrip(self):
        original = parse_object_text(MINIMAL_MD)
        serialized = serialize_object(original)
        roundtripped = parse_object_text(serialized)
        assert roundtripped.object_id == original.object_id
        assert roundtripped.role == original.role
