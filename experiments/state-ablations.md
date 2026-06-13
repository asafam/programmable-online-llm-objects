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

## Replan-on-branch diagnosis (2026-06-13, expenses A/B)

Q (user): does the planner (1) correctly identify branch nodes and (2) replan
successfully? Evidence from 04:49 expenses plans (replan ON, verbose):

1. **Branch node: identified, but DUPLICATED 3-4x.** SC001 policy plan emits
   s4,s5,s6,s7 — all kind=replan, all deps=[s3] (the window read), all asking
   the same "send consolidated finance email?" decision. Correct location,
   4 markers for 1 decision. Same SC003 (s4-s7), SC005 (s4,s5,s7,s9).
2. **Replan fires but duplication wrecks it.** SC003: s4,s5,s6 status=done →
   re-planned 3x for ONE decision, exhausting replan_max_per_trace=3 on
   duplicates, still failed. SC005: re-entries produced conflicting
   tell→expense-window continuations (s6,s8,s10) that FAILED. SC001 passed
   only because all 4 replan steps were skipped (standard path didn't need them).

Scores (expenses /12): replan-OFF(old stack) 9 | replan-ON(no fix) 4 |
replan-ON+inflight-fix 5 | replan-OFF(today's stack) PENDING.

ROOT CAUSE: defer-and-append model lets the planner emit N duplicate replan
markers for one branch; budget consumed by dupes; re-entries conflict.
DIRECTION: guarded-prune model (plan both branches ONCE, resolve guard with one
decision call, mark untaken branch skipped) makes duplication structurally
impossible. In-flight evaluator fix (4a07172) is orthogonal & kept.

## Expenses iteration loop (2026-06-13, single-example feedback)

| H | Hypothesis | Prediction | Result | Verdict |
|---|---|---|---|---|
| H-exp-1 | append_expense_row lacks an expense_id field → rows can't carry their REQ id → judge can't confirm "REQ-X added" (id-tracking cluster) | add expense_id field+behavior → ≥7/12, zero "generic row" verdicts | **9/12** (was 5/12); all "not confirmed as REQ-X" verdicts gone; IRR001 recovered | **CONFIRMED** |

Remaining 3 fails all in the THRESHOLD/digest cluster: SC004, PM003 (consolidated
email didn't fire at 3rd in-window), PM004 (category-group append missing). Same
7-day-window branch logic applicant needs. → H-exp-2.

## Applicant-tracker iteration (2026-06-13) — 0.15 → 0.69

Each probe shifted the failure to the next-deepest layer (textbook convergence):
| H | Change | Result | Lesson |
|---|---|---|---|
| H4 | optimistic+concrete application-window (transferred from expenses) | 0/13 (worse) | window mechanics OVERLOADED intake, crowding out the tracker write |
| H5 | collapse tracker write to 1 hop (intake writes directly) | 1/13; write now reliable, failure → "email not sent" | deep write chains fail for mini; collapse critical write to entry |
| H6 | intake sends standard email directly, write+email mandatory-first | 2/13; email fires, failure → "generic recipient" | same: collapse critical send to entry |
| H7 | expose posting→manager-email mapping to intake | **9/13** | ROOT CAUSE — no object could address the email; applicant was ALWAYS ~0 for this |

Reusable pattern (lifted expenses 0.33→0.75 and applicant 0.15→0.69):
1. write tools must carry the row's identity (expense_id);
2. one-tool-two-modes needs a both-modes description (email standard vs digest);
3. collapse the critical per-event conjunction (write + send) to the ENTRY object,
   1-hop, mandatory and FIRST — mini reliably completes ~2 actions/event, so depth kills it;
4. expose the reference data the workflow needs (manager emails);
5. optimistic + concrete custodian commits.
RESIDUAL (both samples): the threshold-digest (3rd-in-window fires consolidated) —
the genuine cross-event accumulation case. → H8.

## FINAL (2026-06-13 single-example feedback loop, ~$15 of $30 budget)

Two samples solved-by-pattern, gpt-5.4-mini:
- **expenses-tracker: 0.33 → 0.75** (H1 expense_id field, confirmed/committed)
- **applicant-tracker: 0.15 → ~0.62-0.69** (H5-H7: collapse write+email to entry, expose
  manager-email reference data; stable across confirm runs, base events 5/5)

H8 boundary finding: making the threshold custodian-count ROBUST (intake self-counts)
fixed the base threshold event but BROKE the post-mod events (the modification RETIRES
the quorum; heavy threshold logic doesn't cleanly retire). Reverted to H7's lighter touch.

RESIDUAL (both samples, the genuine mini floor): the threshold-digest event (3rd-in-window
fires consolidated email) — distributed cross-event state accumulation, the multi-agent tax
the dataset is designed to measure. Not closable without breaking modification-retirement.
