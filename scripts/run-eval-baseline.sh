#!/bin/bash
# Run the OpenClaw Baseline evaluation (evaluate_baseline.py).
#
# Usage:
#   ./scripts/run-eval-baseline.sh -i <workflows-mods.jsonl> [options]
#
# Examples:
#   ./scripts/run-eval-baseline.sh -i outputs/data/zapier/.../workflows-mods.jsonl
#   ./scripts/run-eval-baseline.sh -i outputs/data/zapier/.../workflows-mods.jsonl --model gpt-4o --runs 3
#   ./scripts/run-eval-baseline.sh -i outputs/data/zapier/.../workflows-mods.jsonl --pool docker/worker-pool-8.yaml --model gpt-4o
#   ./scripts/run-eval-baseline.sh -i outputs/data/zapier/.../workflows-mods.jsonl --tc 11 --verbose
#
# Parallel single + multi-agent runs (two terminal windows):
#   # Terminal 1 — single-agent on dedicated workers
#   ./docker/start-pool.sh --type single --workers 8
#   ./scripts/run-eval-baseline.sh -i <input.jsonl> --pool docker/worker-pool-single-8.yaml --single-agent -o <out_single.jsonl>
#
#   # Terminal 2 — multi-agent on separate dedicated workers
#   ./docker/start-pool.sh --type multi --workers 8
#   ./scripts/run-eval-baseline.sh -i <input.jsonl> --pool docker/worker-pool-multi-8.yaml -o <out_multi.jsonl>
#
# All extra arguments are passed through to evaluate_baseline.py unchanged.
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

"$PYBIN" -m src.data.evaluate_baseline "$@" 2>&1 | tee "${TEMP_LOG}"

OUTPUT_FILE=$(grep "^Complete\. Output:" "${TEMP_LOG}" | sed 's/Complete\. Output: //' | tail -1)
if [ -n "${OUTPUT_FILE}" ]; then
    BASENAME=$(basename "${OUTPUT_FILE}" .jsonl)
    FINAL_LOG="logs/evaluate/${BASENAME}.log"
    mv "${TEMP_LOG}" "${FINAL_LOG}"
else
    FINAL_LOG="logs/evaluate/eval_baseline_${TIMESTAMP}.log"
    mv "${TEMP_LOG}" "${FINAL_LOG}"
fi
echo ""
echo "Log: ${FINAL_LOG}"

# ── Non-determinism report (only meaningful with --runs ≥2) ───────────────────
if [ -n "${OUTPUT_FILE}" ] && [ -f "${OUTPUT_FILE}" ]; then
    python scripts/measure_nondeterminism.py "${OUTPUT_FILE}"
fi
