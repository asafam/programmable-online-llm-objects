# Changelog

Release notes for the LNL runtime — newest first. Each push gets a version entry
written like app-store "What's New": plain language, what changed and why it matters.

## v0.3.0 — 2026-06-14 · Multi-agent reliability

- **Entry/coordinator objects now act on event triggers**, not just direct messages. Previously an object that received an external trigger as an event never planned, so it forwarded downstream only intermittently — now it plans reliably (e.g. the applicant-tracker hiring-manager email went from ~0–15% to ~85%).
- **Deterministic step dispatch is on by default.** The harness now dispatches every ready step of a plan, so a planned action can no longer be silently skipped by the model finishing early. Escape hatch: set `LNL_HARNESS_DISPATCH=0` to restore the old behavior.

## v0.2.0 — 2026-06-14 · Deterministic shared state (replaces "custodian")

- **New: shared state.** Every object can own a shared-state partition, read and written with two deterministic tools — `read_state` and `set_state`. The store is plain code with **no LLM** inside it. Use it for anything multiple requests touch at once: counters, quotas, running totals, rate-limits, or per-item registries. Guarded ops (`incr` with a max, `reserve`/`confirm` against a cap) keep those correct under concurrency.
- **One state model.** Private, shared, and a plan's working copy are now the same `State` type. Private state stays per-request and, on success, the whole object is copied over to the object's master state (copies for deterministic harness actions; deltas remain the LLM's interface).
- **No cost to objects that don't use it.** The shared-state tools and instructions appear only for objects that declare a `## Shared State` section — every other object's prompt and tool list is unchanged.
- **Cleanup.** The old "custodian" concept is gone (replaced by shared state); inter-object "report your state" now goes through the normal object cycle, and deterministic reads live in `read_state`. Internal renames: "custodian" → "shared-state owner"; executor prompt files renamed (`executor.yaml` is the default nested prompt, `executor_flat.yaml` the flat one).

## v0.1.0 — baseline

- Everything prior to this changelog (LNL runtime, data-generation pipeline, evaluation harness).
