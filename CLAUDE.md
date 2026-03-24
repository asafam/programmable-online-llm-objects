# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
# Create venv (one-time)
[ -d .venv ] || uv venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

Requires a `.env` file with `OPENAI_API_KEY` and/or `ANTHROPIC_API_KEY`.

## Common Commands

```bash
# Run tests (unit — no API key needed)
pytest tests/test_object.py tests/test_bus.py tests/test_parser.py tests/test_runtime.py tests/test_mocks.py tests/test_benchmark.py -v

# Run scenario tests (requires OPENAI_API_KEY)
pytest tests/test_scenario.py -v -s

# CLI
python -m src.lnl.cli --provider openai load programs/hotel/objects/
python -m src.lnl.cli --provider openai send guest-manager "Check in Alice"

# Data generation pipeline
python -m src.data.pipeline -i data/zapier/raw/templates.yaml --target-dir outputs/my-run
python -m src.data.pipeline -i data/zapier/raw/templates.yaml --target-dir outputs/my-run  # continues if samples.jsonl exists
python -m src.data.pipeline --samples outputs/data/zapier/templates_samples_object.jsonl  # skip stage 1 explicitly

# Evaluation — see EVALUATION.md for full details
python -m src.data.evaluate -i outputs/data/zapier/20260322_120000/test_cases.jsonl --runs 3
python -m src.data.evaluate_baseline -i outputs/data/zapier/20260322_010211/test_cases.jsonl --runs 3
```

## Architecture

### LNL Runtime (`src/lnl/`)

LLM-objects communicate via natural language messages through a message bus. Definitions are written in Markdown and can be modified at runtime while state persists.

- **LLMObject** (`object.py`) — Definition + brain + mutable NL state string. Processes messages via LLM.
- **MessageBus** (`bus.py`) — Routes messages between objects. Supports peer-to-peer (with peer validation), pub/sub, broadcast, and synchronous chaining with depth limit.
- **LLMBrain** (`brain.py`) — Abstract LLM interface. OpenAI, Anthropic, and Mock implementations.
- **Runtime** (`runtime.py`) — Library API: load, send, modify, inspect objects.
- **Parser** (`parser.py`) — Markdown ↔ ObjectDefinition serializer.

**Flow:** `Runtime.send(target, msg)` → `MessageBus.send()` → `LLMObject.process_message()` → LLM returns `{updated_state, reply, outgoing_messages}` → bus recursively delivers outgoing messages.

### Data Generation Pipeline (`src/data/`)

Two-stage LLM pipeline generating test cases from automation templates:

- **Stage 1** (`generate_samples.py`): Raw YAML templates → concrete sample instances (JSONL)
- **Stage 2** (`generate_test_cases.py`): Samples → test cases with modifications and events (JSONL)
- **Stage 3** (`evaluate.py`): Test cases → evaluation results with pass/fail per event, token costs, and aggregate metrics. Supports `--runs N` for behavioral consistency measurement.

Key design: `mod_type` and `ambiguity` are **script-controlled**, not LLM-generated. The LLM produces `GeneratedModification` (id, when, intent only). The script assigns `mod_type` and `ambiguity` during `scenario_to_test_case` conversion. For `--mod-type mixed` or `--ambiguity random`, the script samples values per iteration.

**Schemas** (`schema.py`): `GeneratedModification` (LLM output) vs `Modification` (final output with script-assigned fields). `Scenario` uses `GeneratedModification`; `TestCase` uses `Modification`.

**Output path** is derived from input filename, mod-type, and ambiguity (e.g., `samples__temporal__vague.jsonl`).

### Baseline Evaluation (`src/data/evaluate_baseline.py`)

Single-agent comparison using OpenClaw. See [EVALUATION.md](EVALUATION.md) for setup, usage, and comparison details.

## Configuration

- `config/prompts/lnl/object.yaml` — System prompt template for LLM-objects
- `config/prompts/baseline/agent.yaml` — System prompt template for the OpenClaw baseline agent
- `config/prompts/data-gen/` — Data generation prompt templates (use `{PLACEHOLDER}` substitution)

## Skills

- `/commit` — Creates a git commit using haiku (cheaper/faster model). Accepts optional message guidance: `/commit fix ambiguity handling`.

## Principles

- Never hardcode domain-specific logic — keep code generic, configurable, LLM-driven
- Prefer YAML configs over hardcoded values
- Maintain clean object separation with message passing via MessageBus
- All domain behavior should be configurable or user-specified
