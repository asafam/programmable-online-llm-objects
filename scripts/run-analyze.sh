#!/bin/bash
# Analyze evaluation result files and generate an HTML report + insights summary.
#
# Usage:
#   ./scripts/run-analyze.sh outputs/.../runs/test_cases_eval_*.jsonl
#   ./scripts/run-analyze.sh lnl.jsonl baseline.jsonl --label LNL,Baseline
#   ./scripts/run-analyze.sh outputs/.../eval.jsonl --no-html
#
# All extra arguments are passed through to analyze_results.py unchanged.
# Log is written to logs/analyze/<output_basename>.log (same name as the HTML report).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# ── Activate venv ─────────────────────────────────────────────────────────────
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# ── Run with temp log, then rename to match output file ───────────────────────
mkdir -p logs/analyze
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
TEMP_LOG="logs/analyze/.tmp_${TIMESTAMP}.log"

python -m src.data.analyze_results "$@" 2>&1 | tee "${TEMP_LOG}"

OUTPUT_FILE=$(grep "^Report: " "${TEMP_LOG}" | sed 's/Report: //' | tail -1)
if [ -n "${OUTPUT_FILE}" ]; then
    BASENAME=$(basename "${OUTPUT_FILE}" .html)
    FINAL_LOG="logs/analyze/${BASENAME}.log"
    mv "${TEMP_LOG}" "${FINAL_LOG}"
else
    FINAL_LOG="logs/analyze/analyze_${TIMESTAMP}.log"
    mv "${TEMP_LOG}" "${FINAL_LOG}"
fi
echo ""
echo "Log: ${FINAL_LOG}"
