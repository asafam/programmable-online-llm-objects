"""
LLM-based event role classifier for existing test cases.

For each non-step event in samples.jsonl:
  - Events that fire BEFORE the first modification → "pre_mod" (no LLM needed)
  - Events that fire AFTER the first modification → ask LLM: "post_mod" or "irrelevant"

"irrelevant" means the event tests automation logic that is NOT affected by the modification.
"post_mod" means the event directly exercises the behavior that the modification changes.

After classifying, writes updated roles back into samples.jsonl and (optionally)
re-runs retroactive_classify.py on the specified eval files.

Usage:
    python -m src.data.classify_event_roles \\
        --samples outputs/.../samples.jsonl \\
        --model gpt-4o-mini \\
        --workers 8

    # Also update eval files in-place:
    python -m src.data.classify_event_roles \\
        --samples outputs/.../samples.jsonl \\
        --eval outputs/.../test_cases_eval_*.jsonl \\
        --model gpt-4o-mini --workers 8
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

from src.data.utils import infer_provider

# ── Timestamp parsing ─────────────────────────────────────────────────────────

def _parse_when(when: str) -> float:
    m = re.match(r"W(\d+)-(\d+)T(\d{1,2}):(\d{2})", when or "")
    if not m:
        return 0.0
    wk, dy, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return wk * 10080 + dy * 1440 + hh * 60 + mm


_STEP_RE = re.compile(r"^S\d+$")

_SYSTEM_PROMPT = """\
You classify test events relative to a modification applied to an automation system.

Given:
  - modification_intent: the natural language rule change applied to the system
  - event_input: the external trigger sent to the system

Respond with JSON only:
{
  "role": "post_mod" | "irrelevant",
  "reasoning": "<one sentence>"
}

Definitions:
  post_mod   — the event exercises the SAME behavior that the modification changes.
               A different outcome is expected before vs after the modification fires.
  irrelevant — the event exercises automation logic that is NOT affected by the modification.
               The same outcome is expected regardless of whether the modification has fired.
