# Test Case Data Generator

Two-stage pipeline for generating test cases from Zapier automation templates using LLM-based generation.

## Pipeline Overview

```
Templates (YAML) → [generate_samples] → Samples (JSONL) → [generate_test_cases] → Test Cases (JSONL)
```

1. **Stage 1: Generate Samples** - Instantiate templates with concrete values
2. **Stage 2: Generate Test Cases** - Create scenarios with modifications and events

## Quick Start

```bash
# Stage 1: Generate samples from templates
python -m src.data.generate_samples \
    -i data/zapier/raw/templates.yaml \
    --samples-per-template 1

# Stage 2: Generate test cases from samples
python -m src.data.generate_test_cases \
    -i outputs/data/zapier/generated/samples.jsonl \
    --scenario-count 1
```

---

## Stage 1: Generate Samples

Instantiates raw templates with specific, realistic values.

### Usage

```bash
python -m src.data.generate_samples \
    --input data/zapier/raw/examples.yaml \
    --model gpt-4o \
    --samples-per-template 3
```

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--input`, `-i` | (required) | Path to raw templates YAML file |
| `--output`, `-o` | `outputs/data/zapier/generated/samples.jsonl` | Output JSONL path |
| `--prompt-template` | `config/prompts/data-gen/generate_samples.yaml` | Prompt template path |
| `--model`, `-m` | `gpt-4o` | Model name (provider auto-detected) |
| `--seed`, `-s` | None | Random seed for reproducibility |
| `--samples-per-template` | `1` | Samples to generate per template |
| `--temperature` | `0.7` | LLM temperature |
| `--force` | False | Regenerate all templates |
| `--limit`, `-n` | None | Process only first N templates |

### Output

```json
{"id": "it-helpdesk", "name": "IT Help Desk", "domain": "it-support", "source_type": "Zapier", "link": "https://...", "steps": ["When ticket arrives in #support-tickets...", ...]}
```

---

## Stage 2: Generate Test Cases

Creates test scenarios with modifications and events from samples.

### Usage

```bash
python -m src.data.generate_test_cases \
    --input outputs/data/zapier/generated/samples.jsonl \
    --model gpt-4o \
    --scenario-count 1
```

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--input`, `-i` | (required) | Path to samples JSONL (from Stage 1) |
| `--output`, `-o` | `<input_stem>__<mod-type>__<ambiguity>.jsonl` | Output JSONL path (derived from input, mod-type, and ambiguity) |
| `--prompt-template` | `config/prompts/data-gen/generate_test_cases.yaml` | Prompt template path |
| `--model`, `-m` | `gpt-4o` | Model name (provider auto-detected) |
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

## Examples

```bash
# Full pipeline with OpenAI (generates all 6 modification types)
python -m src.data.generate_samples -i data/zapier/raw/examples.yaml --samples-per-template 3
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/samples.jsonl

# Generate only temporal modification scenarios
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/samples.jsonl --mod-type temporal

# Generate 3 scenarios per modification type
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/samples.jsonl --scenario-count 3

# Generate scenarios with 2 modifications each (same type)
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/samples.jsonl --mod-type temporal --mods-per-scenario 2

# Generate scenarios with 3 random/mixed modification types
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/samples.jsonl --mod-type mixed --mods-per-scenario 3

# Force all modifications to be vague
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/samples.jsonl --ambiguity vague

# Random ambiguity (default behavior)
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/samples.jsonl --ambiguity random

# With Anthropic Sonnet
python -m src.data.generate_samples -i data/zapier/raw/examples.yaml --model claude-sonnet-4-5-20250929
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/samples.jsonl --model claude-sonnet-4-5-20250929

# Resume after interruption (skips completed items)
python -m src.data.generate_samples -i data/zapier/raw/examples.yaml
python -m src.data.generate_test_cases -i outputs/data/zapier/generated/samples.jsonl

# Force regeneration
python -m src.data.generate_samples -i data/zapier/raw/examples.yaml --force
```

---

## LLM Module

The `src/data/llm/` module provides a clean abstraction for LLM calls:

```python
from src.data.llm import create_llm, user_message
from src.data.schema import Samples

llm = create_llm(provider="openai", model="gpt-4o", temperature=0.7, seed=42)
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
