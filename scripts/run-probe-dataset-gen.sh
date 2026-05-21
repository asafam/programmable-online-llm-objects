#!/usr/bin/env bash
# Generate probe-dataset test cases (probe-first, 4-stage).
# Activates the project venv and runs generate_probe_dataset_tcs.py.
#
# Usage:
#   ./scripts/run-probe-dataset-gen.sh -i outputs/data/zapier/<run>/workflows.jsonl [options]
#
# Common options (passed through to the generator):
#   --depths 10 20 30 50    event depths (default: 10 20 30 50)
#   --seeds 3               seeds per cell (default: 3)
#   --model gpt-5.4-mini    LLM model (default: gpt-5.4-mini)
#   --workers 4             parallel workers (default: 1)
#   --output path.jsonl     override output path
#   --id <sample_id>        filter to specific sample IDs (repeatable)
#
# Example:
#   ./scripts/run-probe-dataset-gen.sh \
#       -i outputs/data/zapier/20260411_zapier_clean/workflows.jsonl \
#       --depths 10 20 --seeds 3 --model gpt-5.4-mini --workers 4

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"

if [ -f "$VENV/bin/activate" ]; then
    source "$VENV/bin/activate"
else
    echo "ERROR: venv not found at $VENV" >&2
    exit 1
fi

cd "$REPO_ROOT"
python -m src.data.generate_probe_dataset_tcs "$@"
