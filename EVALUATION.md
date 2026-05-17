# Evaluation: LNL Runtime vs OpenClaw Baseline

Both evaluators use the same test cases, the same LLM judge (or judge panel), and produce the same output schema for direct comparison.

1. **LNL Runtime** (`evaluate`) — Multi-object: LLM-objects communicate via a message bus
2. **OpenClaw Baseline** (`evaluate_baseline`) — Single OpenClaw agent handles the entire workflow

---

## LNL Runtime

### Prerequisites

```bash
source .venv/bin/activate
# Requires API keys in .env — whichever providers you use:
#   OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY
```

### Running

```bash
python -m src.data.evaluate \
    -i outputs/data/zapier/20260322_010211/test_cases.jsonl \
    --runs 3 \
    --model gpt-4o
```

Use a separate model for the LLM judge (e.g., a stronger model to judge a cheaper object model):

```bash
python -m src.data.evaluate \
    -i outputs/data/zapier/20260322_010211/test_cases.jsonl \
    --model gpt-4o \
    --judge-model claude-sonnet-4-6
```

Use **multiple judges** for higher confidence — each `--llm-judge` flag adds a judge, and the panel decides by majority vote (ties count as fail):

```bash
python -m src.data.evaluate \
    -i outputs/data/zapier/20260322_010211/test_cases.jsonl \
    --model gpt-4o \
    --llm-judge gpt-4o \
    --llm-judge claude-sonnet-4-6
```

Each judge spec is either a bare model name (provider inferred from the name) or `provider/model`:

```bash
--llm-judge gpt-4o                         # inferred: openai
--llm-judge claude-sonnet-4-6              # inferred: anthropic
--llm-judge gemini-2.5-pro                 # inferred: google
--llm-judge anthropic/claude-sonnet-4-6    # explicit provider
```

**Agreement rules:**
- 2 judges: both must agree — disagreement counts as fail
- 3+ judges: simple majority vote — ties count as fail

Per-event results include a `judge_votes` field in the output JSONL when a panel is used, with each judge's individual verdict and reasoning:

```json
{
  "event_id": "E001",
  "passed": true,
  "reasoning": "PASS (2/2 judges agree) — openai/gpt-4o=PASS: ...; anthropic/claude-sonnet-4-6=PASS: ...",
  "judge_votes": [
    {"judge": "openai/gpt-4o", "passed": true, "reasoning": "..."},
    {"judge": "anthropic/claude-sonnet-4-6", "passed": true, "reasoning": "..."}
  ]
}
```

The `RunConfig` record (first line of the output JSONL) stores all judge specs in `judge_specs` for later reference.

> **Note:** `--llm-judge` takes precedence over `--judge-model` / `--judge-provider`. Use `--judge-model` for a single dedicated judge; use `--llm-judge` (repeatable) for a panel.

Add `--verbose` / `-v` to see per-event details during the run — what the judge expected, the evidence it saw, and its reasoning:

```bash
python -m src.data.evaluate \
    -i outputs/data/zapier/20260322_010211/test_cases.jsonl \
    --model gpt-4o --verbose
```

Debug specific test cases:

```bash
# Run test case at position 3 (1-based index), full run (steps + modifications + events)
python -m src.data.evaluate \
    -i outputs/data/zapier/20260322_010211/test_cases.jsonl \
    --tc 3 --verbose --debug-messages --model gpt-4o

# Run multiple test cases by index or ID
python -m src.data.evaluate \
    -i outputs/data/zapier/20260322_010211/test_cases.jsonl \
    --tc 1 3 5 TC007 --verbose --model gpt-4o

# Run only steps (no modifications/events) for baseline behavior
python -m src.data.evaluate \
    -i outputs/data/zapier/20260322_010211/test_cases.jsonl \
    --tc 1 --steps-only --debug-messages --model gpt-4o
```

Output: `test_cases_eval.jsonl` (next to input file)

#### Memory backend (`--memory`)

LLM-objects emit `state_update` deltas that the runtime applies to per-object state. The shape of those deltas — and the resulting state — is governed by a pluggable backend, selected per run with `--memory`:

