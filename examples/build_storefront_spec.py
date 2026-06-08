"""Build the Self-Running Storefront capstone as a pipeline-v2 WorkflowSpec.

This hand-authors the object-agnostic Phase-1 artifact (spec-mods.jsonl) that
mirrors examples/self-running-storefront.md. Objects are intentionally NOT derived
here — that is Phase 2 (bind_spec), which turns this spec into the evaluable
workflows-mods.jsonl.

Run:  python examples/build_storefront_spec.py
Then: python -m src.data.bind_spec -i outputs/data/capstone/self-running-storefront/spec-mods.jsonl
"""
from pathlib import Path

from src.data.schema import (
    Ambiguity,
    EventExpect,
    ModType,
    SpecEvent,
    SpecEventWithExpect,
    SpecModification,
    SpecStep,
    StateConstraint,
    StateConstraintType,
    WorkflowSpec,
)
from src.data.utils import load_jsonl

OUT = Path("outputs/data/capstone/self-running-storefront/spec-mods.jsonl")

# ── The single formal cross-request invariant the base scenario exercises ──────
# (The example carries several aggregates; the weekly promo-budget cap is the one
#  the concurrent race in the base scenario stresses, so it is the recorded one.)
STATE_CONSTRAINT = StateConstraint(
    type=StateConstraintType.cap,
    threshold="$10,000 promotional discount per calendar week",
    description=(
        "Cumulative promotional discount granted within a week must never exceed "
        "the weekly budget cap. When remaining headroom is too small for a "
        "discount, that discount must be denied (and escalated), not silently "
        "granted — even under concurrent checkouts reserving against the same "
        "headroom."
    ),
)

# ── Abstract policy workflow (object-free) ────────────────────────────────────
TEMPLATE = [
    "Capture each customer purchase request as it arrives.",
    "Reserve stock for every line item; reserving a unit that leaves on-hand at or below the reorder point triggers a restock evaluation for that item.",
    "On a restock evaluation, reorder only if on-hand is at/below the reorder point AND recent sell-through meets the reorder threshold AND the item is not clearance.",
    "Apply an item's active promotional discount only if the cumulative weekly discount stays within the promotional budget cap; otherwise deny the discount and escalate.",
    "Classify the customer's risk from return rate and payment standing; hold a high-risk order when it claims the last unit of a non-restockable, high-value item.",
    "Let customers redeem store credit up to their balance and charge the remainder to card; a declined card voids the order and releases reservations.",
    "Defer any supplier payment that would drop cash below the floor.",
    "Recompute the customer's loyalty tier from cumulative spend after each completed order and grant tier perks.",
    "Ship completed orders via a carrier with remaining daily capacity, forcing express for entitled tiers; hold the shipment if no eligible carrier has capacity.",
    "On a returned item, refund the customer, restore undamaged stock, update the return record, and reverse the tax accrual.",
    "Accrue sales tax on each completed order by the customer's jurisdiction.",
    "At end of day, reconcile cash, summarize discounts given vs. budget, list stockouts and escalations, and reset daily capacity and rolling windows.",
]

GROUNDED = [
    "Capture each order on the storefront. Seed catalog: Alpine Jacket M ($220, cost $130, Outerwear, CLEARANCE, on-hand 1, 7-day sales 1); Black Tee ($25, cost $8, Basics, on-hand 5, 7-day sales 60); Trail Boot 42 ($160, cost $95, Footwear, on-hand 12, 7-day sales 18). Reorder point 4.",
    "Reserve stock per line; a sale that leaves on-hand <= 4 triggers a restock evaluation (Alpine Jacket 1->0 fires it; Black Tee 5->4 fires it; Trail Boot 12->11 does not).",
    "Restock evaluation reorders iff on-hand <= 4 AND 7-day sell-through >= 10 AND not clearance; reorder quantity = 2x the 7-day sell-through. (Black Tee reorders 120 units / $960; Alpine Jacket does not — clearance and sell-through 1 < 10.)",
    "Apply the 40% Outerwear promo only if it fits the weekly promo budget: cap $10,000, already $9,850 spent -> $150 free. Reserve atomically; deny + escalate if spent + held + this_discount > $10,000.",
    "Elevated-risk = return rate over last 10 orders >= 50% OR an unresolved bounced-payment/chargeback flag within 90 days. Hold for review iff the order claims the last unit of an item the restock rule won't reorder AND that line's value >= $100. Dana: 6/10 returns, 60-day payment flag, last Alpine Jacket M, $132 line -> HELD.",
    "Customers may redeem store credit (wallet never negative; redeemed credit brings in no new cash); remainder to card; declined card voids. Dana elects $40 wallet + remainder on card.",
    "Cash floor $2,000; defer a supplier payment that would breach it. Seed cash today $2,150 after morning refunds.",
    "Loyalty from cumulative spend (post-discount subtotal excluding tax; wallet-paid still counts): Silver >= $1,000, Gold >= $5,000; recompute only on completed orders. Gold = free express + promo early access. Dana at $4,880 (Silver); a completed $132 order would reach $5,012 (Gold).",
    "Ship via a carrier with remaining daily capacity; Gold forces express. Seed: Express-1 has 1 slot left today, Ground-1 has 50.",
    "On a return: refund to original method, restore undamaged stock, update the return record, reverse tax. Tax rate 8%.",
    "Accrue 8% sales tax on each completed order.",
    "Midnight: reconcile cash, summarize discounts vs the $10,000 budget, list stockouts and escalations, reset Express/Ground daily capacity and roll the 7-day windows.",
]

