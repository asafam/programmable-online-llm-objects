# object.yaml Prompt Ablation Log

Model: `gpt-5.4-mini` via Azure  
Dataset: `data/zapier/test_cases.jsonl`  
Flags: `--runs 1 --steps-only`  
Date: 2026-05-09

Each row is a single change tested on top of the previous **accepted** baseline.
Delta is relative to the accepted baseline at the time of the test.

---

## Baseline recovery

The prompt was restructured in commit `7f4dcaa` (Operating Principles first,
identity/definition after `---`). This caused a large regression. We restored
the `e9df936` version (identity first) and confirmed recovery before ablations.

| Prompt state | Pass rate | Notes |
|---|---|---|
| `7f4dcaa` restructured prompt | 0.540 | Operating Principles first — regressed |
| `e9df936` restored (identity first) | 0.738 | Baseline for ablations |

---

## Section ordering experiments

Goal: move dynamic sections to the end for prompt caching, without hurting quality.

| Change | Pass rate | Delta | Decision |
|---|---|---|---|
| Active Plan → end (State stays) | 0.695 | -4pt | ❌ reverted |
| Current State → end (Plan stays) | 0.730 | -1pt | ✅ neutral |
| Both Active Plan + Current State → end | 0.739 | ~0pt | ✅ accepted |
| Operating Principles first, definition + state at end | 0.544 | -19pt | ❌ reverted |

**Finding:** Identity + definition must stay at the top — moving them after
Operating Principles caused a 19pt collapse. Moving only the dynamic sections
(active_plan, current_state) to the end is free — neutral quality with better
caching. Committed as `7384954`.

**Caching implication:** Since identity/definition are object-specific, the
cache breaks per-object regardless. The win from state/plan at the end is
within a single object's conversation lifetime.

---

## Failure mode fixes

Failure modes identified from analysis of n=34 failures (pass rate 0.726 at time of analysis):

- **A** Mid-chain coordination break — router produces empty state, chain dies
- **B** Partial fan-out — router dispatches to some peers but not all  
- **C** Incomplete payload — message arrives but missing required fields
- **D** Partial loop — only some items in a batch processed
- **E** Scope refusal — object decides "not my job" (definition problem, not prompt)

Baseline for this phase: `0.739` (post caching restructure, commit `7384954`).

| Change | Pass rate | Delta | Decision |
|---|---|---|---|
| Routing + multi-item paragraphs (combined) | 0.678 | -6pt | ❌ reverted |
| **Lightweight dispatch rule (A)** | **0.743** | **+0.4pt** | **✅ accepted** |
| + Multi-item rule on top of dispatch (D) | 0.708 | -3.5pt | ❌ reverted |
| Fan-out: re-read Behavior before messaging (B) | 0.698 | -4.5pt | ❌ reverted |
| Payload copy-from-input rule (C) | 0.730 | -1.3pt | ❌ reverted (noise, existing language sufficient) |

### Dispatch rule (A) — accepted, commit `e3bd922`

Added after "Domain actions require a state_update":

> **Dispatching to peers is itself a domain action.** If you received a
> payload and forwarded it to one or more peers, record that in your state
> — the schema is yours to decide. Empty state after a dispatch means there
> is no evidence the chain continued.

### Multi-item rule (D) — rejected

> **Multi-item events require per-item processing.** When an incoming event
> contains N distinct items (e.g. two attachments, three contacts, five
> records), handle each item independently — one state entry and one
> outgoing message per item per peer. Do not collapse multiple items into
> a single write.

Hypothesis for rejection: most objects process single-item events; the rule
nudges them to over-generate entries or messages.

### Fan-out behavior re-read (B) — rejected

Added "Before composing messages, re-read your Behavior definition and enumerate
every peer it names for this event type — use that list, not recall."

Hypothesis for rejection: caused non-fan-out objects to over-examine their peer
list and send spurious messages.

### Payload copy-from-input rule (C) — rejected

Added to the "When forwarding an entity record downstream" bullet:

> Copy field values directly from the incoming payload — do not reconstruct
> or paraphrase from memory. If a field (URL, ID, timestamp, link) was present
> in the input, it must appear in the outgoing message.

Result: 0.730 (-1.3pt). Within noise but slightly negative. Reverted —
the existing forwarding language is already comprehensive enough.

---

## Confirmation run

After reverting C, ran the prompt as-is (caching restructure + dispatch rule only).
Result: **0.779** — new high watermark, above the historical 0.776 reference.

Current accepted prompt: identity → definition → operating_principles (with dispatch
rule) → tools → response_format → active_plan → current_state.

---

## General pattern

Additions that make objects *more thorough* tend to backfire — they over-activate
objects that were already behaving correctly, causing net regression. The only
safe additions so far are lightweight, narrowly-scoped rules that don't apply to
the common case.
