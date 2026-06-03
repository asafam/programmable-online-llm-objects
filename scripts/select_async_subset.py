"""Select a tool-heavy, multi-hop subset from the 20260420 clean dataset
for the sync-vs-async ablation. Outputs:

  outputs/data/zapier/async_subset/eval30.jsonl   — 30 TCs (the ablation set)
  outputs/data/zapier/async_subset/holdout10.jsonl — 10 TCs (collateral check)

Criteria (per docs/ABLATIONS.md §Plan/3):
  * len(tools)   >= 2  — async dispatch needs ≥2 tools to expose batch/ordering
  * len(objects) >= 5  — multi-hop graph so cascades have continuation turns
  * len(steps)   >= 2  — multi-turn workflows

Stratification: 2 TCs per sample_id (workflow family) to avoid duplicating
workflow shapes; within a family, pick 2 distinct mod_types deterministically
via random.seed(42).
"""
from __future__ import annotations

import json
import random
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.data.schema import Sample

SRC = REPO_ROOT / "outputs/data/zapier/20260420_zapier_clean/test_cases.jsonl"
OUT_DIR = REPO_ROOT / "outputs/data/zapier/async_subset"
EVAL_PATH = OUT_DIR / "eval30.jsonl"
HOLDOUT_PATH = OUT_DIR / "holdout10.jsonl"

EVAL_SIZE = 30
HOLDOUT_SIZE = 10
PER_FAMILY = 2  # mod-type variants per workflow family
SEED = 42


def main() -> int:
    if not SRC.exists():
        print(f"missing source: {SRC}", file=sys.stderr)
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    by_family: dict[str, list[tuple[Sample, str]]] = defaultdict(list)
    for raw in SRC.open():
        record = json.loads(raw)
        try:
            s = Sample.model_validate(record)
        except Exception as exc:
            print(f"skip unparseable: {record.get('id')} ({exc})", file=sys.stderr)
            continue
        if len(s.tools) < 2 or len(s.objects) < 5 or len(s.steps) < 2:
            continue
        by_family[s.sample_id].append((s, raw))

    rng = random.Random(SEED)
    picked: list[tuple[str, str]] = []
    for sid in sorted(by_family):
        candidates = by_family[sid]
        n_pick = min(PER_FAMILY, len(candidates))
        for s, raw in rng.sample(candidates, n_pick):
            picked.append((s.id, raw))

    if len(picked) < EVAL_SIZE + HOLDOUT_SIZE:
        print(
            f"only {len(picked)} TCs meet criteria across "
            f"{len(by_family)} families (need ≥ {EVAL_SIZE + HOLDOUT_SIZE})",
            file=sys.stderr,
        )
        return 2

    rng.shuffle(picked)
    eval_rows = picked[:EVAL_SIZE]
    hold_rows = picked[EVAL_SIZE : EVAL_SIZE + HOLDOUT_SIZE]

    EVAL_PATH.write_text("".join(raw for _, raw in eval_rows))
    HOLDOUT_PATH.write_text("".join(raw for _, raw in hold_rows))

    print(f"wrote {len(eval_rows)} → {EVAL_PATH}")
    print(f"wrote {len(hold_rows)} → {HOLDOUT_PATH}")
    print(f"families used (eval): {sorted({r[0].rsplit('-TC', 1)[0].rsplit('-', 1)[0] for r in eval_rows})[:8]} ...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
