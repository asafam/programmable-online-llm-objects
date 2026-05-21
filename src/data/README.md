# Test Case Data Generator

Two-stage pipeline for generating test cases from Zapier automation templates using LLM-based generation.

## Pipeline Overview

```
Templates (YAML) → [generate_samples] → Samples (JSONL) → [generate_test_cases] → Test Cases (JSONL)
```

1. **Stage 1: Generate Samples** - Instantiate templates with concrete values and (with `--step-style object`) identify LLM-objects including service objects
2. **Stage 2: Generate Test Cases** - Create scenarios with modifications and events targeting the objects from Stage 1

## Quick Start

```bash
# Full pipeline into a target folder
python -m src.data.pipeline -i data/zapier/raw/templates.yaml --target-dir outputs/my-run

# Re-run (continues automatically: stage 1 skipped if workflows.jsonl exists)
python -m src.data.pipeline -i data/zapier/raw/templates.yaml --target-dir outputs/my-run

# Skip stage 1 with a specific samples file
python -m src.data.pipeline --workflows outputs/data/zapier/templates_samples_object.jsonl

# Stages can also be run individually
python -m src.data.generate_samples -i data/zapier/raw/templates.yaml --step-style object
python -m src.data.generate_test_cases -i outputs/data/zapier/templates_samples_object.jsonl
```

---

## Stage 1: Generate Samples

Instantiates raw templates with specific, realistic values.

### Usage

```bash
python -m src.data.generate_samples \
    --input data/zapier/raw/examples.yaml \
    --model claude-sonnet-4-5-20250929 \
    --workflows-per-template 3
```

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--input`, `-i` | (required) | Path to raw templates YAML file |
| `--output`, `-o` | `outputs/data/zapier/<stem>_samples_<style>.jsonl` | Output JSONL path (derived from input and step-style) |
| `--prompt-template` | `config/prompts/data-gen/generate_samples.yaml` | Prompt template path |
| `--model`, `-m` | `claude-sonnet-4-5-20250929` | Model name (provider auto-detected) |
| `--seed`, `-s` | None | Random seed for reproducibility |
| `--workflows-per-template` | `1` | Samples to generate per template |
| `--step-style` | `plain` | `plain` (rewrite steps only) or `object` (identify LLM-objects and rewrite steps using them) |
| `--temperature` | `0.7` | LLM temperature |
| `--force` | False | Regenerate all templates |
| `--limit`, `-n` | None | Process only first N templates |

### Step Styles

- **`plain`** — Instantiates placeholders in steps, no object decomposition.
- **`object`** — Identifies all components as LLM-objects and rewrites steps using them. Objects fall into two categories:
  - **Service objects** — represent external systems (Slack, Active Directory, databases). Read services have seeded state with reference data; write services record messages in state.
  - **Business logic objects** — own domain data and decision-making. Interact with external systems by sending messages to service objects via peer declarations.

### Output

Plain style:
```json
{"id": "it-helpdesk", "name": "IT Help Desk", "domain": "it-support", "source_type": "Zapier", "link": "https://...", "raw_steps": [...], "objects": [], "steps": ["When ticket arrives in #support-tickets...", ...]}
```

Object style:
```json
{"id": "it-helpdesk", "name": "IT Help Desk", "domain": "it-support", "source_type": "Zapier", "link": "https://...", "raw_steps": [...], "objects": [{"object_id": "support-triage", "role": "...", "state_description": "...", "behavior": "...", "peers": [{"object_id": "slack", "relationship": "..."}], "skills": [], "subscriptions": []}], "steps": ["Create object `support-triage` that ...", ...]}
```

---

## Stage 2: Generate Test Cases

Creates test scenarios with modifications and events from samples.

### Usage

```bash
python -m src.data.generate_test_cases \
    --input outputs/data/zapier/generated/workflows.jsonl \
    --model claude-sonnet-4-5-20250929 \
    --scenario-count 1
```

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--input`, `-i` | (required) | Path to samples JSONL (from Stage 1) |
| `--output`, `-o` | `<input_stem>__<mod-type>__<ambiguity>.jsonl` | Output JSONL path (derived from input, mod-type, and ambiguity) |
| `--prompt-template` | `config/prompts/data-gen/generate_test_cases.yaml` | Prompt template path |
| `--model`, `-m` | `claude-sonnet-4-5-20250929` | Model name (provider auto-detected) |
| `--seed`, `-s` | None | Random seed for reproducibility |
| `--mod-type` | None | Modification type: `temporal`, `contextual`, `exception`, `correction`, `expansion`, `removal`, or `mixed` (random types). If omitted, generates all types separately. |
| `--ambiguity` | `random` | Ambiguity level: `precise`, `semantic`, `vague`, `implicit`, or `random`. When `random`, the script samples a random level per iteration. |
| `--mods-per-scenario` | `1` | Number of modifications per scenario |
| `--scenario-count` | `1` | Scenarios per modification type |
| `--events-before` | `1` | Events before modification |
| `--events-after` | `2` | Events after modification |
| `--events-unrelated` | `1` | Events unaffected by modification |
| `--temperature` | `0.7` | LLM temperature |
| `--force` | False | Regenerate all samples |
| `--limit`, `-n` | None | Process only first N samples |

### Output

```json
{"id": "it-helpdesk-TC001", "name": "IT Help Desk", "domain": "it-support", "modifications": [{"id": "M001", "when": "W02-1T10:30", "mod_type": "temporal", "intent": "...", "ambiguity": "precise"}], "events": [...]}
```

