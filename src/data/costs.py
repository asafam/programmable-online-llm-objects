"""
Cost tracking for data-generation runs.

Every LLM client reports its token usage here (thread-safe); the pipeline labels the
current stage between phases and, at the end of a run, prints a per-stage/per-model
summary and writes `run-costs.json` next to the other artifacts.

Pricing comes from `config/pricing.yaml` ($ per 1M tokens, longest-prefix match on the
model name). Models with no pricing entry still get token counts; their cost is null.
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from pathlib import Path

_PRICING_PATH = Path("config/pricing.yaml")


def _load_pricing() -> dict:
    try:
        import yaml
        return yaml.safe_load(_PRICING_PATH.read_text()) or {}
    except Exception:
        return {}


class CostTracker:
    """Accumulates (stage, model) → token counts across all threads of a run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stage = "unlabeled"
        self._rows: dict = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        self._pricing: dict | None = None

    def reset(self) -> None:
        with self._lock:
            self._rows.clear()
            self.stage = "unlabeled"

    def record(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        with self._lock:
            row = self._rows[(self.stage, model or "unknown")]
            row["calls"] += 1
            row["prompt_tokens"] += int(prompt_tokens or 0)
            row["completion_tokens"] += int(completion_tokens or 0)

    # ── pricing ───────────────────────────────────────────────────────────────
    def _rates(self, model: str):
        if self._pricing is None:
            self._pricing = _load_pricing()
        best = None
        for prefix, rates in self._pricing.items():
            if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
                best = (prefix, rates)
        return best[1] if best else None

    def _cost(self, model: str, pt: int, ct: int):
        r = self._rates(model)
        if not r:
            return None
        return pt / 1e6 * float(r.get("input", 0)) + ct / 1e6 * float(r.get("output", 0))

    # ── reporting ─────────────────────────────────────────────────────────────
    def summary(self) -> dict:
        with self._lock:
            rows = []
            total_cost, priced = 0.0, True
            for (stage, model), v in sorted(self._rows.items()):
                cost = self._cost(model, v["prompt_tokens"], v["completion_tokens"])
                if cost is None:
                    priced = False
                else:
                    total_cost += cost
                rows.append({"stage": stage, "model": model, **v,
                             "cost_usd": round(cost, 4) if cost is not None else None})
            return {
                "rows": rows,
                "total_calls": sum(r["calls"] for r in rows),
                "total_prompt_tokens": sum(r["prompt_tokens"] for r in rows),
                "total_completion_tokens": sum(r["completion_tokens"] for r in rows),
                "total_cost_usd": round(total_cost, 4) if rows and priced else
                                  (round(total_cost, 4) if total_cost else None),
            }

    def print_summary(self) -> None:
        s = self.summary()
        if not s["rows"]:
            return
        print("\n--- Run cost ---")
        for r in s["rows"]:
            cost = f"${r['cost_usd']:.4f}" if r["cost_usd"] is not None else "n/a (no pricing)"
            print(f"  {r['stage']:<12} {r['model']:<18} calls={r['calls']:<4} "
                  f"in={r['prompt_tokens']:<9,} out={r['completion_tokens']:<8,} {cost}")
        tc = f"${s['total_cost_usd']:.4f}" if s["total_cost_usd"] is not None else "n/a"
        print(f"  TOTAL        calls={s['total_calls']}  in={s['total_prompt_tokens']:,}  "
              f"out={s['total_completion_tokens']:,}  cost={tc}")

    def write(self, path: Path) -> None:
        s = self.summary()
        if s["rows"]:
            Path(path).write_text(json.dumps(s, indent=2))


tracker = CostTracker()