| Value | Action shape | State shape | When to use |
|---|---|---|---|
| `nested` (default) | List of `{op, path, value}` actions with `op ∈ {set, merge, delete, append}` and dotted paths (`tickets.T-042.status`) | Nested JSON object; updates are applied immutably as in Redux, so a single-attribute change touches only that path | Default; targeted updates to nested entities, no full-record re-emit |
| `flat` | `{op, key, value}` with `op ∈ {set, delete, append}` | Flat top-level dict; nested entities are stored as whole sub-dicts and re-emitted in full on every attribute change | A/B comparison with historical (pre-2026-05-18) runs |

The two backends pair with two different executor prompts — `config/prompts/lnl/executor_nested.yaml` (nested) and `config/prompts/lnl/executor.yaml` (flat). `--memory` picks the right one automatically; do not override `--object-prompt` unless you know what you're doing.

```bash
# Default (nested): targeted path-based updates
python -m src.data.evaluate -i test_cases.jsonl --model gpt-4o

# Legacy flat: bit-for-bit identical to pre-2026-05-18 runs
python -m src.data.evaluate -i test_cases.jsonl --model gpt-4o --memory flat
```

A/B compare on the same TCs to see whether the nested action model improves precision on multi-attribute entity updates.

#### Mock tool execution

The LNL evaluator supports **in-process mock tools** — scripted implementations of external APIs (Slack, Email, HubSpot, Jira, etc.) that LLM-objects call directly via `tool_calls`, bypassing the message bus.

Mock tools are wired automatically when a test case has events with `triggered_by` set: the evaluator derives a `MockToolDef` for each unique tool name, and when an LLM-object calls that tool, the corresponding event input is injected into the target object via `inject_event`. All tool calls are logged and included as evidence for the judge.

For additional tool coverage, pass one or more YAML mock config files:

```bash
python -m src.data.evaluate \
    -i test_cases.jsonl \
    --model gpt-4o \
    --mock-config config/mocks/lnl/email.yaml \
    --mock-config config/mocks/lnl/slack.yaml
```

**Priority order** (highest wins on `tool_name` collision):

| Layer | Source | When to use |
|---|---|---|
| `tc.mock_tools` | Inline in each TestCase | Per-test-case scripted responses |
| `--mock-config` | Shared YAML files | Reusable boilerplate responses |
| `triggered_by` auto-derived | `Event.triggered_by` fields | Orchestration: tool call → event injection |

Any tool the LLM calls that isn't covered by the above layers hits a **PassthroughExecutor** fallback — it returns a generic success and logs the call for judge evidence, so evaluation never errors on unknown tools.

**Mock config YAML format** (`config/mocks/lnl/*.yaml`):

```yaml
tools:
  - tool_name: email.send
    description: Send an email to a recipient.
    arguments_schema:
      type: object
      properties:
        to: {type: string, description: Recipient email address}
        subject: {type: string, description: Subject line}
        body: {type: string, description: Email body}
      required: [to, subject, body]
    response_template: "Email sent to {to} (subject: '{subject}'). Message queued."
    # scripted_responses: consumed FIFO per call; {call_index} = 1-based call number
    # triggers: dispatch events to other objects when the tool fires
    triggers:
      - target_object_id: slack-notifier
        message_template: "[Email Sent] To: {to} | Subject: {subject}"
        source: email
```

#### Test case selection and debugging

**`--tc N [N2 ...]`** — Run specific test cases by 1-based index, ID, or `sample_id`:
- `--tc 3` — run test case at position 3
- `--tc 1 3 5` — run test cases 1, 3, and 5
- `--tc TC007 TC015` — run test cases by ID
- `--tc S001` — run all TCs that share `sample_id == "S001"` (all mod-type variants of the same sample)
- `--tc 2 TC010 S003` — mix indices, IDs, and sample IDs

Overrides `--limit`. Useful for isolating flaky or incomplete test cases.

