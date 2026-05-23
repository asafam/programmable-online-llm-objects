# Benchmark Quality Audit — 2026-05-23 Session

Concentrated effort to improve the quality of the 498-sample test-case benchmark
under `outputs/data/zapier/20260522_rev/` by detecting and fixing structural
defects: event-sequence paradoxes, modification-effect ambiguity, missing
observable outputs, and mock-data cheat-sheets that bypass policy inference.

## Starting state

Initial validation (baseline = `sample_event_validation_all.jsonl`):

| Verdict | Count | % |
|---|---|---|
| CLEAN | 216 | 43% |
| MILD_ISSUES | 212 | 43% |
| PARADOX | 14 | 3% |
| INCOMPLETE | 56 | 11% |

Issue codes (events flagged across the dataset):

| Code | Count |
|---|---|
| `causal_orphan` | 216 |
| `mod_effect_not_reflected` | 168 |
| `expect_incomplete` | 129 |
| `expect_null_invalid` | 68 |
| `redundant` | 48 |
| `mod_unsuppression_invalid` | 25 |
| `sequential_paradox` | 19 |
| `expect_leak` | 7 |

## Final state (v6 after Stage 2 regen)

| Verdict | Count | % |
|---|---|---|
| CLEAN | **382** | **77%** |
| MILD_ISSUES | 107 | 22% |
| PARADOX | 4 | <1% |
| INCOMPLETE | 5 | 1% |

Issue codes after all passes:

| Code | v1 | v6 | Reduction |
|---|---|---|---|
| `mod_effect_not_reflected` | 168 | 12 | **-93%** |
| `expect_null_invalid` | 68 | 1 | **-99%** |
| `expect_incomplete` | 129 | 15 | **-88%** |
| `sequential_paradox` | 19 | 4 | -79% |
| `mod_unsuppression_invalid` | 25 | 4 | -84% |
| `causal_orphan` | 216 | 133 | -38% |
| `redundant` | 48 | 51 | +6% |
| `expect_leak` | 7 | 6 | -14% |

Net: **+166 CLEAN samples**, PARADOX cut by 71%, INCOMPLETE by 91%.

---

## What we found

The benchmark had four distinct quality problems, each requiring a different fix:

### 1. Mock cheat-sheets bypassing policy inference

`org_directory_data` for HubSpot quote-approvals contained a pre-resolved
`approval_recipients` array literally naming Q-2048's approvers (Maria Chen,
Daniel Lopez) with their reasons. An agent could read this directly without
applying the workflow's policy ("AE's reporting chain + Rev Ops + Enterprise
Sales leadership"). Same pattern existed in:

- `slack_actions` (pre-recorded approver responses — leaks future external events)
- `escalation_recipients` (pre-resolved escalation chains)
- `approval_relationships` in `automate-employment-verification-letters`

### 2. Workflow base events losing structure / containing paradoxes

The HubSpot workflow (`deal-desk-manage-hubspot-quote-approvals-slack`) had a
5-event base sequence that included:
- A `sequential_paradox` (Maria approves in S003 after closing the case
  with "changes-requested" in S002, with no resubmission event in between)
- A `causal_orphan` (Daniel Lopez approves in S004 but no prior event sends
  an approval request to him)
- An `expect_leak` in S001 (mentions Maria's S002 decision in S001's expect)

### 3. Modification ambiguity (mod_effect_not_reflected)

168 post_mod events did not visibly differ from their baseline equivalents.
Three sub-modes:
- **Condition under-match**: event input doesn't fit the mod's qualifying
  condition; expect over- or under-applies the mod
- **Expect ignores mod**: input matches mod, but expect describes baseline
- **Mod over-applied**: input outside mod scope, expect treats mod as active

### 4. Input-level paradoxes (Stage 2 issue, not fixable via expect regen)

7 samples had event INPUTS that contradicted each other:
- canaries-employee-attrition (same employee, two different employee IDs)
- email-campaign-portal (timer expirations at impossible timestamps)
- facebook-content-calendar (post-edit/publish time mismatches)
- instagram-content-calendar (scheduled vs publish time mismatch)
- linkedin-conversion-tracking (lead "doesn't exist" after being created)
- offline-conversion-tracking (LinkedIn click ID inconsistency)
- + others

---

## Tools built

### `src/data/validate_workflow_events.py`

