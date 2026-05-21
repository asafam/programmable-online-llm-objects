#!/usr/bin/env python3
"""Quick CLI to edit a Zapier template's raw_steps in templates.yaml.

Usage:
    python scripts/edit_template.py <template-id>
    python scripts/edit_template.py --list-flagged

Opens raw_steps in $EDITOR (defaults to vim). Empty lines and lines starting
with '#' are stripped. Leading '- ' or 'N. ' bullets are removed.
Writes a .bak alongside templates.yaml on save.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
TEMPLATES_PATH = REPO / "data/zapier/raw/templates.yaml"

# Review recommended (1 NO_SECTION + 8 NOTABLE_DRIFT)
FLAGGED_REVIEW: list[tuple[str, str]] = [
    ("slack-notion-task-manager",                          "NO_SECTION — page lacks workflow section; consider re-scrape"),
    ("linkedin-conversion-tracking-for-physical-stores",   "NOTABLE_DRIFT — YAML adds 4 setup steps"),
    ("lead-router",                                        "NOTABLE_DRIFT — YAML reframes features as runtime steps"),
    ("utm-builder",                                        "NOTABLE_DRIFT — adds display-table + downstream Zaps steps"),
    ("order-request-form",                                 "NOTABLE_DRIFT — different framing (storage vs approval flow)"),
    ("round-robin-lead-assignment",                        "NOTABLE_DRIFT — adds rep notification, 3rd-party intake, repeat"),
    ("ai-email-assistant",                                 "NOTABLE_DRIFT — adds Gmail trigger + connect/test/publish setup"),
    ("contact-list",                                       "NOTABLE_DRIFT — adds CSV import + custom fields"),
    ("inventory",                                          "NOTABLE_DRIFT — adds product-data entry, Need-More checkbox, per-product email"),
]

# Optional review (13 MILD_DRIFT — 1 added step each, still workflow-accurate)
FLAGGED_OPTIONAL: list[tuple[str, str]] = [
    ("email-campaign-portal",                              "MILD_DRIFT — YAML focuses on usage flow, page on setup"),
    ("ai-generated-press-mentions",                        "MILD_DRIFT — YAML adds 'email notification to team' step"),
    ("ai-content-idea-generator",                          "MILD_DRIFT — adds 'Publish the workflow' step"),
    ("instagram-content-calendar",                         "MILD_DRIFT — adds db-tracking view step"),
    ("lead-capture",                                       "MILD_DRIFT — adds branding step"),
    ("employee-onboarding-manager",                        "MILD_DRIFT — adds explicit button-click trigger step"),
    ("ai-form",                                            "MILD_DRIFT — adds share/embed step"),
    ("expenses-tracker",                                   "MILD_DRIFT — adds manual data-entry setup step"),
    ("subscription-tracker",                               "MILD_DRIFT — adds customization step"),
    ("g2-reviews-product-team",                            "MILD_DRIFT — adds 'track sentiment changes' (not on page)"),
    ("user-research-customer-interview-signup",            "MILD_DRIFT — adds benefit-statement step"),
    ("import-account-signals-salesloft-looker",            "MILD_DRIFT — adds explicit 'notify reps' step"),
    ("ai-image-generator",                                 "MILD_DRIFT — adds optional Google Drive upload"),
]


def load_templates() -> list[dict]:
    with TEMPLATES_PATH.open() as f:
        return yaml.safe_load(f)


def save_templates(data: list[dict]) -> Path:
    backup = TEMPLATES_PATH.with_suffix(".yaml.bak")
    shutil.copy2(TEMPLATES_PATH, backup)
    with TEMPLATES_PATH.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=120, default_flow_style=False)
    return backup


def edit_in_editor(initial_text: str) -> str:
    editor = os.environ.get("EDITOR", "vim")
    with tempfile.NamedTemporaryFile("w", suffix=".steps.txt", delete=False) as tmp:
        tmp.write(initial_text)
        tmp_path = tmp.name
    try:
        # Do not pass check=True — some editors (vi in particular) can exit
        # non-zero for benign reasons. Read the file regardless; we'll detect
        # an unmodified-or-aborted edit downstream by comparing to the initial
        # text.
        result = subprocess.run([editor, tmp_path])
        if result.returncode != 0:
            print(
                f"warning: editor '{editor}' exited with code {result.returncode}; "
                f"reading the file anyway in case your edits were saved.",
                file=sys.stderr,
            )
        with open(tmp_path) as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def parse_steps(text: str) -> list[str]:
    steps: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        s = line.lstrip()
        if s.startswith("- "):
            s = s[2:]
        else:
            m = re.match(r"^\d+[.)]\s*(.*)$", s)
            if m:
                s = m.group(1)
        if s.strip():
            steps.append(s.strip())
    return steps


def cmd_list_flagged(include_optional: bool) -> None:
    templates = {t["id"]: t for t in load_templates()}

    def _print_group(title: str, items: list[tuple[str, str]]) -> None:
        print(f"\n{title} ({len(items)}):\n")
        for tid, note in items:
            t = templates.get(tid)
            if t is None:
                print(f"  {tid}  [MISSING from templates.yaml]")
                continue
            print(f"  {tid}  ({len(t['raw_steps'])} steps)")
            print(f"    {note}")
            print(f"    {t['link']}\n")

    _print_group("Review recommended", FLAGGED_REVIEW)
    if include_optional:
        _print_group("Optional review (MILD_DRIFT)", FLAGGED_OPTIONAL)
    else:
        print(f"+ {len(FLAGGED_OPTIONAL)} MILD_DRIFT templates (pass --all to see).")


def cmd_edit(tid: str) -> None:
    templates = load_templates()
    target = next((t for t in templates if t["id"] == tid), None)
    if target is None:
        print(f"Template '{tid}' not found in {TEMPLATES_PATH}", file=sys.stderr)
        sys.exit(1)

    header = [
        f"# Editing raw_steps for: {target['id']}",
        f"# Name: {target['name']}",
        f"# Link: {target['link']}",
        "#",
        "# One step per line. Leading '- ' / 'N. ' / 'N) ' bullets are stripped.",
        "# Empty lines and lines starting with '#' are ignored.",
        "# Save and exit to apply; quit without saving to abort.",
        "",
    ]
    initial = "\n".join(header + list(target.get("raw_steps", []))) + "\n"

    edited = edit_in_editor(initial)
    new_steps = parse_steps(edited)

    if not new_steps:
        print("No steps after edit — aborting (nothing saved).", file=sys.stderr)
        sys.exit(1)

    print(f"\nBefore: {len(target.get('raw_steps', []))} step(s)")
    print(f"After:  {len(new_steps)} step(s)")
    for i, s in enumerate(new_steps, 1):
        suffix = "…" if len(s) > 80 else ""
        print(f"  [{i}] {s[:80]}{suffix}")

    resp = input("\nSave to templates.yaml? [y/N]: ").strip().lower()
    if resp != "y":
        print("Aborted (no changes written).")
        sys.exit(0)

    target["raw_steps"] = new_steps
    backup = save_templates(templates)
    print(f"Saved. Backup: {backup}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("template_id", nargs="?", help="Template id to edit (e.g. 'lead-router').")
    parser.add_argument("--list-flagged", action="store_true", help="List templates flagged by the audit (review-recommended only).")
    parser.add_argument("--all", action="store_true", help="With --list-flagged: also show MILD_DRIFT (optional review).")
    args = parser.parse_args()

    if args.list_flagged:
        cmd_list_flagged(include_optional=args.all)
        return
    if not args.template_id:
        parser.print_help()
        sys.exit(1)
    cmd_edit(args.template_id)


if __name__ == "__main__":
    main()
