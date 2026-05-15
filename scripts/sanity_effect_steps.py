"""Sanity check: verify the planner fires for sink objects and produces
effect steps. Runs a real planner call against TC 6 sink objects.

Usage:
    python scripts/sanity_effect_steps.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

from src.lnl.brain import AzureBrain, build_planner_prompt, plan_dict_to_plan
from src.lnl.types import Message, MessageType, ObjectDefinition, PeerDeclaration

MODEL = os.environ.get("SANITY_MODEL", "gpt-5.4-mini")

# ── Orchestrator message that a sink would receive ───────────────────────────

ORCHESTRATOR_PAYLOAD = (
    "Sales call feedback record for Jordan Alvarez (call-20240617-9104). "
    "Rubric scores — talk-to-listen: 2/6, objection handling: 2/6, "
    "next steps: 2/6, product knowledge: 3/6, rapport: 3/6. "
    "Total: 12/30. Escalation flag: true (score < 15). Manager: Sandra Kim."
)

SINK_MESSAGE = Message(
    sender="sales-call-coach",
    recipient="coaching-store",
    type=MessageType.DOMAIN,
    content=ORCHESTRATOR_PAYLOAD,
    depth_remaining=8,
    id="test-msg-01",
)

# ── Sink object definitions ──────────────────────────────────────────────────

COACHING_STORE = ObjectDefinition(
    object_id="coaching-store",
    role="Zapier Table write service",
    behavior=(
        "Write the sales coaching feedback record to the Zapier Table. "
        "Store all fields: rep identity, call metadata, rubric dimension scores, "
        "total score, escalation flag, and full feedback text."
    ),
    peers=[],
)

SLACK_SINK = ObjectDefinition(
    object_id="slack-sales-coaching",
    role="Slack message sender",
    behavior=(
        "Post the sales coaching feedback summary to the #sales-coaching Slack channel. "
        "Address the message to the rep. When escalation flag is true, also post a "
        "separate escalation message addressed to the manager."
    ),
    peers=[],
)


def check_sink(defn: ObjectDefinition, brain: AzureBrain) -> None:
    print(f"\n{'='*60}")
    print(f"Object: {defn.object_id}  (peers={len(defn.peers)})")
    print(f"Role: {defn.role}")

    prompt = build_planner_prompt(defn, {}, SINK_MESSAGE)
    plan_dict, metrics = brain.plan_call(prompt, object_id=defn.object_id)
    plan = plan_dict_to_plan(plan_dict)

    print(f"\nPlanner goal: {plan_dict.get('goal')}")
    print(f"Steps ({len(plan.steps)} executable, {len(plan_dict.get('steps', []))} raw):")
    for i, s in enumerate(plan.steps):
        tgt = f" → {s.target}" if s.target else ""
        print(f"  [{i}] kind={s.kind}{tgt}: {s.description}")

    raw_steps = plan_dict.get("steps", [])
    n_effect = sum(1 for s in raw_steps if s.get("kind") == "effect")
    n_tell   = sum(1 for s in raw_steps if s.get("kind") == "tell")
    n_ask    = sum(1 for s in raw_steps if s.get("kind") == "ask")
    n_final  = sum(1 for s in raw_steps if s.get("kind") == "final")

    print(f"\nRaw step breakdown: effect={n_effect} tell={n_tell} ask={n_ask} final={n_final}")
    print(f"Tokens: in={metrics.input_tokens} out={metrics.output_tokens} ({metrics.latency_ms:.0f}ms)")

    # Assertions
    has_effect = any(s.kind == "effect" for s in plan.steps)
    has_tell_to_peer = any(s.kind == "tell" and s.target for s in plan.steps)

    if has_effect and not has_tell_to_peer:
        print("✓ PASS: sink produced effect steps (no spurious peer dispatches)")
    elif has_effect:
        print("~ WARN: sink produced effect steps but also tell steps (check for peer refs)")
    else:
        print("✗ FAIL: sink produced NO effect steps — model ignored rubric instructions")

    # Show full reasoning for effect steps
    for raw in raw_steps:
        if raw.get("kind") == "effect":
            print(f"\n  Effect reasoning: {raw.get('reasoning', '(none)')}")


def main() -> None:
    brain = AzureBrain(model=MODEL)
    print(f"Model: {MODEL}")
    check_sink(COACHING_STORE, brain)
    check_sink(SLACK_SINK, brain)
    print("\n" + "="*60)
    print("Sanity check complete.")


if __name__ == "__main__":
    main()