# ── External-stimulus steps (default behavior, observable expects) ────────────
STEPS = [
    SpecStep(
        text="A repeat customer (Dana: $4,880 lifetime spend, 6/10 returns, a bounced-payment flag 60 days old, $40 store credit) places an order for 1x Alpine Jacket M — the last unit, a clearance item, on a 40% promo ($220 -> $132) — electing to pay $40 from store credit and the rest on card, while the weekly promo budget sits at $9,850 of $10,000.",
        source="storefront",
        expect=EventExpect(
            action="The order is HELD for manual review and nothing is charged: the jacket is reserved (on-hand 1->0) and its restock evaluation declines to reorder (clearance, sell-through 1 < 10); the $88 discount is reserved against the budget (free headroom 150->62); no card charge, no wallet debit; Dana is NOT promoted to Gold.",
            reason="Dana is elevated-risk (6/10 >= 50% and a 60-day payment flag) and the order claims the last unit of a non-restockable item with a line value of $132 >= $100, so all three hold-conditions are met. Spend only counts on a completed order, so a held order cannot trigger the Gold promotion.",
        ),
    ),
    SpecStep(
        text="A previously shipped Trail Boot 42 ($160) arrives back as an undamaged return from a low-risk customer (Priya).",
        source="warehouse",
        expect=EventExpect(
            action="Refund $172.80 ($160 + $12.80 tax) to the original card; restore 1 unit to Trail Boot 42 on-hand; update Priya's return record; reverse the tax accrual; emit a refund notification.",
            reason="Returns intake refunds to the original method, restores undamaged stock, updates the return history, and reverses tax. Refunds are non-discretionary and paid even when cash is tight.",
        ),
    ),
    SpecStep(
        text="The end-of-day clock strikes midnight.",
        source="scheduler",
        expect=EventExpect(
            action="Reconcile the day's cash; summarize promotional discount granted/held vs the $10,000 budget; list items that hit zero on-hand and every escalation; reset per-carrier daily capacity and roll the 7-day sell-through windows.",
            reason="End-of-day close reconciles aggregates and resets the per-day and rolling-window state.",
        ),
    ),
]