"""


def _classify_prompt(mod_intent: str, event_input: str) -> str:
    return (
        f"modification_intent: {mod_intent}\n\n"
        f"event_input: {event_input}"
    )


_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "enum": ["post_mod", "irrelevant"]},
        "reasoning": {"type": "string"},
    },
    "required": ["role", "reasoning"],
    "additionalProperties": False,
}


# ── LLM backends ──────────────────────────────────────────────────────────────

def _safe_json(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        # Best-effort extraction
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


class _OpenAIClassifier:
    def __init__(self, model: str):
        from openai import OpenAI
        self.model = model
        self._client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    def classify(self, mod_intent: str, event_input: str) -> tuple[str, str]:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _classify_prompt(mod_intent, event_input)},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = _safe_json(resp.choices[0].message.content or "{}")
        role = raw.get("role", "post_mod")
        if role not in ("post_mod", "irrelevant"):
            role = "post_mod"
        return role, raw.get("reasoning", "")


class _AnthropicClassifier:
    def __init__(self, model: str):
        import anthropic
        self.model = model
        self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=60.0)

    def classify(self, mod_intent: str, event_input: str) -> tuple[str, str]:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=256,
            temperature=0.0,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _classify_prompt(mod_intent, event_input)}],
            output_config={"format": {"type": "json_schema", "schema": _CLASSIFY_SCHEMA}},
        )
        content = "".join(b.text for b in resp.content if hasattr(b, "text"))
        raw = _safe_json(content)
        role = raw.get("role", "post_mod")
        if role not in ("post_mod", "irrelevant"):
            role = "post_mod"
        return role, raw.get("reasoning", "")


def _make_classifier(provider: str, model: str):
    if provider == "openai":
        return _OpenAIClassifier(model)
    return _AnthropicClassifier(model)


# ── Classification work item ───────────────────────────────────────────────────

class _WorkItem:
    __slots__ = ("tc_id", "evt_idx", "mod_intent", "event_input")

    def __init__(self, tc_id: str, evt_idx: int, mod_intent: str, event_input: str):
        self.tc_id = tc_id
        self.evt_idx = evt_idx
        self.mod_intent = mod_intent
        self.event_input = event_input


# ── Main ──────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    tc_path: Path = args.samples
    if not tc_path.exists():
        print(f"Error: {tc_path} not found", file=sys.stderr)
        sys.exit(1)

    provider = getattr(args, "provider", None) or infer_provider(args.model)
    classifier = _make_classifier(provider, args.model)
    print(f"Classifier: {provider}/{args.model}")

    # Load all test cases as raw dicts to preserve unknown fields
    raw_tcs: list[dict] = []
    header: Optional[dict] = None  # Samples wrapper if present

    raw_lines = [l.strip() for l in tc_path.read_text().splitlines() if l.strip()]
    if len(raw_lines) == 1:
        parsed = json.loads(raw_lines[0])
        if "test_cases" in parsed:
            header = parsed
            raw_tcs = parsed["test_cases"]
        else:
            raw_tcs = [parsed]
    else:
        for line in raw_lines:
            raw_tcs.append(json.loads(line))

    print(f"Loaded {len(raw_tcs)} test cases from {tc_path}")

    # Build work items — only events that need LLM classification (post-mod timing, no role yet)
    work_items: list[_WorkItem] = []
    pre_mod_assigned = 0
    already_set = 0
    step_skipped = 0

    for tc in raw_tcs:
        tc_id = tc.get("id", "")
        mods = tc.get("modifications", [])
        events = tc.get("events", [])

        first_mod_when = min(
            (_parse_when(m.get("when", "W99-9T23:59")) for m in mods),
            default=float("inf"),
        )
        # Use first modification intent as the canonical rule change description
        mod_intent = " / ".join(m.get("intent", "") for m in mods if m.get("intent"))

        for idx, evt in enumerate(events):
            eid = evt.get("id", "")
            if _STEP_RE.match(eid):
                step_skipped += 1
                continue

            existing_role = evt.get("role") or None
            if existing_role is not None:
                already_set += 1
                continue

            evt_when = _parse_when(evt.get("when", "W00-0T00:00"))
            if evt_when < first_mod_when:
                evt["role"] = "pre_mod"
                pre_mod_assigned += 1
            else:
                work_items.append(_WorkItem(tc_id, idx, mod_intent, evt.get("input", "")))

    print(f"  pre_mod (timing):  {pre_mod_assigned}")
    print(f"  already set:       {already_set}")
    print(f"  steps skipped:     {step_skipped}")
    print(f"  LLM to classify:   {len(work_items)}")

    if not work_items:
        print("Nothing to classify.")
        _write_back(tc_path, raw_tcs, header)
        _apply_to_evals(args, tc_path)
        return

    # Build tc_id → events list index for quick lookup
    tc_index: dict[str, list] = {tc.get("id", ""): tc["events"] for tc in raw_tcs}

    errors = 0
    irrelevant_count = 0
    post_mod_count = 0
    write_lock = threading.Lock()

    def _do_classify(item: _WorkItem) -> tuple[_WorkItem, str, str]:
        role, reasoning = classifier.classify(item.mod_intent, item.event_input)
        return item, role, reasoning

    with tqdm(total=len(work_items), unit="evt", desc="Classifying") as pbar:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_do_classify, w): w for w in work_items}
            for future in as_completed(futures):
                try:
                    item, role, reasoning = future.result()
                    events_list = tc_index[item.tc_id]
                    with write_lock:
                        events_list[item.evt_idx]["role"] = role
                        if role == "irrelevant":
                            irrelevant_count += 1
                        else:
                            post_mod_count += 1
                except Exception as e:
                    errors += 1
                    tqdm.write(f"ERROR: {e}", file=sys.stderr)
                pbar.update(1)

    print(f"\nResults:")
    print(f"  post_mod:   {post_mod_count}")
    print(f"  irrelevant: {irrelevant_count}")
    print(f"  errors:     {errors}")

    _write_back(tc_path, raw_tcs, header)
    _apply_to_evals(args, tc_path)


def _write_back(tc_path: Path, raw_tcs: list[dict], header: Optional[dict]) -> None:
    """Write updated test cases back to tc_path (backup first)."""
    backup = tc_path.with_suffix(".jsonl.orig_classify")
    if not backup.exists():
        import shutil
        shutil.copy2(tc_path, backup)
        print(f"Backup: {backup}")
    else:
        print(f"Backup already exists, skipping: {backup}")

    with open(tc_path, "w") as f:
        if header is not None:
            header["test_cases"] = raw_tcs
            f.write(json.dumps(header) + "\n")
        else:
            for tc in raw_tcs:
                f.write(json.dumps(tc) + "\n")
    print(f"Updated: {tc_path}")


def _apply_to_evals(args: argparse.Namespace, tc_path: Path) -> None:
    eval_paths: list[Path] = getattr(args, "eval", None) or []
    if not eval_paths:
        return
    from src.data.retroactive_classify import run as retro_run, build_parser as retro_parser
    retro_base = retro_parser()
    for ep in eval_paths:
        print(f"\nRetroactively classifying {ep} ...")
        retro_args = retro_base.parse_args([
            "--eval", str(ep),
            "--samples", str(tc_path),
        ])
        retro_run(retro_args)


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="LLM-classify event roles (post_mod vs irrelevant) in samples.jsonl",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.data.classify_event_roles \\
      --samples outputs/data/zapier/20260407_zapier_clean/samples.jsonl \\
      --model gpt-4o-mini --workers 8

  # Also update eval files after classification:
  python -m src.data.classify_event_roles \\
      --samples outputs/data/zapier/20260407_zapier_clean/samples.jsonl \\
      --eval outputs/data/zapier/20260407_zapier_clean/runs/test_cases_eval_20260410_113327.jsonl \\
      --model gpt-4o-mini --workers 8
""",
    )
    parser.add_argument("--samples", type=Path, required=True, metavar="JSONL",
                        help="samples.jsonl to classify and update in-place")
    parser.add_argument("--eval", type=Path, action="append", default=None, metavar="JSONL",
                        help="Eval file(s) to update after classification (repeatable)")
    parser.add_argument("--model", default="gpt-4o-mini",
                        help="LLM model for classification (default: gpt-4o-mini)")
    parser.add_argument("--provider", default=None, choices=["openai", "anthropic"],
                        help="Provider (inferred from model name if not set)")
    parser.add_argument("--workers", "-w", type=int, default=4,
                        help="Parallel classification workers (default: 4)")
    return parser


def main():
    args = build_parser().parse_args()
    run(args)


if __name__ == "__main__":
    main()
