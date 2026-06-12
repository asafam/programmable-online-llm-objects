# State Ablations — LNL(mini) on state-scenario samples

Goal: lift LNL (gpt-5.4-mini) on the v34 state-scenario dataset toward/past the
single-agent baseline, by ablating the design variables that govern distributed
state. Constraint: **gpt-5.4-mini only** (full 5.4 was diagnostic-only).

Probe TC: `round-robin-lead-assignment` (31 events; hardest state chain; the
sample a single gpt-5.4 agent solved 31/31, so every expect is reachable).

## Reference points

| Run | Config | Score | Cost | Notes |
|---|---|---|---|---|
| baseline-single (mini), full set | OpenClaw single agent, all harness fixes | **0.468** (9 TCs, 1 infra) | $2.33 | the bar to beat; ±ME 0.28 |
| baseline-single (mini), round-robin | takes 11–13 | **0/31** | ~$0.2 | mini can't drive the workflow from prose |
| baseline-single (gpt-5.4), round-robin | take 17 | **31/31** | $1.39 | validity certificate: task fully solvable; NOT a deployment option |
| LNL (mini), full set 2026-06-12 | sync, versioned, log-state | **0.42** | ~$17 | leave 0.57, deal-desk 0.65 |
| LNL (mini), full set 2026-06-13 early | sync, versioned, log-state (+runtime fixes) | ~0.40 partial (9/10: leave **1.0**, deal-desk 0.82, applicant 0.0, brand infra) | ~$22 | filter killed brand; applicant chronic 0 |

## Ablation variables

| Var | Off (historical) | On | Landed |
|---|---|---|---|
| A. Commit protocol | versioned + stale-reject + bootstrap-reject | **optimistic: apply immediately, last-write-wins, never reject** | `47d312c` (prompt contract + v34 round-robin) |
| B. Tool dispatch | `sync` (inline tool loop in one LLM call) | **`async`** (one-shot action; tool results return as REPLY messages — design-faithful actor semantics) | `3e9936f` (eval default flipped) |
| C. State shape | log-shaped ("state becomes the audit trail") | **facts + aggregates, replace in place, no histories** | `ad795e3` (bind contract + object.yaml + 33 v34 fields) |
| D. JSON custodian state | NL prose state | compact JSON for custodians (`object_compact_state.yaml` exists) | not started |
| E. Read-and-commit collapse | separate read turn + commit turn | single custodian exchange | not started |
| F. Sink/window tuning | — | (mostly subsumed by C) | — |

## Run ladder (probe = round-robin unless noted)

| # | Config (A/B/C/...) | Score | Coherence | Cost | Run file | Verdict |
|---|---|---|---|---|---|---|
| clock2 | none (pre-fixes) | 8/31 | 1/21 | $7.02 | `eval_rr_clock2.jsonl` | all-Maya; counts keyed by timestamp |
| clock3 | clock fix | 5/31 | 9/22 | $6.49 | `eval_rr_clock3.jsonl` | replan stalls + peer-as-tool fake successes (since fixed) |
| P1 | **A** (sync, log-state) | RUNNING | — | — | `eval_rr_optimistic.jsonl` | isolates optimistic commits vs clock series |
| P2 | **A+B+C** (new defaults) | queued | — | — | `eval_rr_async_facts.jsonl` | the landed-package measurement |
| P3 | A+B+C+**D** | planned | — | — | — | only if P2 leaves a gap |
| P4 | A+B+C+D+**E** | planned | — | — | — | last protocol simplification |
| Full | best config, all 10 TCs, `--runs 3` | planned | — | — | — | the headline vs baseline 0.468 |

Coherence = assignments matching a rotation seeded by the system's OWN history
(`python -m src.data.coherence -r <results.jsonl>`); separates "shifted but
self-consistent" from incoherent.

## Standing measurement rules

- One variable per probe when feasible; package-measure (P2) only because the
  defaults already flipped together — decompose post-hoc only if the magnitude
  warrants.
- Fresh `-o` filename per run (the evaluator resumes/skips on existing outputs).
- Health-burst the Azure path before paid runs (8 concurrent pings — episodes
  of connection blackholing recur; fail-fast clients + watchdog now installed).
- Probes ~$1.5–7 each; full runs ~$2 (baseline) / ~$20 (LNL). Budget cap $300,
  trend-gated. Spent so far: ~$35 across both days.

## Known blockers, not part of the ablation

- **Content-filter samples**: brand (invalid_prompt, sticky) and engineering
  (Azure jailbreak flag) die to filters, not failures — reword trigger language
  before the next full run; until then full-set means are over 8 real samples.
- **applicant-tracker chronic 0.0** — never passed in any run; needs a
  transcript-level diagnosis (suspect wiring/expect mismatch like leave had).
- Baseline timed-out TC (round-robin at 900s) — rerun on same output retries.

## Decision log

- 2026-06-13: gpt-5.4 ruled out as agent model (cost); all comparisons mini-vs-mini.
- 2026-06-13: optimistic commits made the DEFAULT custodian contract (serial
  dispatch + quiesce make versioned handshakes cost more than they protect).
- 2026-06-13: `--tool-dispatch` default flipped to async — all historical LNL
  numbers were measured in sync mode and undersell the design if async wins.
- 2026-06-13: state contract — facts/aggregates only; audit trails belong to
  the external systems (tool-call logs), never object state.
