# State Ablations — LNL(mini) on state-scenario samples

Goal: lift LNL (gpt-5.4-mini) on the v34 state-scenario dataset toward/past the
single-agent baseline, by ablating the design variables that govern distributed
state. Constraint: **gpt-5.4-mini only** (full 5.4 was diagnostic-only).

Probe TC (since 2026-06-13): **`expenses-tracker`** — 7 events, ~5-8 min/probe,
and LNL's worst relative gap vs baseline (0.33 vs 0.62): fast loops aimed at the
most informative deficit. Secondary: `inventory` (0.40 vs 0.60). Round-robin
(31 events, 20-45 min) is reserved for MILESTONE confirmations only — it
burned too much wall-clock per data point as the default probe.

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

## Hypothesis register

Process rule (2026-06-13): NO run launches without a row here — hypothesis,
prediction, single variable, then result and verdict. Confounded results say so.

| ID | Hypothesis | Prediction | Test | Result | Verdict |
|---|---|---|---|---|---|
| H1 | Versioned custodian commits (stale-reject, bootstrap-reject) kill waves via rejection loops | optimistic commits raise round-robin score | P1 (A alone, sync) | run killed (port race) | **untested in isolation** — folded into package |
| H2 | Async dispatch (design-faithful) is not worse than sync, once measured | package ≥ sync band 5–8/31 | P2 round-robin | 6/31, coherence 56% (best), WITH storm active | supported (not worse); exposed re-fire storm |
| H3 | Log-shaped state drives cost + late-event errors | probe cost drops materially | P2 vs clock series | $3.43 vs $6.5–7 | **supported for cost**; accuracy effect unattributed |
| H4 | Re-fire storm killed 9/31 P2 waves; in-flight guard recovers them | round-robin +3–8 events vs P2's 6/31 | P3 round-robin (guard) | stopped at 45min/$4.98 (uneconomical; config superseded by leaf path + tool scoping mid-run) | **SUPERSEDED** — guard effect will be read from the next round-robin milestone on the full stack |
| H5 | Custodian ceremony (planner+evaluator per atomic turn) wastes 3–6× latency/cost | latency/cost drop ≥3×, score unchanged | P4 expenses (leaf path) | 3:25/$0.50 (≥3× faster/cheaper ✓) but 1/12 score | **CONFOUNDED** — probe also first to isolate expenses; freelancing observed; score drop not attributable to leaf path (no control) |
| H6 | Unscoped tools let entry services freelance write-sinks' jobs (observed: append_expense_row(status=submitted) from the entry, bypassing the policy's "pending") | with skills-scoped tools (0ea200a): expenses ≥4/12 AND zero append calls by the entry object | P5 expenses | 2/12; mechanism check: entry IS the sink (legit owner) — no freelancing to block | **FALSIFIED**; real defect = sample design |

| H7 | Tracker behavior's "append with current status" is ambiguous → model writes "submitted"; workflow semantics define new expenses as Pending | with behavior stating Pending-on-append + explicit forward-to-policy: expenses ≥7/12, zero "submitted rather than pending" verdicts | P6 expenses | **8/12** (was 2/12); zero "submitted vs pending" verdicts; now ABOVE baseline 0.62 | **SUPPORTED** — residue is the threshold/email chain (SC004-class), registered as H8 |

| H8 | The remaining expenses failures are the policy→window→email chain at the threshold (3rd in-window occurrence): the consolidated email doesn't fire or leaves no evidence | TBD after reading SC004's transcript — hypothesis to be sharpened BEFORE the next probe | — | — | registered, not yet sharpened |

| H9 | Read/write services (skills, no peers) still pay the planner+evaluator ceremony — the asks policies wait on (forms-pdf-extraction, inventory-record-store: 8s unanswered) are slow for the same reason custodians were | widening fast path to ALL peerless objects: inventory probe — suppressed-ask ages drop to ≤4s typical, score not worse than its 0.40 reference | P7 inventory | — | pending |

Lesson recorded: H5/H6 were stacked without a control — P4 cannot separate
leaf-path effects from pre-existing freelancing. Don't repeat.

## Run ladder (probe = round-robin unless noted)

| # | Config (A/B/C/...) | Score | Coherence | Cost | Run file | Verdict |
|---|---|---|---|---|---|---|
| clock2 | none (pre-fixes) | 8/31 | 1/21 | $7.02 | `eval_rr_clock2.jsonl` | all-Maya; counts keyed by timestamp |
| clock3 | clock fix | 5/31 | 9/22 | $6.49 | `eval_rr_clock3.jsonl` | replan stalls + peer-as-tool fake successes (since fixed) |
| P1 | **A** (sync, log-state) | KILLED (port race + quota contention) | — | — | — | superseded; decompose later if needed |
| P2 | **A+B+C** (new defaults) | **6/31** | **9/16 (56%)** | $3.43 | `eval_rr_async_facts.jsonl` | in sync band DESPITE re-fire storm; 9 dead waves = storm cost = headroom; first_div SC002→SC004; cost halved vs clock series → build the in-flight guard |
| P3 | A+B+C+**D** | planned | — | — | — | only if P2 leaves a gap |
| P4 | A+B+C+D+**E** | planned | — | — | — | last protocol simplification |
| Full | best config, all 10 TCs, `--runs 3` | planned | — | — | — | the headline vs baseline 0.468 |

Coherence = assignments matching a rotation seeded by the system's OWN history
(`python -m src.data.coherence -r <results.jsonl>`); separates "shifted but
self-consistent" from incoherent.

## Findings log

- **B (async) has a re-fire storm** (found 2026-06-13 02:15, both live runs):
  each tool/peer REPLY starts a fresh turn and the object re-dispatches plan
  steps still awaiting their own replies (policy→lead-desk ×7 in P2;
  engineering-intake → all peers until depth limit). Chain-depth=20 drops the
  excess — protective but lossy. Async needs an IN-FLIGHT GUARD (mark step
  dispatched-awaiting-reply; no re-dispatch until correlated reply/timeout)
  before it can be judged fairly. If P2 ≥ sync history even with the storm,
  async+guard is strictly better; if it craters, revert default to sync until
  the guard lands.

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
- 2026-06-13: probe TC switched round-robin → expenses-tracker (iteration
  velocity; round-robin only for milestones).
- 2026-06-13: state contract — facts/aggregates only; audit trails belong to
  the external systems (tool-call logs), never object state.
