"""Post-bind patch for the Self-Running Storefront capstone.

Phase 2 (`bind_spec`) LLM-authors expects for the modification-test events
E101/E102/E103 (SpecEvent carries no expect). This script overwrites those with
the hand-authored DETERMINISTIC expects so the headline outcome is graded against
a fixed ground truth, per the zero-ambiguity principle.

It does NOT touch the base-race events (SC001-SC004): base events run in a
sequential loop and the harness ignores their concurrent_group, so the race is
already a deterministic cumulative-cap test and needs no patch (see the spec's
flag_reasons for the atomic-concurrency caveat).

Usage (after `python -m src.data.bind_spec -i .../spec-mods.jsonl`):
    python examples/patch_storefront_bound.py \
        outputs/data/capstone/self-running-storefront/workflows-mods.jsonl

Idempotent: re-running overwrites the same three expects to the same values.
"""
import json
import sys
from pathlib import Path

from src.data.schema import Sample

DEFAULT = Path("outputs/data/capstone/self-running-storefront/workflows-mods.jsonl")
TC_ID = "self-running-storefront"

# Deterministic expects to pin onto the bound modification-test events.
EXPECTS = {
    "E101": {
        "action": (
            "The order is HELD for manual review and nothing is charged; the "
            "reservation is kept pending."
        ),
        "reason": (
            "This fires BEFORE modification M001. Under the original risk rule an "
            "elevated-risk customer (7/10 returns >= 50%) claiming the last unit of "
            "a non-restockable item with a line value of $145 >= $100 meets all "
            "three hold-conditions, so the order must be held. Baseline behavior is "
            "unaffected by the not-yet-applied rewrite."
        ),
    },
    "E102": {
        "action": (
            "The order COMPLETES. The $88 discount is granted under the tripled "
            "$30,000 budget; the risk-HOLD is suspended because the $132 order total "
            "is under $200 (allow-and-flag); $40 is redeemed from the wallet (bringing "
            "in no new cash for that $40) and the remainder is charged to card; Dana's "
            "cumulative spend reaches $5,012 and she is promoted to Gold; the order "
            "ships express; and selling this last Alpine Jacket M now triggers a "
            "reorder because clearance is restockable for the weekend."
        ),
        "reason": (
            "Modification M001 (a) tripled the promo budget, (b) made clearance "
            "restockable, (c) prioritized express for all tiers, and (d) suspended "
            "risk-HOLDs for orders under $200. Dana's identical click therefore "
            "resolves through completion, Gold promotion, express shipment, and "
            "reorder, while her unchanged histories ($4,880 spend, 6/10 returns, "
            "$40 wallet) carry straight through the rewrite."
        ),
    },
    "E103": {
        "action": (
            "The order is processed normally and completes: stock is reserved with no "
            "restock evaluation (the item is well above its reorder point), no discount "
            "applies, the card is charged, and tax accrues — exactly as it would have "
            "before the rewrite."
        ),
        "reason": (
            "M001 changed the promo budget, clearance restockability, express priority, "
            "and the under-$200 risk-HOLD rule; none of these touch a normal in-stock, "
            "full-price, low-value order from a low-risk customer, so its handling is "
            "unchanged by the modification."
        ),
    },
}


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not path.exists():
        print(f"ERROR: {path} not found. Run bind_spec first:\n"
              f"  python -m src.data.bind_spec -i "
              f"outputs/data/capstone/self-running-storefront/spec-mods.jsonl",
              file=sys.stderr)
        return 1

    lines = path.read_text().splitlines()
    out_lines, patched_tc, patched_ids = [], False, []
    for line in lines:
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("id") == TC_ID:
            patched_tc = True
            found = {e["id"] for e in rec.get("events", [])}
            for evt in rec["events"]:
                if evt["id"] in EXPECTS:
                    evt["expect"] = dict(EXPECTS[evt["id"]])
                    patched_ids.append(evt["id"])
            missing = set(EXPECTS) - found
            if missing:
                print(f"WARNING: expected events not found in bound TC: {sorted(missing)}",
                      file=sys.stderr)
            # Validate the patched record against the eval schema.
            Sample.model_validate(rec)
        out_lines.append(json.dumps(rec))

    if not patched_tc:
        print(f"ERROR: no test case with id='{TC_ID}' in {path}", file=sys.stderr)
        return 1

    path.write_text("\n".join(out_lines) + "\n")
    print(f"OK  patched {path}")
    print(f"    pinned deterministic expects on: {sorted(patched_ids)}")
    print(f"    (base-race SC001-SC004 left as-is — sequential cumulative-cap test)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
