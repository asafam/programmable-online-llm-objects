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

---

## Plot Scripts

Static matplotlib plots saved as PNGs. Run after experiments complete.

---

### `scripts/plot_experiments.py` — Concurrency × modifications grid

Reads `exp_{lnl|baseline}_{N}mod_conc{C}.jsonl` files and plots pass rate, elapsed time, and token usage across concurrency levels, with one line per paradigm × mod-count combination.

```bash
python scripts/plot_experiments.py outputs/data/zapier/runs/experiments/<exp_dir>
```

Output → `<exp_dir>/plots/`:
- `concurrency_x_modifications_passrate.png` — 2×3 panel: mean / steps / mod / pre-mod / post-mod / irrelevant pass rate
- `concurrency_x_modifications_elapsed.png` — mean and P90 elapsed time per TC
- `concurrency_x_modifications_tokens.png` — mean agent and judge token usage per event

If results contain rejudge entries, one additional pass-rate plot is emitted per rejudge model.

---

### `scripts/plot_concurrency.py` — Concurrent-events pass rate by paradigm

Reads `exp_{lnl|baseline}_{N}mod_conc{C}[_{single|multi}].jsonl` files and plots three lines: **Ours (LNL)**, **OpenClaw (single-agent)**, **OpenClaw (multi-agent)**.

```bash
# Default dir: outputs/data/zapier/runs/experiments/concurrency
python scripts/plot_concurrency.py

# Or specify explicitly:
python scripts/plot_concurrency.py outputs/data/zapier/runs/experiments/concurrency
```

Output → `<exp_dir>/plots/concurrency_passrate.png` — 2×3 panel: same metrics as above.

Use `--metric` to generate a single panel instead of the full grid:
```bash
python scripts/plot_concurrency.py --metric post_mod
# choices: mean, steps, mod, pre_mod, post_mod, irrelevant
# saves: concurrency_passrate_post_mod.png
```

File naming convention for inputs:
```
exp_lnl_1mod_conc8.jsonl              # LNL at concurrency 8
exp_baseline_1mod_conc8_single.jsonl  # OpenClaw single-agent at concurrency 8
exp_baseline_1mod_conc8_multi.jsonl   # OpenClaw multi-agent at concurrency 8
```

---

### `scripts/plot_state_probes.py` — State-probe accuracy vs event depth

Plots probe pass rate and token cost as a function of event depth, comparing LNL vs OpenClaw baseline on state-probe test cases.

```bash
python scripts/plot_state_probes.py \
    outputs/.../lnl_probes.jsonl \
    outputs/.../baseline_probes.jsonl \
    [plots_dir] \
    [--tcs data/zapier/test_cases_state_probes.jsonl]
```

`--tcs` is optional but enables the conditioned accuracy panel (probes counted only when their `depends_on` events passed).

Output PNGs (in `plots_dir` or next to the LNL results file):
- `probe_accuracy_vs_depth.png` — raw post-mod pass rate by depth
- `probe_conditioned_accuracy_vs_depth.png` — conditioned accuracy (requires `--tcs`)
- `tokens_vs_depth.png` — agent input tokens per event by depth
- `elapsed_vs_depth.png` — mean elapsed time per TC by depth

Use `--chart` to generate only one of the above:
```bash
python scripts/plot_state_probes.py lnl.jsonl --chart accuracy
# choices: accuracy, conditioned, tokens, elapsed
```

---

### `scripts/plot_event_trace.py` — Transaction trace viewer

Prints an ASCII cascade of a single event's multi-agent message chain, plus a per-sender duration breakdown. Useful for debugging hop latency or verifying agent-to-agent routing.

```bash
# Pick the first multi-hop event automatically:
python scripts/plot_event_trace.py --results outputs/.../eval.jsonl

# Target a specific TC and event:
python scripts/plot_event_trace.py \
    --results outputs/.../eval.jsonl \
    --tc-id my-sample-TC001 \
    --event-id E003
```

Output is printed to stdout (no PNG saved).