# ── State-infused base scenario (Thursday timeline; hard, judgeable expects) ──
# Thursday = W01-4. Concurrency in the budget race is encoded via near-identical
# `when` + trigger_delay_seconds (arrival order). NOTE: the formal concurrent_group
# is a Phase-2 (Event-level) field absent from spec base events — see flag_reasons.
BASE_EVENTS = [
    SpecEventWithExpect(
        id="E001", call_type="send_event", source="__external__",
        input="Dana places an order: 1x Alpine Jacket M (last unit, clearance, 40% promo -> $132). She pays $40 from her store-credit wallet, remainder on card. Context: her lifetime spend is $4,880 (Silver), return rate 6/10, a bounced-payment flag dated 60 days ago, wallet balance $40. The weekly promo budget is $9,850 of $10,000 spent.",
        when="W01-4T14:14", role="base", trigger_delay_seconds=0.0,
        expect=EventExpect(
            action="Order HELD for manual review; nothing charged. Jacket reserved (on-hand 1->0); restock evaluation declines (clearance, sell-through 1 < 10, no reorder); the $88 discount is reserved (budget free headroom 150->62); card not charged, $40 wallet intact; Dana NOT promoted to Gold; an 'order held' notification is sent.",
            reason="Elevated-risk (6/10 >= 50%, 60-day payment flag) AND last unit of a non-restockable item AND line value $132 >= $100 => all hold-conditions met. Spend counts only on completed orders, so the held order cannot promote her.",
        ),
    ),
    SpecEventWithExpect(
        id="E002", call_type="send_event", source="__external__",
        input="A second customer checks out a different discounted Outerwear item with an $88 promotional discount, arriving just after Dana.",
        when="W01-4T14:14", role="base", trigger_delay_seconds=0.05, depends_on=["E001"],
        expect=EventExpect(
            action="The $88 discount is DENIED and escalated to a manager (the item is offered at full price); the weekly budget is never exceeded.",
            reason="After Dana's $88 is reserved only $62 of headroom remains; $9,938 + $88 > $10,000, so the discount cannot be granted.",
        ),
    ),
    SpecEventWithExpect(
        id="E003", call_type="send_event", source="__external__",
        input="A third customer checks out a different discounted Outerwear item with a $96 promotional discount, arriving just after the second.",
        when="W01-4T14:14", role="base", trigger_delay_seconds=0.10, depends_on=["E001"],
        expect=EventExpect(
            action="The $96 discount is DENIED and escalated; the budget holds.",
            reason="Only $62 of headroom remains after Dana's reservation; $96 > $62.",
        ),
    ),
    SpecEventWithExpect(
        id="E004", call_type="send_event", source="__external__",
        input="A fourth customer checks out a different discounted Outerwear item with a $72 promotional discount, arriving just after the third.",
        when="W01-4T14:14", role="base", trigger_delay_seconds=0.15, depends_on=["E001"],
        expect=EventExpect(
            action="The $72 discount is DENIED and escalated; the budget holds. Only Dana (first to reserve) keeps a discount this week.",
            reason="Only $62 of headroom remains after Dana's reservation; $72 > $62. The winner is decided purely by arrival order, not by any property of the orders.",
        ),
    ),
    SpecEventWithExpect(
        id="E005", call_type="send_event", source="__external__",
        input="Omar (Gold, $7,300 lifetime, 0/10 returns, no flags) checks out 1x Trail Boot 42 ($160) + 1x Black Tee ($25), shipping to an 8% jurisdiction.",
        when="W01-4T14:20", role="base",
        expect=EventExpect(
            action="Order COMPLETES. Trail Boot 12->11 (no restock). Black Tee 5->4 fires a restock evaluation that reorders 120 units ($960) — but the supplier payment is HELD by treasury because $2,349.80 cash - $960 = $1,389.80 < the $2,000 floor (PO created, payment deferred, alert raised). $199.80 charged to card ($185 subtotal + $14.80 tax) -> cash $2,349.80. Gold forces express -> Express-1 assigned, its last slot consumed (capacity 1->0). Confirmation + shipped notifications sent.",
            reason="Omar is not elevated-risk so the order completes; the Tee crosses the reorder point and is a steady non-clearance seller so it reorders, but the treasury floor defers the supplier payment; Gold entitles express, taking the final express slot.",
        ),
    ),
    SpecEventWithExpect(
        id="E006", call_type="send_event", source="__external__",
        input="Priya's earlier Trail Boot 42 arrives back as an undamaged return.",
        when="W01-4T14:35", role="base", depends_on=["E005"],
        expect=EventExpect(
            action="Refund $172.80 ($160 + $12.80 tax) to her card -> cash $2,349.80 - $172.80 = $2,177.00. Restore 1 unit -> Trail Boot 11->12. Update Priya's return record (now 2/10, below the risk line). Reverse the tax accrual. Refund notification sent.",
            reason="Returns intake refunds to original method (non-discretionary, paid despite tight cash), restores undamaged stock, updates the return history, and reverses tax.",
        ),
    ),
    SpecEventWithExpect(
        id="E007", call_type="send_event", source="__external__",
        input="A second Gold customer's order completes all checks and needs to ship; it requires express.",
        when="W01-4T14:50", role="base", depends_on=["E005"],
        expect=EventExpect(
            action="The shipment is HELD (no express capacity remains today); fulfillment is alerted, a support case is opened and linked to the order, and the customer is notified of the delay.",
            reason="Express-1's last slot was consumed minutes earlier (E005) and no other carrier offers express today, so the shipment cannot dispatch and must hold.",
        ),
    ),
    SpecEventWithExpect(
        id="E008", call_type="send_event", source="__external__",
        input="The end-of-day clock strikes midnight Thursday.",
        when="W01-4T23:59", role="base", depends_on=["E001", "E005", "E006", "E007"],
        expect=EventExpect(
            action="Close the books: cash reconciled to $2,177.00; promo budget $88 reserved + 3 discounts denied; stockout list = Alpine Jacket M; escalations = Dana HELD, 3 budget denials, Black Tee PO payment held by treasury, one express shipment held; reset Express/Ground daily capacity; roll the 7-day sell-through windows.",
            reason="End-of-day close reconciles the day's aggregates and resets per-day capacity and rolling windows.",
        ),
    ),
]