LLM-judge validator for **workflow base events** (the `events` list inside each
`Workflow` record where `role=="base"`). Six issue codes:
`sequential_paradox`, `causal_orphan`, `expect_leak`, `expect_incomplete`,
`expect_null_invalid`, `redundant`. Four sequence-level verdicts: `CLEAN`,
`MILD_ISSUES`, `PARADOX`, `INCOMPLETE`. Wired into the pipeline as **Stage 1e**.

```bash
python -m src.data.validate_workflow_events \
  --input outputs/.../workflows.jsonl \
  --judge-model gpt-5.4 --provider azure --workers 12
```

### `src/data/validate_sample_events.py`

Same six base codes + two mod-specific codes:
- `mod_effect_not_reflected` — post_mod expect doesn't visibly differ from baseline
- `mod_unsuppression_invalid` — `[suppressed by Mxxx]` annotation used incorrectly

Operates on the **full** Sample event sequence (base + pre_mod + post_mod +
irrelevant) plus modifications context. Used as the primary feedback loop for
the 6 iteration passes.

### `src/data/regen_workflow_events.py`

Surgical re-run of `_write_steps()` for chosen workflows in `workflows.jsonl`
WITHOUT going through Stage 2. Passes `workflow.tools` mock data into the
prompt so concrete instance values stay consistent.

### `src/data/check_mock_event_sync.py`

Cross-checks named entities in event text against the workflow's mock-tool
response data. Has known false-positive rate (event-payload entities, name
formatting variance) — useful as a candidate-surfacing tool rather than a
strict gate.

---

## Prompt changes

### `config/prompts/data-gen/write_steps.yaml`

- **Concrete instance values** (replaced an earlier "abstract template" rule
  that was inconsistent with the existing dataset convention). Steps must use
  concrete names/IDs/amounts pulled from the new `{MOCK_DATA}` section.
- **Sequential coherence** section: state compatibility, actor prerequisites,
  isolated expect scope.
- **Stop at next external-response boundary**: notifications are terminal
  outputs; the reply is a separate future Step.

### `config/prompts/data-gen/write_expectations.yaml`

Iterated 4 times across the session. Final rules (additive):

1. **CRITICAL ISOLATION RULE** — each event's expect uses only that event's input.
2. **Cross-event consistency** — same entity → same identifier; do not contradict
   state established by a prior event in the sequence.
3. **Name created artifacts and notified actors explicitly** — name the thread
   ID, case ID, person, etc. so downstream events have a clean reference.
4. **Mod scope check** — conditional scope + temporal scope (one-shot vs
   persistent vs time-bounded). Out-of-scope post_mod events get baseline behavior.
5. **Baseline-vs-modified differential** — write both candidate outputs mentally;
   if identical, the event can't test the mod.
6. **Defer-until sub-rule** — when an event fires AT or AFTER the deferral
   target time, the held action fires; tag `[fired-on-schedule by Mxxx]`.
7. **Visibility annotations** — `[suppressed by]`, `[rerouted by]`,
   `[adjusted by]`, `[added by]`, `[deferred by]`, `[fired-on-schedule by]`.
8. **Threshold rules** — when null `action` is valid (rare), when to use
   suppression vs adjusted/rerouted, when identical expects for same-trigger
   events are acceptable.

### `config/prompts/data-gen/validate_workflow_events.yaml` + `validate_sample_events.yaml`

Created from scratch. Define the six base issue codes and the two mod-specific
codes with concrete examples. Sequence-level verdict semantics.

---

## What we did to the data

Applied to `outputs/data/zapier/20260522_rev/`:

| Action | What |
|---|---|
| Cheat-sheet strip | Removed `approval_recipients`, `escalation_recipients`, `slack_actions` from HubSpot `org_directory_data`. Removed `approval_relationships` from employment-verification mock. |
| HubSpot base events | Rebuilt to 2-event sequence (S001 quote submission, S002 reminder) with concrete instance data preserved from the original (Q-2048, James Brown, Maria Chen, etc.) |
| Template fix | `data/zapier/raw/templates.yaml` step 3 of HubSpot: added "actionable Slack approval message" delivery (resolving the email-out/Slack-in inconsistency) |
| Sample base sync | All 6 HubSpot Samples synced to 2-event base sequence; expects refreshed |
| 5 expect-regen passes | `regen_event_expects.py` over non-CLEAN samples, picking up each prompt iteration |
| Stage 2 input regen | 20 stubborn PARADOX/INCOMPLETE samples — fresh modifications + events generated from scratch |
| Manual input patches | 4 events with input-level paradoxes (employee ID mismatch, timer timestamp, scheduled-time mismatch, missing LinkedIn ID) |

---

## Per-pass progression

