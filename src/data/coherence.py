"""
Cascade-aware post-analysis for rotation-family results.

Strict per-event scoring punishes one early divergence ~30 times on a 31-event
serial chain — it cannot distinguish "shifted but internally consistent" from
"genuinely incoherent". This analyzer re-reads a results JSONL and reports, per
test case:

  strict        events passed / judged (the headline score)
  first_div     id of the first failed event (where the cascade starts)
  coherence     of the events where the system ASSIGNED someone, how many chose
                the front of a rotation seeded by the system's OWN history —
                i.e., was it at least consistent with itself?
  dead_waves    events where nothing was written at all
  holds         events resolved as unassigned hold

Usage:
    python -m src.data.coherence -r <results.jsonl> [--reps "A,B,C"]
"""
from __future__ import annotations

import argparse
import json
import re


ASSIGN_RX = (r"assigned (?:and recorded )?(?:to |it was assigned to )?({reps})",
             r"({reps}) (?:was|as) (?:the )?assigned")


def analyze(results_path: str, reps: list[str]) -> list[dict]:
    rows = [json.loads(l) for l in open(results_path)]
    out = []
    for r in rows:
        if not r.get("sample_id") or not r.get("events"):
            continue
        order = reps[:]
        strict = scored = coherent = dead = holds = 0
        first_div = None
        for e in r["events"]:
            reason = e.get("reasoning") or ""
            passed = bool(e.get("passed"))
            strict += passed
            if not passed and first_div is None:
                first_div = e["event_id"]
            who = None
            for rx in ASSIGN_RX:
                m = re.search(rx.format(reps="|".join(map(re.escape, reps))), reason)
                if m:
                    who = m.group(1)
                    break
            if "unassigned" in reason or "hold" in reason.lower():
                holds += 1
                continue
            if who is None:
                if "no tool call" in reason or "only shows" in reason:
                    dead += 1
                continue
            scored += 1
            if order and who == order[0]:
                coherent += 1
            if who in order:
                order.remove(who)
                order.append(who)
        out.append({
            "sample_id": r["sample_id"],
            "strict": f"{strict}/{len(r['events'])}",
            "first_div": first_div,
            "coherence": f"{coherent}/{scored}",
            "dead_waves": dead,
            "holds": holds,
        })
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results", "-r", required=True)
    p.add_argument("--reps", default="Maya Patel,Jordan Lee,Sofia Ramirez",
                   help="rotation roster in canonical order, comma-separated")
    a = p.parse_args()
    for row in analyze(a.results, [x.strip() for x in a.reps.split(",")]):
        print(json.dumps(row))


if __name__ == "__main__":
    main()
