# Live Natural Language Programming

Research runtime for **Live Natural Language Programming** — a paradigm where programs are collections of LLM-objects that communicate via natural language messages, and definitions can be modified at runtime while state persists.

## Prerequisites

- Python 3.9+
- [uv](https://docs.astral.sh/uv/) package manager
- OpenAI and/or Anthropic API keys

## Setup

```bash
[ -d .venv ] || uv venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

Create a `.env` file:
```
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
```

## Key Concepts

### LLM-Object

The single runtime entity. Each object has:

- **Definition** (from markdown) — role, behavior, peers, skills, subscriptions
- **Brain** — an LLM provider that processes messages
- **State** — a natural language string, managed entirely by the LLM

The core "live" property: **definitions can change while state persists**. Modify an object's role or behavior mid-execution and it continues with its accumulated state.

### Message Bus

Objects communicate through a bus with:

- **Peer-to-peer** messages (validated against peer declarations)
- **Topic subscriptions** (pub/sub)
- **Broadcast** (to all objects)
- **Synchronous chains** — if A messages B and B messages C, all results return from a single `send()` call
- **Chain depth limit** (default 10) prevents infinite loops

### MD Definitions

Objects are defined in markdown files:

```markdown
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

## Subscriptions

- housekeeping-events
```

The H1 heading is slugified into the `object_id` ("Guest Manager" becomes `guest-manager`). Only `## Role` is required.

## Usage

### Library API

```python
from src.lnl import Runtime, MockBrain, OpenAIBrain

# Use MockBrain for testing, OpenAIBrain/AnthropicBrain for real LLM calls
brain = OpenAIBrain(model="gpt-4o-mini")
rt = Runtime(brain, strict_peers=False)

# Load objects from markdown files
rt.load_directory("programs/hotel/objects/")

# Send a message
results = rt.send("guest-manager", "Check in Alice to a standard room")
for r in results:
    print(f"[{r.object_id}] {r.reply}")
    print(f"  State: {r.state_after}")

# Modify a definition at runtime (state persists)
rt.modify("guest-manager", behavior="Also offer room upgrades on check-in.")

# Inspect
print(rt.state("guest-manager"))
print(rt.topology())

# Save modified definition back to disk
rt.save_object("guest-manager")
```

### CLI

```bash
# Load and interact
python -m src.lnl.cli --provider openai load programs/hotel/objects/
python -m src.lnl.cli --provider openai send guest-manager "Check in Alice"
python -m src.lnl.cli --provider openai state guest-manager
python -m src.lnl.cli --provider openai topology

# Modify at runtime
python -m src.lnl.cli --provider openai modify guest-manager --role "Senior front desk manager"

# Save changes
python -m src.lnl.cli --provider openai save guest-manager --path out/guest-manager.md

# Run benchmarks
python -m src.lnl.cli --provider openai run scenarios/hotel-checkin/
```

CLI commands: `load`, `new`, `send`, `event`, `modify`, `state`, `snapshot`, `topology`, `log`, `save`, `run`

### Benchmarks

Scenarios are defined as folders:

```
scenarios/hotel-checkin/
├── objects/
│   ├── guest-manager.md
│   └── room-tracker.md
├── scenario.yaml
└── mocks.yaml          # optional
```

`scenario.yaml`:
```yaml
name: hotel-checkin
steps:
  - action: send
    target: guest-manager
    content: "Check in Alice to a standard room"
  - action: modify
    target: guest-manager
    modifications:
      behavior: "Also offer room upgrades on check-in."
  - action: send
    target: guest-manager
    content: "Check in Bob"
assertions:
  - type: state
    target: guest-manager
    condition: "Both Alice and Bob are checked in"
  - type: reply
    target: guest-manager
    condition: "Bob was offered a room upgrade"
```

Assertion types: `state`, `reply`, `bus_log`, `mock_recording`. Evaluation uses LLM-as-judge for semantic matching.

### Testing with MockBrain

```python
from src.lnl import MockBrain, LLMResponse, Runtime, ObjectDefinition

brain = MockBrain()
brain.script("worker", LLMResponse(
    updated_state="task completed",
    reply="Done!",
))

rt = Runtime(brain, strict_peers=False)
rt.create_object(ObjectDefinition(object_id="worker", role="Does tasks"))
results = rt.send("worker", "do the thing")

assert results[0].reply == "Done!"
assert brain.call_log[0].message.content == "do the thing"
```

## Architecture

```
src/lnl/
├── __init__.py      # Public API exports
├── types.py         # Core data types (Message, ObjectDefinition, etc.)
├── brain.py         # LLM provider abstraction (OpenAI, Anthropic, Mock)
├── object.py        # LLMObject — definition + brain + mutable NL state
├── bus.py           # MessageBus — routing, peer validation, chaining
├── parser.py        # MD parser and serializer
├── runtime.py       # Runtime — library API tying everything together
├── mocks.py         # Mock external services for benchmarks
├── benchmark.py     # Benchmark harness with LLM-as-judge
└── cli.py           # CLI wrapper
```

## Tests

```bash
pytest tests/test_object.py tests/test_bus.py tests/test_parser.py tests/test_runtime.py tests/test_mocks.py tests/test_benchmark.py -v
```

All tests use `MockBrain` — no API keys needed.

## Legacy System

The original actor-based system is in `src/system/` and can still be run:

```bash
python -m src.app --provider openai --model gpt-4o-mini
```