**`--steps-only`** — Run only the steps (baseline behavior section); skip modifications and events. Useful for:
- Testing initialization behavior without scenario changes
- Debugging what happens when external systems first contact the objects
- Verifying that seed_data and object definitions are correct before applying test logic

**`--reuse-steps` / `--no-reuse-steps`** — Run base steps once per sample and reuse the resulting runtime state across all TC variants that share the same `sample_id`. Since the dataset has 6 variants per sample (different `mod_type` and `ambiguity` combinations), this reduces step executions from 498 to 83 — roughly a **6× reduction in step costs**. Enabled by default; use `--no-reuse-steps` to disable.

How it works: the first variant of each sample runs steps normally. After steps complete, the runtime state (object states, conversation histories, definitions, mock executor call counts) is snapshotted. Each subsequent variant restores that snapshot and proceeds directly to modifications and events, skipping step re-execution entirely.

Step `EventResult`s and raw event data are reused verbatim for all variants — they are identical by definition since all variants share the same base steps.

> Note: has no effect on TCs without a `sample_id`, or when `--steps-only` is set.

**`--debug-messages`** — Print all messages flowing through the message bus, including JSON envelopes for external events and internal peer communication. Shows sender, recipient, message type, and content.

---

## OpenClaw Baseline

The baseline uses a single OpenClaw agent that receives all object definitions as context and processes steps, modifications, and events as sequential messages in one conversation.

> **Running parallel evaluations?** Use the Docker worker pool instead of managing OpenClaw locally — each container is a fully isolated daemon. See [`docker/README.md`](docker/README.md).

### One-time setup

**1. Install OpenClaw** (if not already installed):

```bash
# Install Node 22+
brew install node@22
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"

# Install OpenClaw and run the interactive onboarding wizard
# (configures your model provider and API key, installs the daemon)
curl -fsSL https://openclaw.ai/install.sh | bash
openclaw onboard --install-daemon
```

**2. Install the mock tools plugin** (one time, after OpenClaw is installed):

```bash
cd plugins/openclaw-mock-external
npm install
npm run build       # builds and copies to ~/.openclaw/extensions/
```

Verify it loaded:
```bash
openclaw plugins list | grep lnl
# should show: lnl-mock-external | loaded
```

