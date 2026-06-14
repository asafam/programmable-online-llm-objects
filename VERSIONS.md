# Versions

A running log of what each push to `main` did — newest first, one entry per commit.

Format: `## <short-hash> — <YYYY-MM-DD> — <title>` followed by a short bullet list of what the push changed.

## 836d7e5 — 2026-06-14 — Replace custodian with deterministic per-object shared state

- New `State` class (`src/lnl/state.py`): one class backing private, shared, and plan-dirty state — a memory backend behind a lock (`read`/`write`/`apply`/`derive`/`clone`).
- Per-object **shared state** with two built-in tools, `read_state` / `set_state` (`src/lnl/shared_state.py`) — deterministic, no LLM in the store; the `set_state` schema matches the configured backend dialect (flat key-based / nested path-based).
- Private state is now a `State`; shared writes are guarded-op atomic on a live store. The shared-state prompt block + tools are gated to objects that declare a `## Shared State` section — base prompt and tool list are byte-identical otherwise.
- `plan.state` is a JSON-object copy of master, committed by whole-object **copy-over** on success (delta replay / `accumulated_deltas` removed) — copies for deterministic harness actions, deltas for the LLM.
- Removed the deterministic custodian-read fast path; inter-object reads now go through the full LLM cycle (deterministic reads live in `read_state`).
- Renamed "custodian" → "shared-state owner" across data-gen + docs; renamed `executor_nested.yaml` → `executor.yaml` (the default) and the old flat `executor.yaml` → `executor_flat.yaml`.
- Tests: `tests/test_shared_state.py` (store/registry/tools + sync/async integration); `test_custodian_validation.py` → `test_shared_state_validation.py`.