# ── The runtime rewrite (programmable-online) + events that test it ───────────
MODIFICATIONS = [
    SpecModification(
        id="M001", call_type="modify_workflow", source="__user__",
        when="W01-5T00:00",
        intent=(
            "Black Friday weekend is live. (a) Triple the weekly promotional budget to $30,000. "
            "(b) Treat clearance items as restockable for the weekend. (c) Prioritize express carriers "
            "for all tiers, not just Gold. (d) Suspend risk-HOLDs for elevated-risk customers whose order "
            "total is under $200 — allow-and-flag instead of holding."
        ),
        mod_type=ModType.contextual,
        ambiguity=Ambiguity.precise,
    ),
]

EVENTS = [
    # Baseline-unaffected check: the same kind of risky last-unit order, just BEFORE the rewrite.
    SpecEvent(
        id="E101", call_type="send_event", source="__external__",
        input="Just before midnight, an elevated-risk customer (return rate 7/10) places a $145 order for the last unit of a non-clearance, currently-non-restockable item.",
        when="W01-4T23:30", role="pre_mod",
    ),
    # The headline: Dana's identical click, now under the rewritten rules.
    SpecEvent(
        id="E102", call_type="send_event", source="__external__",
        input="Dana places the identical order again after the rewrite. The weekend clearance-restock policy (clause b) has replenished Alpine Jacket M, so one unit is in stock and her order is again the last unit: 1x Alpine Jacket M (clearance, 40% promo -> $132), $40 from her wallet, remainder on card. Her histories are unchanged: $4,880 lifetime, 6/10 returns, 60-day payment flag, $40 wallet.",
        when="W01-5T09:00", role="post_mod", after_mod_ids=["M001"],
    ),
    # Unrelated functionality still works the same after the rewrite.
    SpecEvent(
        id="E103", call_type="send_event", source="__external__",
        input="A low-risk customer places a normal in-stock, low-value order ($35, item well above its reorder point) the morning after the rewrite.",
        when="W01-5T09:05", role="irrelevant",
    ),
]

spec = WorkflowSpec(
    id="self-running-storefront",
    name="The Self-Running Storefront",
    domain="general",
    source_type="Authored/Capstone",
    link="",
    template=TEMPLATE,
    grounded_steps=GROUNDED,
    steps=STEPS,
    base_events=BASE_EVENTS,
    modifications=MODIFICATIONS,
    events=EVENTS,
    state_constraint=STATE_CONSTRAINT,
    flagged=True,
    flag_reasons=[
        "Hand-authored capstone (not LLM-generated); see examples/self-running-storefront.md for the full prose program and complete deterministic expects.",
        "EXPECTS PRESERVED (confirmed): the 3 steps (-> S###) and 8 base_events (-> SC###) carry authored expects. bind_spec._rewrite_event_expectations skips events whose expect is already set, so all 11 deterministic outcomes survive Phase 2 unchanged.",
        "CONCURRENCY (confirmed harness behavior): base-role events run in a sequential loop (evaluate.py ~L801) and their concurrent_group is IGNORED. The promo-budget race SC001-SC004 therefore fires SEQUENTIALLY, which still deterministically tests the cumulative cap (exactly one discount granted, the rest denied, cap never exceeded). Exercising the atomic lost-update property additionally would require a mod-window concurrent group fired with --concurrency (a separate construct, intentionally not applied to the base scenario here).",
        "MOD-TEST EXPECTS — PINNED POST-BIND: pre/post/irrelevant events E101-E103 carry no spec-level expect (SpecEvent has no expect field); bind_spec LLM-authors them. Run examples/patch_storefront_bound.py on the bound workflows-mods.jsonl to overwrite them with deterministic expects: E101 -> still HELD; E102 -> COMPLETES, Dana promoted to Gold, ships express, jacket reorders (clause b); E103 -> unaffected.",
        "STATE_CONSTRAINT SCOPE: the example carries several aggregates (promo cap, per-SKU reorder window, cash floor, carrier-capacity pool, loyalty tier); only the weekly promo-budget cap is recorded as the formal state_constraint, as it is the invariant the concurrent base race stresses.",
    ],
)

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(spec.model_dump_json() + "\n")

# Round-trip to prove the artifact validates against the v2 loader.
reloaded = load_jsonl(OUT, WorkflowSpec)
assert len(reloaded) == 1
r = reloaded[0]
print(f"OK  wrote {OUT}")
print(f"    id={r.id}  base_events={len(r.base_events)}  steps={len(r.steps)}  "
      f"mods={len(r.modifications)}  events={len(r.events)}  "
      f"constraint={r.state_constraint.type.value if r.state_constraint else None}")
