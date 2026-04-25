# Evaluation Analysis

`analyze_results.py` reads one or more evaluation result JSONL files and produces an interactive HTML report with charts plus a textual insights summary.

## Running

```bash
# Single file (shell wrapper — activates venv, writes log to logs/analyze/)
./scripts/run-analyze.sh outputs/data/zapier/.../runs/test_cases_eval_*.jsonl

# Compare LNL runtime vs baseline side-by-side
./scripts/run-analyze.sh \
    outputs/.../runs/test_cases_eval_20260422.jsonl \
    outputs/.../runs/test_cases_baseline_20260422.jsonl \
    --label LNL,Baseline

# Insights only — no HTML
python -m src.data.analyze_results outputs/.../eval.jsonl --no-html
```

Output: `<input_stem>_analysis_<timestamp>.html` next to the input file.  
Log: `logs/analyze/<output_basename>.log`

## Charts

| Chart | What it shows |
|---|---|
| Pass rate distribution | Histogram of per-TC pass rates with mean marker |
| Pass rate by role | Steps / pre-mod / post-mod / irrelevant with error bars (std dev across runs) |
| Pass rate by modification type | temporal / contextual / exception / correction / expansion / removal |
| Per-TC pass rate | Sorted bar (worst→best) with hover labels; rangeslider for large datasets |
| Run consistency | Std dev per TC across runs — most inconsistent first (omitted for single-run files) |
| Token usage | Stacked bar: event input + output + judge input + output, top-N most expensive TCs |
| Latency distribution | Box plot per bundle |
| Chain depth vs pass rate | Pass rate binned by observed max peer-message hop depth per TC (0 / 1 / 2 / 3 / 4+) |
| Head-to-head comparison | Key metrics side-by-side (only shown when comparing two files) |

## Insights summary

The printed summary covers:

- **Overall** — mean pass rate, behavioral consistency label, inconclusive TC count
- **Role breakdown** — per-role pass rates; flags if post-mod is substantially below pre-mod
- **Chain depth** — pass rate by observed max hops per TC; flags if deeper chains degrade performance
- **Top failing TCs** — bottom-N by pass rate with mod type and name
- **Most expensive TCs** — total tokens (event + judge) per TC
- **Slowest events** — top-N by latency
- **Failure patterns** — regex-bucketed failure categories from judge reasoning text
- **Comparison delta** — delta in mean pass rate, post-mod pass rate, inconclusive TCs, and token cost ratio (multi-file only)

## CLI flags

| Flag | Default | Description |
|---|---|---|
| `RESULTS_JSONL` (positional, repeatable) | — | One or more result JSONL files. Two+ files enable comparison mode. |
| `--output`, `-o` | `<stem>_analysis_<ts>.html` | Output HTML path |
| `--top-n` | 10 | Items in ranked lists (failing TCs, expensive TCs, slow events) |
| `--label` | model name from RunConfig | Comma-separated display labels for each input file |
| `--no-html` | — | Skip HTML generation; print insights only |
| `--png-dir` | — | Also export each chart as a PNG (requires `kaleido`) |
