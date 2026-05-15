#!/usr/bin/env python3
"""Re-run the judge on already-evaluated results without touching the agent.

Reads a results JSONL (from evaluate.py or evaluate_baseline.py), re-scores
each event using the stored `expected`, `evidence`, and `prior_context`, and
writes a new JSONL with a `rejudges` list appended to each event.  The
original `passed` / `reasoning` / `judge_*` fields are left intact.

Usage:
    python scripts/rejudge.py -i <results.jsonl> --model claude-sonnet-4-6
    python scripts/rejudge.py -i <results.jsonl> --model gpt-5.4-mini -o <out.jsonl>
    python scripts/rejudge.py -i <results.jsonl> --model gpt-5.4-mini --workers 8

Output:
    Defaults to updating the input file in place (adds a `rejudges` list to each
    event without touching the original `passed` / `reasoning` / `judge_*` fields).
    Use -o to write to a separate file instead.

    Each rejudge entry:
        {model, provider, passed, reasoning, votes,
         judge_input_tokens, judge_output_tokens}
    Multiple calls with different models accumulate entries in the list.
"""

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _infer_provider(model: str) -> str:
    from src.data.utils import infer_provider
    return infer_provider(model)


def _make_judge(provider: str, model: str):
    if provider == "openai":
        from src.lnl.judge import OpenAIJudge
        return OpenAIJudge(model=model)
    elif provider == "google":
        from src.lnl.judge import GeminiJudge
        return GeminiJudge(model=model)
    else:
        from src.lnl.judge import AnthropicJudge
        return AnthropicJudge(model=model)


def _default_output(input_path: Path, _model: str) -> Path:
    return input_path  # update in place by default


# ── Core ──────────────────────────────────────────────────────────────────────

def rejudge_file(
    input_path: Path,
    output_path: Path,
    model: str,
    provider: str,
    workers: int = 4,
) -> None:
    judge = _make_judge(provider, model)

    # Load all lines preserving order; separate meta from TC records.
    raw_lines: list[str] = []
    records: list[dict] = []       # parsed dicts (meta or TC)
    tc_indices: list[int] = []     # indices into records[] that are TCs

    with open(input_path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            raw_lines.append(line)
            d = json.loads(line)
            records.append(d)
            if "tc_id" in d or "events" in d:
                tc_indices.append(len(records) - 1)

    print(f"  {len(tc_indices)} TC records, {len(records) - len(tc_indices)} meta lines")

    # Collect all (tc_idx, evt_idx) pairs that need judging and aren't
    # already scored by this model in a previous rejudge run.
    pending: list[tuple[int, int]] = []
    for ri in tc_indices:
        for ei, evt in enumerate(records[ri].get("events", [])):
            already = {e.get("model") for e in evt.get("rejudges", [])}
            if model not in already and evt.get("expected") and evt.get("evidence") is not None:
                pending.append((ri, ei))

    if not pending:
        print("  Nothing to do — all events already scored by this model.")
        return

    print(f"  {len(pending)} events to score with {provider}/{model}")

    # Shared result store: (ri, ei) → rejudge entry dict
    results: dict[tuple[int, int], dict] = {}
    lock = threading.Lock()
    err_count = [0]

    def _score(ri: int, ei: int) -> None:
        evt = records[ri]["events"][ei]
        try:
            passed, reasoning, votes, j_in, j_out = judge.evaluate_with_votes(
                evt["expected"],
                evt["evidence"],
                evt.get("prior_context", ""),
            )
            entry = {
                "model": model,
                "provider": provider,
                "passed": passed,
                "reasoning": reasoning,
                "votes": votes,
                "judge_input_tokens": j_in,
                "judge_output_tokens": j_out,
            }
        except Exception as exc:
            entry = {
                "model": model,
                "provider": provider,
                "passed": None,
                "reasoning": f"[rejudge error] {exc}",
                "votes": [],
                "judge_input_tokens": 0,
                "judge_output_tokens": 0,
            }
            with lock:
                err_count[0] += 1
        with lock:
            results[(ri, ei)] = entry

    with tqdm(total=len(pending), unit="evt", desc="Rejudging") as pbar:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_score, ri, ei): (ri, ei) for ri, ei in pending}
            for fut in as_completed(futs):
                fut.result()   # propagate unexpected exceptions
                pbar.update(1)

    # Merge results back into records.
    for (ri, ei), entry in results.items():
        evt = records[ri]["events"][ei]
        evt.setdefault("rejudges", []).append(entry)

    # Write output.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")

    total_in  = sum(r.get("judge_input_tokens",  0) for r in results.values())
    total_out = sum(r.get("judge_output_tokens", 0) for r in results.values())
    passed    = sum(1 for r in results.values() if r.get("passed"))
    scored    = sum(1 for r in results.values() if r.get("passed") is not None)
    print(f"  Pass rate:   {passed}/{scored} ({passed/scored:.1%})" if scored else "  No events scored.")
    print(f"  Tokens:      {total_in:,} in / {total_out:,} out")
    print(f"  Errors:      {err_count[0]}")
    print(f"  Written:     {output_path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-i", "--input",    type=Path, required=True,
                        help="Results JSONL from evaluate.py / evaluate_baseline.py")
    parser.add_argument("-o", "--output",   type=Path, default=None,
                        help="Output path (default: <input>__rejudge_<model>.jsonl)")
    parser.add_argument("--model",          required=True,
                        help="Judge model, e.g. claude-sonnet-4-6, gpt-5.4-mini")
    parser.add_argument("--provider",       default=None,
                        help="Provider override (inferred from model if omitted)")
    parser.add_argument("--workers", "-w",  type=int, default=4,
                        help="Parallel judge threads (default: 4)")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"Input not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    provider = args.provider or _infer_provider(args.model)
    output   = args.output   or _default_output(args.input, args.model)

    print(f"Input:    {args.input}")
    print(f"Output:   {output}")
    print(f"Judge:    {provider}/{args.model}  workers={args.workers}")

    t0 = time.monotonic()
    rejudge_file(args.input, output, args.model, provider, args.workers)
    print(f"  Elapsed: {time.monotonic()-t0:.1f}s")


if __name__ == "__main__":
    main()
