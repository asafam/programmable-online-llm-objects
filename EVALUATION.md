# Evaluation: LNL Runtime vs OpenClaw Baseline

This document describes how to run and compare the two evaluation modes:

1. **LNL Runtime** (`evaluate`) — Multi-object paradigm where LLM-objects communicate via a message bus
2. **OpenClaw Baseline** (`evaluate_baseline`) — Single OpenClaw agent handling the entire workflow

Both use the same test cases, the same judge, and produce the same output schema for apples-to-apples comparison.

## Prerequisites

### LNL Runtime

```bash
source .venv/bin/activate
# Requires OPENAI_API_KEY and/or ANTHROPIC_API_KEY in .env
```

### OpenClaw Baseline

Requires Node 22+ and Python 3.11+.

```bash
# 1. Install Node 22 (if needed)
brew install node@22
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"

# 2. Install OpenClaw
curl -fsSL https://openclaw.ai/install.sh | bash

# 3. Onboard (interactive — configure model provider + API key)
openclaw onboard --install-daemon

# 4. Verify gateway is running
openclaw gateway status

# 5. Install Python SDK
source .venv/bin/activate
uv pip install openclaw-sdk
```

## Running the Evaluations

### LNL Runtime (multi-object)

```bash
python -m src.data.evaluate \
    -i outputs/data/zapier/20260322_010211/test_cases.jsonl \
    --runs 3 \
    --model gpt-4o
```

Output: `test_cases_eval.jsonl` (next to input file)

### OpenClaw Baseline (single agent)

```bash
python -m src.data.evaluate_baseline \
    -i outputs/data/zapier/20260322_010211/test_cases.jsonl \
    --runs 3
```

Output: `test_cases_baseline.jsonl` (next to input file)

## CLI Flags

| Flag | LNL (`evaluate`) | Baseline (`evaluate_baseline`) |
|---|---|---|
| `--input`, `-i` | Test cases JSONL (required) | Test cases JSONL (required) |
| `--output`, `-o` | Output path | Output path |
| `--runs` | Runs per test case (default: 1) | Runs per test case (default: 1) |
| `--timeout` | Seconds per run (default: 120) | Seconds per run (default: 120) |
| `--model`, `-m` | Model for objects + judge | N/A (configured in OpenClaw) |
| `--provider`, `-p` | `openai` or `anthropic` | N/A |
| `--agent-id` | N/A | OpenClaw agent ID (default: `lnl-baseline`) |
| `--gateway-url` | N/A | OpenClaw gateway URL (default: auto-detect) |
| `--judge-model` | N/A (same as `--model`) | Judge model (default: `gpt-4o-mini`) |
| `--judge-provider` | N/A | Judge provider (default: `openai`) |
| `--limit`, `-n` | First N test cases only | First N test cases only |

## Comparing Results

The last line of each output JSONL is an `EvalSummary` with aggregate metrics:

```bash
tail -1 outputs/.../test_cases_eval.jsonl | python -m json.tool
tail -1 outputs/.../test_cases_baseline.jsonl | python -m json.tool
```

Key metrics in `EvalSummary`:

| Metric | Description |
|---|---|
| `mean_pass_rate` | Average correctness across all test case runs |
| `pass_rate_std` | Behavioral consistency (std dev across runs per test case) |
| `mean_event_input_tokens` | Average input tokens per event |
| `mean_event_output_tokens` | Average output tokens per event |
| `mean_event_latency_ms` | Average latency per event |
| `mean_mod_input_tokens` | Average input tokens per modification |
| `mean_mod_output_tokens` | Average output tokens per modification |
| `mean_mod_latency_ms` | Average latency per modification |

Per-test-case results (`TestCaseResult` lines) include per-event pass/fail with reasoning and token costs.

## How They Differ

**LNL Runtime**: Creates separate LLM-objects for each component (e.g., `hubspot`, `quote-approvals`, `slack`). Each object has its own system prompt, state, and conversation history. Messages route through a `MessageBus` — when one object processes an event, it can send messages to peers, triggering a chain of LLM calls.

**OpenClaw Baseline**: A single OpenClaw agent receives ALL component definitions in one system prompt. Steps, modifications, and events are sent as sequential messages in one conversation. The agent tracks all state internally and describes what actions it took.

The judge is identical: an LLM evaluates whether the expected assertion holds given the observable evidence (replies, actions, state).