| Pass | What changed | CLEAN | MILD | PARADOX | INCOMPLETE | Δ CLEAN |
|---|---|---|---|---|---|---|
| v1 | baseline | 216 | 212 | 14 | 56 | — |
| v2 | regen 209 samples after first prompt iterations | 311 | 166 | 11 | 10 | +95 |
| v3 | regen 187 + cross-event consistency rule | 340 | 133 | 13 | 12 | +29 |
| v4 | regen 158 + entity-naming + threshold rules | 367 | 110 | 14 | 7 | +27 |
| v5 | regen 131 + defer-until rule | 374 | 104 | 14 | 6 | +7 |
| **v6** | **Stage 2 input regen on 20 stubborn samples** | **382** | **107** | **4** | **5** | **+8** |

Diminishing returns on prompt iteration after v4. The Stage 2 input regen
(v6) was the biggest single win on the worst-case tail — PARADOX dropped from
14 to 4, INCOMPLETE from 6 to 5 — because input-level contradictions can't
be repaired by expect-side rewrites.

---

## Residual issues (9 samples = 1.8% of dataset)

The 4 PARADOX + 5 INCOMPLETE that survived even Stage 2 regen are genuine
hard cases — each has a distinct, narrow defect that would need per-sample
hand-patching:

- `ai-agent-marketing-campaign-tracker-temporal-TC001` — persistent vs
  one-shot ambiguity in the mod text itself
- `ai-voice-generator-contextual-TC001` — chain of causal orphans on
  Airtable review records
- `automate-team-meeting-signups-google-calendar-temporal-TC001` — E002
  contradicts M001's week-specific duplicate rule
- `deal-desk-manage-hubspot-quote-approvals-slack-correction-TC001` — E003
  references thread for Q-4102 before any prior event creates it
- `employee-directory-correction-TC001` — mod-effect coverage too narrow
- `employee-directory-exception-TC001` — E001 vs E004 contradict birthday dates
- `order-request-form-expansion-TC001` — mod-effect not adequately tested
- `out-of-office-plan-expansion-TC001` — E005 contradicts active M001 behavior
- `target-account-engagement-alert-rep-outreach-kit-contextual-TC001` —
  mod-effect not tested

These probably need targeted hand-patches or accept-as-known-issues.

---

## Lessons learned

1. **Prompt iteration has diminishing returns.** v1→v2 yielded +95 CLEAN.
   v5→v6 (the same kind of pass) yielded only +7. The LLM has a noise floor;
   each iteration trades 20-30 fixes for 10-15 regressions.

2. **Validator non-determinism is real.** Same input, same model, same temp
   (0.0) — Azure GPT-5.4 still flips borderline samples between runs.
   ~5% of CLEAN samples may flip on a re-validation.

3. **Stage 2 input regen >>> expect regen for input-level defects.**
   `sequential_paradox` dropped 24→4 in a single Stage 2 pass after 5 expect-
   regen passes couldn't move it. Where you regenerate matters.

4. **Cheat-sheets in mock data inflate apparent capability.** Pre-resolved
   `approval_recipients` made a "policy reasoning" test look like a JSON-
   lookup test. Strip these to get a real signal.

5. **Source mod ambiguity propagates downstream.** Mods like "this week's
   campaign analysis" are read as one-shot by some judges, persistent by
   others. A fraction of `mod_effect_not_reflected` cases are unfixable at
   the expect layer.

6. **Generic phrasing breaks causal chains.** When expect says "a thread is
   created" instead of "Slack thread TS-88421 is created in #quote-approvals",
   later events that reference TS-88421 look causally orphaned. Name everything
   you create.

---

## Files modified / created

### New

- `config/prompts/data-gen/validate_workflow_events.yaml`
- `config/prompts/data-gen/validate_sample_events.yaml`
- `src/data/validate_workflow_events.py`
- `src/data/validate_sample_events.py`
- `src/data/regen_workflow_events.py`
- `src/data/check_mock_event_sync.py`

### Modified

- `config/prompts/data-gen/write_expectations.yaml`
- `config/prompts/data-gen/write_steps.yaml`
- `data/zapier/raw/templates.yaml` (HubSpot step 3)
- `src/data/generate_workflows.py` (`_write_steps` accepts `tools`)
- `src/data/pipeline.py` (Stage 1e)
- `src/data/regen_event_expects.py` (symlink-aware path comparison)
- `src/data/schema.py` (`EventVerdict*` types, `SampleEventSequenceValidation`)
- `src/data/sync_sample_steps.py` (stale `Step.model_validate` fix)
- `src/data/validate_sample_modifications.py` (stale `.target` fix)

