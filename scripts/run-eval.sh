#!/bin/bash
# Run the LNL Runtime evaluation (evaluate.py).
#
# Usage:
#   ./scripts/run-eval.sh -i <workflows-mods.jsonl> [options]
#
# Examples:
#   ./scripts/run-eval.sh -i outputs/data/zapier/.../workflows-mods.jsonl
#   ./scripts/run-eval.sh -i outputs/data/zapier/.../workflows-mods.jsonl --model gpt-4o --runs 3
#   ./scripts/run-eval.sh -i outputs/data/zapier/.../workflows-mods.jsonl --tc 1 --verbose
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
# Never trust PATH for the interpreter: a stale activate (venv created under a
# renamed repo dir) silently fell through to miniconda python3.9 — evals ran on
# the wrong interpreter + site-packages for a full day before anyone noticed.
PYBIN="${REPO_ROOT}/.venv/bin/python"
[ -x "$PYBIN" ] || PYBIN="python"

# ── Run with temp log, then rename to match output file ───────────────────────
mkdir -p logs/evaluate
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
TEMP_LOG="logs/evaluate/.tmp_${TIMESTAMP}.log"

"$PYBIN" -m src.data.evaluate "$@" 2>&1 | tee "${TEMP_LOG}"

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

# ── Non-determinism report (only meaningful with --runs ≥2) ───────────────────
if [ -n "${OUTPUT_FILE}" ] && [ -f "${OUTPUT_FILE}" ]; then
    python scripts/measure_nondeterminism.py "${OUTPUT_FILE}"
fi
