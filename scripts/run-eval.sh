#!/bin/bash
# Run the LNL Runtime evaluation (evaluate.py).
#
# Usage:
#   ./scripts/run-eval.sh -i <samples.jsonl> [options]
#
# Examples:
#   ./scripts/run-eval.sh -i outputs/data/zapier/.../samples.jsonl
#   ./scripts/run-eval.sh -i outputs/data/zapier/.../samples.jsonl --model gpt-4o --runs 3
#   ./scripts/run-eval.sh -i outputs/data/zapier/.../samples.jsonl --tc 1 --verbose
#
# All extra arguments are passed through to evaluate.py unchanged.
# Log is written to logs/evaluate/<output_basename>.log (same name as the results file).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# ── Activate venv ─────────────────────────────────────────────────────────────
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# ── Run with temp log, then rename to match output file ───────────────────────
mkdir -p logs/evaluate
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
TEMP_LOG="logs/evaluate/.tmp_${TIMESTAMP}.log"

python -m src.data.evaluate "$@" 2>&1 | tee "${TEMP_LOG}"

OUTPUT_FILE=$(grep "^Output:" "${TEMP_LOG}" | sed 's/Output: //' | tail -1)
if [ -n "${OUTPUT_FILE}" ]; then
    BASENAME=$(basename "${OUTPUT_FILE}" .jsonl)
    FINAL_LOG="logs/evaluate/${BASENAME}.log"
    mv "${TEMP_LOG}" "${FINAL_LOG}"
else
    FINAL_LOG="logs/evaluate/eval_${TIMESTAMP}.log"
    mv "${TEMP_LOG}" "${FINAL_LOG}"
fi
echo ""
echo "Log: ${FINAL_LOG}"