The script uses the `main` agent (OpenClaw's default). No separate agent creation needed.

### Before each session

The OpenClaw daemon must be running:

```bash
openclaw gateway status   # check first
openclaw gateway restart  # start or restart if not running
```

If the gateway has never been installed on this machine, run once:
```bash
openclaw config set gateway.mode local
openclaw gateway install
openclaw gateway restart
```

### Running

```bash
python -m src.data.evaluate_baseline \
    -i outputs/data/zapier/20260411_zapier_clean/test_cases.jsonl \
    --model gpt-4o-mini \
    --mock-server \
    --runs 3
```

`--model` configures the OpenClaw agent's model and provider (and injects the matching API key from `.env`) before the run starts — the same way `evaluate.py` works.

Output: `test_cases_baseline.jsonl` (next to input file)

**Common options:**

```bash
# Single test case (by 1-based index)
python -m src.data.evaluate_baseline -i test_cases.jsonl --model gpt-4o --tc 1 --mock-server

# Multiple test cases by index or ID
python -m src.data.evaluate_baseline -i test_cases.jsonl --model gpt-4o --tc 1 3 TC007 --mock-server

# Anthropic model
python -m src.data.evaluate_baseline -i test_cases.jsonl --model claude-sonnet-4-6

# Without mock tools (agent narrates actions as text, no real tool calls)
python -m src.data.evaluate_baseline -i test_cases.jsonl --model gpt-4o

# LLM-generated mock responses instead of scripted templates
python -m src.data.evaluate_baseline -i test_cases.jsonl --model gpt-4o --mock-server --mock-llm-mode

# Multi-agent mode: one OpenClaw agent per LNL-object (vs. default single combined agent)
python -m src.data.evaluate_baseline -i test_cases.jsonl --model gpt-4o --multi-agent
```

### How `--mock-server` works

Without `--mock-server` the agent can only narrate actions ("I would send a Slack message…") — there's no real feedback loop. With it:

**Outbound (agent → external system):** The `lnl-mock-external` plugin registers 12 tools (`slack_send_message`, `email_send`, `jira_create_issue`, etc.) with the OpenClaw daemon. When the agent calls one, the plugin forwards it to a local MockServer (FastAPI, `localhost:18888`), which returns a scripted response. The call is logged.

**Inbound (external system → agent):** After each tool call, the MockServer can fire a callback into the live session via OpenClaw's `/hooks/wake` endpoint (e.g., a Slack delivery confirmation a few seconds later).

**Per-test-case chaining:** Events with `triggered_by: "<event-id>"` are injected as follow-up messages immediately after their parent event is processed — no tool call matching required. Ordering is deterministic.

**Mock scripts** (`config/mocks/`): define boilerplate immediate responses per tool (delivery ACKs, message IDs, ticket numbers). Shared across all test cases.

**Generic orchestration scripts** (`config/mocks/orchestration/*.yaml`): define tool-call-triggered reactions for common patterns (email sent → Slack reply after N simulated minutes). Apply to any test case automatically when the keywords match.

To rebuild the plugin after making changes to `plugins/openclaw-mock-external/src/index.ts`:
```bash
cd plugins/openclaw-mock-external && npm run build
```
(The build script copies the output to `~/.openclaw/extensions/` automatically.)

### Parallel evaluation with Docker

Run multiple evaluations simultaneously without OpenClaw label collisions by using the Docker worker pool. Each container bundles an isolated OpenClaw gateway and mock server. Workers use host ports `19789`/`19888` (and up) so they don't collide with a locally-running OpenClaw instance on the default `18789`/`18888` ports — both can run at the same time.

**1. Build the image** (one time, from repo root):
```bash
docker build -f docker/Dockerfile -t lnl-openclaw-worker .
```

**2. Start the pool** using the provided script (reads your local OpenClaw operator token automatically):
```bash
./docker/start-pool.sh
```

**3. Run the evaluation with `--pool`** — dispatches all TCs across the worker pool automatically:
```bash
python -m src.data.evaluate_baseline \
  -i outputs/.../test_cases.jsonl \
  --pool docker/worker-pool.yaml \
  --model gpt-4o \
  --runs 3
```

`--pool` reads `docker/worker-pool.yaml`, distributes test cases dynamically across all workers (work-queue style), and writes a single merged output file. No `--mock-server-url` or `--gateway-url` flags needed — the pool YAML supplies them.

The pool can run alongside a local evaluation without port conflicts:
```bash
# Both can run simultaneously
python -m src.data.evaluate_baseline -i test_cases.jsonl --model gpt-4o --runs 1 &
python -m src.data.evaluate_baseline -i test_cases.jsonl --pool docker/worker-pool.yaml --model gpt-4o --runs 1 &
wait
```

See [`docker/README.md`](docker/README.md) for full setup details: bind-mount mechanics, plugin placement, session isolation, port assignments, and instructions for enabling more workers.

## CLI Flags

| Flag | LNL (`evaluate`) | Baseline (`evaluate_baseline`) |
|---|---|---|
| `--input`, `-i` | Test cases JSONL (required) | Test cases JSONL (required) |
| `--output`, `-o` | Output path | Output path |
| `--runs` | Runs per test case (default: 1) | Runs per test case (default: 1) |
| `--timeout` | Wall-clock seconds per step/event (default: 60) | Seconds per run (default: 120) |
| `--model`, `-m` | Model for LLM-objects | Model for OpenClaw agent (sets provider/model/API key from `.env`) |
| `--provider`, `-p` | `openai` or `anthropic` | `openai` or `anthropic` (overrides inference from `--model`) |
| `--llm-judge` | Judge model spec, repeatable for a panel (e.g. `--llm-judge gpt-4o --llm-judge claude-sonnet-4-6`). Overrides `--judge-model`. | N/A |
| `--judge-model` | Single judge model (default: same as `--model`). Ignored when `--llm-judge` is set. | Judge model (default: `gpt-4o-mini`) |
| `--judge-provider` | Judge provider (inferred from model name). Ignored when `--llm-judge` is set. | Judge provider (default: `openai`) |
| `--verbose`, `-v` | Print per-event evidence, expected, and judge reasoning | Print each message sent, agent response, and per-event pass/fail reasoning |
| `--agent-id` | N/A | OpenClaw agent ID (default: `main`) |
| `--gateway-url` | N/A | OpenClaw gateway URL (default: auto-detect) |
| `--limit`, `-n` | First N test cases only | First N test cases only |
| `--tc` | Specific test cases by 1-based index or ID (overrides `--limit`) | Same |
| `--steps-only` | Run only steps; skip modifications and events | N/A |
| `--memory` | Memory backend: `nested` (default; Redux-style `{op,path,value}` actions on a nested JSON tree) or `flat` (legacy `{op,key,value}` top-level deltas). Picks the matching executor prompt. | N/A |
| `--modifications N` | Limit to the first N modifications; events referencing later mods are skipped | Same |
| `--concurrency N` | Fire N events concurrently per mod window (0 = sequential, default). Requires TCs with `concurrent_group` fields. | N/A |
| `--reuse-steps` / `--no-reuse-steps` | Run steps once per sample; reuse state across variants (saves ~6× step cost). **Default: on.** | N/A |
| `--debug-messages` | Print messages exchanged between LLM-objects | N/A |
| `--mock-config` | YAML file(s) with shared mock tool definitions (repeatable) | N/A |
| `--mock-server` | N/A | Enable MockServer + plugin tool integration (local) |
| `--mock-server-url` | N/A | URL of a remote mock server (e.g. Docker container). Skips starting a local one. |
| `--pool` | N/A | Path to worker-pool YAML. Distributes TCs across Docker workers automatically. Overrides `--gateway-url`, `--mock-server-url`, and `--concurrency`. |
| `--mock-llm-mode` | N/A | Use LLM to generate mock responses instead of templates |
| `--mock-server-port` | N/A | MockServer port (default: 18888) |
| `--openclaw-http-url` | N/A | OpenClaw gateway HTTP URL for callbacks (default: http://localhost:18789) |
| `--multi-agent` | N/A | Use one agent per LNL-object (default: single combined agent) |

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

Per-test-case results (`TestCaseResult` lines) include per-event pass/fail with reasoning, token costs, the `expected` assertion condition, and the `evidence` text that was presented to the judge.

## Generating Test Cases

Test cases are generated from validated samples in `data/zapier/samples.jsonl`. The generation script produces JSONL files in `data/zapier/`.

### Basic usage

```bash
python -m src.data.generate_test_cases \
  -i data/zapier/samples.jsonl \
  -o data/zapier/test_cases__1mod.jsonl \
  --model gpt-4o \
  --workers 4
```

### Flavours

Different flavours are generated from the same base samples by varying the key parameters:

| Flavour | Command | Events per TC |
|---|---|---|
| 1 mod (baseline) | `--mods-per-scenario 1` | ~4 |
| 3 mods | `--mods-per-scenario 3` | ~8 |
| 3 mods + concurrency | `--mods-per-scenario 3 --concurrent-events 3` | ~8 + 6 concurrent groups |

### Generation parameters

| Parameter | Default | Description |
|---|---|---|
| `--mods-per-scenario N` | 1 | Modifications per scenario. Each mod refines the previous one. |
| `--events-before N` | 1 | Baseline events before the first modification (`pre_mod`). |
| `--events-inter-mod N` | 1 | Events per gap between consecutive modifications (`post_mod`). |
| `--events-after N` | 2 | Events after the last modification (`post_mod`). |
| `--events-unrelated N` | = mods | Irrelevant events, one per modification window. |
| `--concurrent-events N` | 0 | Events per concurrent group per mod window (0 = no concurrent groups). When >0, generates `cgroup_pre_{mod_id}` and `cgroup_post_{mod_id}` groups: 1 relevant + N-1 irrelevant events each. |
| `--scenario-count N` | 1 | Scenarios per (sample, mod-type) pair. |
| `--mod-type TYPE` | all | Modification type: `temporal`, `contextual`, `exception`, `correction`, `expansion`, `removal`, `mixed`. Default generates all types. |
| `--ambiguity LEVEL` | random | Ambiguity level for modification intents: `precise`, `semantic`, `vague`, `implicit`, `random`. |
| `--workers N` | 1 | Parallel workers. One per (sample, mod-type) unit. Safe up to ~12 with Anthropic/OpenAI. |

### Concurrency experiment

Generate once with N concurrent events, evaluate with different C values without re-generating:

```bash
# Generate with 4 concurrent events per group
python -m src.data.generate_test_cases \
  -i data/zapier/samples.jsonl \
  -o data/zapier/test_cases__3mod_conc4.jsonl \
  --mods-per-scenario 3 --concurrent-events 4 --model gpt-4o --workers 4

# Evaluate: sequential baseline
python -m src.data.evaluate -i data/zapier/test_cases__3mod_conc4.jsonl \
  --concurrency 0 --model gpt-4o

# Evaluate: 2 concurrent events (sampled from 4)
python -m src.data.evaluate -i data/zapier/test_cases__3mod_conc4.jsonl \
  --concurrency 2 --seed 42 --model gpt-4o

# Evaluate: all 4 concurrent events
python -m src.data.evaluate -i data/zapier/test_cases__3mod_conc4.jsonl \
  --concurrency 4 --seed 42 --model gpt-4o
```

`--seed` controls which C events are sampled from each group of N. The same seed always picks the same subset — different seeds explore different subsets.

### Evaluation-time slicing: `--modifications` and `--concurrency`

These two flags let you re-use the same generated file at different evaluation granularities without regenerating.

**`--modifications N`** — Limit evaluation to the first N modifications per TC. Events whose `after_mod_ids` reference mods beyond N are automatically excluded. This lets you evaluate a 3-mod file as a 1-mod or 2-mod run:

```bash
# Full 3-mod evaluation
python -m src.data.evaluate -i data/zapier/test_cases__3mod.jsonl --model gpt-4o

# Same file, evaluated as 1-mod only
python -m src.data.evaluate -i data/zapier/test_cases__3mod.jsonl --modifications 1 --model gpt-4o

# Same for the baseline
python -m src.data.evaluate_baseline -i data/zapier/test_cases__3mod.jsonl --modifications 1 --model gpt-4o
```

**`--concurrency N`** (LNL only) — Fire N events concurrently per modification window. Requires TCs generated with `--concurrent-events`. With `--concurrency 0` (default), concurrent groups are ignored entirely and each event fires sequentially, identical to files without concurrent groups:

```bash
# Sequential (default)
python -m src.data.evaluate -i data/zapier/test_cases__3mod_conc4.jsonl --concurrency 0 --model gpt-4o

# Fire 2 concurrent events per group (sampled from the 4 generated)
python -m src.data.evaluate -i data/zapier/test_cases__3mod_conc4.jsonl --concurrency 2 --seed 42 --model gpt-4o
```

Both flags compose: `--modifications 1 --concurrency 2` evaluates only the first modification and fires at most 2 concurrent events around it.

## Analysis

See **[ANALYSIS.md](ANALYSIS.md)** for charts, insights summary, and CLI flags.

---

## How They Differ

**LNL Runtime**: Creates separate LLM-objects for each component (e.g., `hubspot`, `quote-approvals`, `slack`). Each object has its own system prompt, state, and conversation history. Messages route through a `MessageBus` — when one object processes an event, it can send messages to peers, triggering a chain of LLM calls.

**OpenClaw Baseline**: A single OpenClaw agent receives ALL component definitions in one system prompt. Steps, modifications, and events are sent as sequential messages in one conversation. The agent tracks all state internally and describes what actions it took.

The judge is identical: an LLM evaluates whether the expected assertion holds given the observable evidence (replies, actions, state).