---

## Mock Tool Coverage

Test cases require mock tools for any external data lookup (org directories, employee records,
product catalogs, etc.) that an LLM-object performs at evaluation time. The pipeline generates
these during Stage 1, but may miss lookups that are embedded in business logic objects rather
than in explicit read-service objects.

Two complementary scripts close this gap on an existing `samples.jsonl`:

### retrofit_mock_tools — static analysis

Analyzes each sample's object descriptions and step text using an LLM to infer what
read-service data tools are needed, then generates mock data for any that are missing.
Runs once per sample (80 LLM calls for 80 samples), no LNL runtime required.

```bash
# Preview what tools would be added (no writes)
python -m src.data.retrofit_mock_tools \
    -i outputs/my-run/samples.jsonl \
    --dry-run --model gpt-4o

# Patch samples.jsonl and workflows.jsonl in-place
python -m src.data.retrofit_mock_tools \
    -i outputs/my-run/samples.jsonl \
    --workflows outputs/my-run/workflows.jsonl \
    --model gpt-4o
```

### discover_mock_tools — dynamic discovery

Runs each sample's steps through the LNL runtime (with a no-op judge) and records
every `_data` tool call that falls through to `PassthroughExecutor` — i.e., was called
but not yet mocked. Generates mock data for any newly discovered tools and patches the file.

Run **after** `retrofit_mock_tools` to catch tools that static analysis missed.

```bash
# Preview discovered tools (no writes)
python -m src.data.discover_mock_tools \
    -i outputs/my-run/samples.jsonl \
    --dry-run --model gpt-4o

# Patch samples.jsonl and workflows.jsonl in-place
python -m src.data.discover_mock_tools \
    -i outputs/my-run/samples.jsonl \
    --workflows outputs/my-run/workflows.jsonl \
    --model gpt-4o
```

### Recommended workflow for a new run

```bash
# 1. Generate pipeline as normal
python -m src.data.pipeline -i data/zapier/raw/templates.yaml --target-dir outputs/my-run

# 2. Fill mock tool gaps (static pass)
python -m src.data.retrofit_mock_tools \
    -i outputs/my-run/samples.jsonl \
    --workflows outputs/my-run/workflows.jsonl \
    --model gpt-4o

# 3. Fill remaining gaps (dynamic pass)
python -m src.data.discover_mock_tools \
    -i outputs/my-run/samples.jsonl \
    --workflows outputs/my-run/workflows.jsonl \
    --model gpt-4o

# 4. Evaluate
python -m src.data.evaluate \
    -i outputs/my-run/samples.jsonl \
    --model gpt-4o --judge-model gpt-4o
```

### How uncovered tool calls are handled at eval time

Any `_data` tool call that still has no mock at evaluation time is caught by
`PassthroughExecutor`, which returns `{}` — a valid empty JSON response. The LLM-object
receives this and can handle the missing data gracefully rather than failing hard.
Non-data tool calls (action tools like `email.send`, `slack.post`) return
`"[mock] <tool> executed successfully."` and are captured as evidence for the judge.

---

## Examples

```bash
# Full pipeline with target folder
python -m src.data.pipeline -i data/zapier/raw/examples.yaml --target-dir outputs/my-run

# Continue existing run (stage 1 auto-skipped when workflows.jsonl exists)
python -m src.data.pipeline -i data/zapier/raw/examples.yaml --target-dir outputs/my-run

# Full pipeline without target folder (paths derived from input filename)
python -m src.data.generate_samples -i data/zapier/raw/examples.yaml --step-style object --workflows-per-template 3
python -m src.data.generate_test_cases -i outputs/data/zapier/examples_samples_object.jsonl

# Generate only temporal modification scenarios
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/workflows.jsonl --mod-type temporal

# Generate 3 scenarios per modification type
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/workflows.jsonl --scenario-count 3

# Generate scenarios with 2 modifications each (same type)
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/workflows.jsonl --mod-type temporal --mods-per-scenario 2

# Generate scenarios with 3 random/mixed modification types
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/workflows.jsonl --mod-type mixed --mods-per-scenario 3

# Force all modifications to be vague
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/workflows.jsonl --ambiguity vague

# Random ambiguity (default behavior)
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/workflows.jsonl --ambiguity random

# With Anthropic Sonnet
python -m src.data.generate_samples -i data/zapier/raw/examples.yaml --model claude-sonnet-4-5-20250929
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/workflows.jsonl --model claude-sonnet-4-5-20250929

# Resume after interruption (skips completed items)
python -m src.data.generate_samples -i data/zapier/raw/examples.yaml
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/workflows.jsonl

# Force regeneration
python -m src.data.generate_samples -i data/zapier/raw/examples.yaml --force
```

---

## LLM Module

The `src/data/llm/` module provides a clean abstraction for LLM calls:

```python
from src.data.llm import create_llm, user_message
from src.data.schema import Samples

llm = create_llm(provider="anthropic", model="claude-sonnet-4-5-20250929", temperature=0.7, seed=42)
result = llm.generate_structured(
    messages=[user_message("Your prompt here")],
    response_model=Samples,
)
```

## Dependencies

- `openai` - OpenAI API calls
- `anthropic` - Anthropic API calls
- `pydantic` - Structured output schemas
- `pyyaml` - YAML parsing
- `tqdm` - Progress display
- `python-dotenv` - Environment variable loading

## Environment Variables

- `OPENAI_API_KEY` - Required when using OpenAI provider
- `ANTHROPIC_API_KEY` - Required when using Anthropic provider