### Data

- `outputs/data/zapier/20260522_rev/workflows.jsonl` — HubSpot base events
  rebuilt; cheat-sheets stripped on 2 workflows
- `outputs/data/zapier/20260522_rev/workflows-mods.jsonl` — 209 expects
  regen'd + 20 samples Stage-2 regen'd
- `outputs/data/zapier/20260522_rev/workflows-mods.jsonl.pre_stage2_regen` —
  backup before Stage 2 input regen
- `outputs/data/zapier/20260522_rev/workflows.jsonl.bak_pre_event_regen` —
  backup before HubSpot workflow event regen
- `outputs/data/zapier/20260522_rev/sample_event_validation_{all,v2,v3,v4,v5,v6}.jsonl` —
  per-pass validator outputs

---

## Path B experiment — cross-event ID matching (REVERTED)

After v7, attempted to flip more `causal_orphan` cases by adding a rule:
*"Before finalizing each expect, scan inputs of LATER events for specific IDs
(thread IDs, case IDs, record IDs). If a later event references an ID that
THIS event's processing would create, USE that exact ID in the expect."*

Regenerated expects for the 75 samples with at least one `causal_orphan` flag.

Result (v8):

| Code | v7 | v8 (Path B) | Δ |
|---|---|---|---|
| `causal_orphan` | 131 | 107 | **-24 (-18%)** ✓ |
| `sequential_paradox` | 3 | 9 | **+6** ✗ |
| `expect_leak` | 4 | 8 | **+4** ✗ |
| `mod_effect_not_reflected` | 12 | 14 | +2 |
| CLEAN | 381 | 388 | +7 |
| PARADOX | 3 | 8 | +5 |
| INCOMPLETE | 4 | 5 | +1 |

The rule worked for its stated purpose (-18% on `causal_orphan`) but the LLM
**over-applied** — when scanning future events for IDs, it also pulled in
future decisions and outcomes, creating new leakage and paradox defects.

**Decision: revert.** +7 CLEAN at the cost of +6 worst-tail samples isn't a
net win when the worst-tail samples are *truly broken* (paradox) vs
*narrowly imperfect* (mild causal_orphan). Backup at
`workflows-mods.jsonl.before_pathB_044144` was restored.

A tighter version of the rule (strictly IDs only, no decisions / outcomes /
status fields) might avoid the regressions, but the experiment wasn't pursued
in this session.

---

## V7 confirmation run

A second validator pass on the v6 output (`sample_event_validation_v7_confirm.jsonl`)
confirmed the state is stable within validator noise:

| Metric | v6 | v7 confirm |
|---|---|---|
| CLEAN | 382 | 381 |
| MILD_ISSUES | 107 | 110 |
| PARADOX | 4 | 3 |
| INCOMPLETE | 5 | 4 |
| `mod_effect_not_reflected` | 12 | 12 |
| `causal_orphan` | 133 | 131 |
| `sequential_paradox` | 4 | 3 |
| `expect_null_invalid` | 1 | 0 |

Stable ~76-77% CLEAN, single-digit PARADOX/INCOMPLETE. Validator non-determinism
accounts for ±2% sample-level variance — final numbers should be read as a range.

---

## How to reproduce

```bash
# Initial validation
python -m src.data.validate_sample_events \
  --samples outputs/.../workflows-mods.jsonl \
  --provider azure --judge-model gpt-5.4 --workers 16 --no-fail \
  --output outputs/.../sample_event_validation_baseline.jsonl

# Identify non-CLEAN samples
python -c "
import json
with open('.../sample_event_validation_baseline.jsonl') as f:
    for line in f:
        v = json.loads(line)
        if v['sequence_verdict'] != 'CLEAN':
            print(v['sample_id'])
" > /tmp/non_clean_ids.txt

# Regen expects for those samples
python -m src.data.regen_event_expects \
  --samples outputs/.../workflows-mods.jsonl \
  --workflows outputs/.../workflows.jsonl \
  --provider azure --model gpt-5.4 --workers 12 \
  --filter $(cat /tmp/non_clean_ids.txt | tr '\n' ' ')

# Re-validate
# ... repeat the validate → regen loop until diminishing returns

# For stubborn PARADOX/INCOMPLETE, Stage 2 input regen
# (see /tmp/stage2_regen_v2.py in the session log — pattern: remove targets,
#  re-run generate_samples with --id <workflows> --mod-type <type>, no --force)
```
