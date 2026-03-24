"""Scenario tests using real LLM calls.

Objects are defined inline via the Runtime API — the way an admin would
create and wire them in a production session.

Requires OPENAI_API_KEY in .env or environment.

Run:
    pytest tests/test_scenario.py -v -s
"""
import os

import pytest
from dotenv import load_dotenv

from src.lnl.brain import OpenAIBrain
from src.lnl.runtime import Runtime
from src.lnl.types import ObjectDefinition, PeerDeclaration

load_dotenv()

# Skip all tests if no API key is available
pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set",
)


def _make_runtime() -> Runtime:
    brain = OpenAIBrain(model="gpt-4o-mini", temperature=0.0, seed=42)
    return Runtime(brain, strict_peers=False)


def _llm_assert(state: str, condition: str, error_msg: str = "Assertion failed") -> None:
    """Use a separate LLM call to judge whether a condition holds on a state."""
    brain = OpenAIBrain(model="gpt-4o-mini", temperature=0.0, seed=42)
    from src.lnl.types import Message, MessageType

    judge_defn = ObjectDefinition(
        object_id="__judge__",
        role=(
            "You are a strict test assertion judge. "
            "Given a state and a condition, answer ONLY 'yes' or 'no'. "
            "Answer 'yes' if the condition is clearly met, 'no' otherwise."
        ),
    )
    judge_msg = Message(
        sender="__system__",
        recipient="__judge__",
        type=MessageType.ADMIN,
        content=f"State:\n{state}\n\nCondition: {condition}",
    )
    response, _ = brain.process(judge_defn, "", judge_msg, [])
    answer = response.reply.strip().lower().rstrip(".")
    assert answer == "yes", f"{error_msg}\n  Condition: {condition}\n  State: {state}\n  Judge said: {response.reply}"


# ---------------------------------------------------------------------------
# Test 1: Proactive Event — vegan constraint removes meat, rejects chicken
# ---------------------------------------------------------------------------

class TestProactiveEvent:
    """Email from Sarah saying she's going vegan should:
    1. Remove steaks from shopping list
    2. Add a vegan constraint
    3. Reject chicken wings on subsequent add
    """

    def test_vegan_event_removes_meat_and_blocks_chicken(self):
        rt = _make_runtime()

        # --- Admin creates the objects ---

        rt.create_object(ObjectDefinition(
            object_id="shopping-list",
            role="Manages a shopping list of items with quantity, name, and unit price. Enforces any dietary constraints when adding items.",
            state_description=(
                "Track two things: (1) current items, each with name, quantity, and unit price; "
                "(2) a list of active dietary constraints (e.g., 'vegan', 'no dairy'). "
                "Always include both sections in your state, even when empty."
            ),
            behavior=(
                "When asked to add items, add them to the list — but first check against active "
                "constraints and REJECT items that violate them. "
                "When asked to remove items, remove them. "
                "When a dietary constraint is received (e.g., 'going vegan'), you MUST: "
                "(1) add it to the active constraints list, AND (2) remove any existing items "
                "that conflict with the new constraint. Always keep constraints in state for future enforcement."
            ),
            subscriptions=["dietary-updates"],
        ))

        rt.create_object(ObjectDefinition(
            object_id="email-monitor",
            role="Monitors incoming emails and forwards relevant dietary or shopping-related updates to the shopping list.",
            state_description="Track whether any emails have been processed.",
            behavior=(
                "When an email event arrives, determine if it contains dietary or shopping-related information."
            ),
            peers=[PeerDeclaration("shopping-list", "Forward dietary or shopping-related updates from emails via the dietary-updates topic")],
        ))

        # --- Scenario execution ---

        # Step 1: Add steaks
        print("\n[STEP 1] Adding steaks to shopping list")
        results = rt.send("shopping-list", "Add 2 steaks at $10 each to the shopping list")
        print(f"  Reply: {results[0].reply}")
        print(f"  State: {rt.state('shopping-list')}")

        _llm_assert(
            rt.state("shopping-list"),
            "The shopping list contains steaks",
            "Steaks should be in the list after adding them",
        )

        # Step 2: Email event — Sarah is going vegan
        print("\n[STEP 2] Email event: Sarah is going vegan")
        results = rt.inject_event(
            "email-monitor",
            "New email from Sarah: 'I've decided to go vegan! "
            "Please remove all meat from the shopping list.'",
        )
        for r in results:
            print(f"  [{r.object_id}] Reply: {r.reply}")
        print(f"  Shopping list state: {rt.state('shopping-list')}")

        _llm_assert(
            rt.state("shopping-list"),
            "The shopping list does NOT contain steaks or any meat items",
            "Steaks should have been removed after vegan event",
        )
        _llm_assert(
            rt.state("shopping-list"),
            "There is a vegan or no-meat dietary constraint active",
            "Vegan constraint should be stored",
        )

        # Step 3: Try to add chicken — should be rejected
        print("\n[STEP 3] Attempting to add chicken wings (should be rejected)")
        results = rt.send("shopping-list", "Add 1 package of chicken wings for $10")
        print(f"  Reply: {results[0].reply}")
        print(f"  State: {rt.state('shopping-list')}")

        _llm_assert(
            rt.state("shopping-list"),
            "The shopping list does NOT contain chicken wings",
            "Chicken should have been rejected due to vegan constraint",
        )


# ---------------------------------------------------------------------------
# Test 2: Irrelevant Event — "nice weather" causes no meaningful state change
# ---------------------------------------------------------------------------

class TestIrrelevantEvent:
    """Sending 'The weather is nice today' should not modify the shopping list
    items or the budget.
    """

    def test_irrelevant_message_preserves_state(self):
        rt = _make_runtime()

        # --- Admin creates the objects ---

        rt.create_object(ObjectDefinition(
            object_id="shopping-list",
            role="Manages a shopping list of items with quantity, name, and unit price.",
            state_description="Track current items (each with name, quantity, unit price).",
            behavior=(
                "When asked to add items, add them to the list. When asked to remove items, remove them. "
                "Ignore messages that are not related to shopping."
            ),
            peers=[PeerDeclaration("budget-manager", "Notify with item names and costs when items are added or removed")],
        ))

        rt.create_object(ObjectDefinition(
            object_id="budget-manager",
            role="Tracks a budget and spending.",
            state_description="Track the total budget and amount spent so far.",
            behavior=(
                "When notified of item additions, add their cost to spending. When notified of removals, subtract. "
                "Ignore messages that are not related to budget or spending."
            ),
        ))

        # --- Scenario execution ---

        print("\n[STEP 1] Setting budget to $50")
        rt.send("budget-manager", "Set the budget to $50")
        print(f"  Budget state: {rt.state('budget-manager')}")

        print("\n[STEP 2] Adding apples")
        rt.send("shopping-list", "Add 2 apples at $1 each")
        print(f"  Shopping state: {rt.state('shopping-list')}")

        print("\n[STEP 3] Adding milk")
        rt.send("shopping-list", "Add 1 milk at $4")
        print(f"  Shopping state: {rt.state('shopping-list')}")

        # Send irrelevant message
        print("\n[STEP 4] Sending irrelevant message: 'The weather is nice today'")
        results = rt.send("shopping-list", "The weather is nice today")
        print(f"  Reply: {results[0].reply}")
        print(f"  State: {rt.state('shopping-list')}")

        _llm_assert(
            rt.state("shopping-list"),
            "The shopping list still contains apples and milk with the same quantities",
            "Items should be unchanged after irrelevant message",
        )

        _llm_assert(
            rt.state("budget-manager"),
            "The budget is still $50",
            "Budget should be unchanged after irrelevant message",
        )


# ---------------------------------------------------------------------------
# Test 3: Affirmative Non-Conflicting Event — "ok to eat meat" is a no-op
# ---------------------------------------------------------------------------

class TestAffirmativeEvent:
    """When the list already has meat items and an email says 'ok to eat meat',
    nothing should change — no items removed, no new constraints.
    """

    def test_affirmative_message_preserves_state(self):
        rt = _make_runtime()

        # --- Admin creates the objects ---

        rt.create_object(ObjectDefinition(
            object_id="shopping-list",
            role="Manages a shopping list of items with quantity, name, and unit price. Enforces any dietary constraints when adding items.",
            state_description="Track current items (each with name, quantity, unit price) and any active dietary constraints.",
            behavior=(
                "When asked to add items, add them to the list. When asked to remove items, remove them. "
                "When a dietary constraint is received, enforce it. Only change items or constraints when there is "
                "an actual conflict or new restriction — do not modify state for redundant affirmations."
            ),
            subscriptions=["dietary-updates"],
        ))

        rt.create_object(ObjectDefinition(
            object_id="email-monitor",
            role="Monitors incoming emails and forwards relevant dietary or shopping-related updates to the shopping list.",
            state_description="Track whether any emails have been processed.",
            behavior=(
                "When an email event arrives, determine if it contains dietary or shopping-related information."
            ),
            peers=[PeerDeclaration("shopping-list", "Forward dietary or shopping-related updates from emails via the dietary-updates topic")],
        ))

        # --- Scenario execution ---

        print("\n[STEP 1] Adding steaks")
        rt.send("shopping-list", "Add 2 steaks at $10 each")
        print(f"  State: {rt.state('shopping-list')}")

        print("\n[STEP 2] Adding chicken breast")
        rt.send("shopping-list", "Add 1 chicken breast at $5")
        print(f"  State: {rt.state('shopping-list')}")

        _llm_assert(
            rt.state("shopping-list"),
            "The shopping list contains both steaks and chicken breast",
            "Both meat items should be present",
        )

        # Send affirmative email
        print("\n[STEP 3] Email event: 'ok to eat meat' (should be no-op)")
        results = rt.inject_event(
            "email-monitor",
            "New email from Sarah: 'Just confirming - it's totally fine to buy meat products.'",
        )
        for r in results:
            print(f"  [{r.object_id}] Reply: {r.reply}")
        print(f"  Shopping list state: {rt.state('shopping-list')}")

        _llm_assert(
            rt.state("shopping-list"),
            "The shopping list still contains steaks and chicken breast",
            "Meat items should still be present after affirmative message",
        )

        _llm_assert(
            rt.state("shopping-list"),
            "There are NO vegan or vegetarian or no-meat dietary restrictions or constraints",
            "No dietary restrictions should have been added",
        )


# ---------------------------------------------------------------------------
# Test 4: Object Interaction — shopping list notifies budget on item changes
# ---------------------------------------------------------------------------

class TestBudgetInteraction:
    """Adding items to the shopping list should trigger a message chain to
    the budget manager, which updates its remaining budget accordingly.
    """

    def test_shopping_list_updates_budget_on_add(self):
        rt = _make_runtime()

        # --- Admin creates the objects ---

        rt.create_object(ObjectDefinition(
            object_id="shopping-list",
            role="Manages a shopping list of items with quantity, name, and unit price.",
            state_description="Track current items (each with name, quantity, unit price).",
            behavior="When items are added, append them to the list. When items are removed, remove them.",
            peers=[PeerDeclaration("budget-manager", "Notify with item names and costs when items are added or removed")],
        ))

        rt.create_object(ObjectDefinition(
            object_id="budget-manager",
            role="Tracks a household budget.",
            state_description=(
                "Track: (1) total budget, (2) total spent so far, (3) remaining budget. "
                "Always include all three values in your state."
            ),
            behavior=(
                "When you receive a notification about items being added, increase total spent "
                "by the cost and decrease remaining budget. "
                "When you receive a notification about items being removed, decrease total spent "
                "and increase remaining budget. "
                "Always recompute remaining = total budget - total spent."
            ),
        ))

        # --- Scenario execution ---

        # Step 1: Set the budget
        print("\n[STEP 1] Setting budget to $100")
        results = rt.send("budget-manager", "Set the total budget to $100")
        print(f"  Reply: {results[0].reply}")
        print(f"  Budget state: {rt.state('budget-manager')}")

        _llm_assert(
            rt.state("budget-manager"),
            "The total budget is $100 and remaining budget is $100",
            "Budget should be initialized to $100",
        )

        # Step 2: Add apples — should chain to budget-manager
        print("\n[STEP 2] Adding 3 apples at $2 each (total $6)")
        results = rt.send("shopping-list", "Add 3 apples at $2 each")
        print(f"  Results from chain ({len(results)} objects responded):")
        for r in results:
            print(f"    [{r.object_id}] Reply: {r.reply}")
        print(f"  Shopping state: {rt.state('shopping-list')}")
        print(f"  Budget state:   {rt.state('budget-manager')}")

        _llm_assert(
            rt.state("shopping-list"),
            "The shopping list contains 3 apples at $2 each",
            "Apples should be in the list",
        )

        _llm_assert(
            rt.state("budget-manager"),
            "Total spent is $6 and remaining budget is $94",
            "Budget should reflect $6 spent on apples",
        )

        # Step 3: Add milk — should chain to budget-manager again
        print("\n[STEP 3] Adding 2 milk at $5 each (total $10)")
        results = rt.send("shopping-list", "Add 2 milk at $5 each")
        print(f"  Results from chain ({len(results)} objects responded):")
        for r in results:
            print(f"    [{r.object_id}] Reply: {r.reply}")
        print(f"  Shopping state: {rt.state('shopping-list')}")
        print(f"  Budget state:   {rt.state('budget-manager')}")

        _llm_assert(
            rt.state("shopping-list"),
            "The shopping list contains apples and milk",
            "Both items should be in the list",
        )

        _llm_assert(
            rt.state("budget-manager"),
            "Total spent is $16 and remaining budget is $84",
            "Budget should reflect $16 total spent ($6 apples + $10 milk)",
        )

        # Step 4: Add steaks — cumulative budget test
        # Step 4: Add steaks — cumulative budget test
        print("\n[STEP 4] Adding 2 steaks at $12 each (total $24)")
        results = rt.send("shopping-list", "Add 2 steaks at $12 each")
        print(f"  Results from chain ({len(results)} objects responded):")
        for r in results:
            print(f"    [{r.object_id}] Reply: {r.reply}")
        print(f"  Shopping state: {rt.state('shopping-list')}")
        print(f"  Budget state:   {rt.state('budget-manager')}")

        _llm_assert(
            rt.state("budget-manager"),
            "Total spent is $40 and remaining budget is $60",
            "Budget should reflect $40 total spent ($6 + $10 + $24)",
        )


# ---------------------------------------------------------------------------
# Test 5: Live Modification of Peer Contract
# ---------------------------------------------------------------------------

class TestPeerContractModification:
    """Modifying the peer communication contract at runtime should change
    what triggers outgoing messages, while state persists.

    1. Shopping list notifies budget-manager on add (original contract)
    2. Admin modifies contract to "only notify on removals"
    3. Add item — budget should NOT update
    4. Remove item — budget SHOULD update
    """

    def test_modify_peer_contract_changes_notification_behavior(self):
        rt = _make_runtime()

        # --- Admin creates the objects ---

        rt.create_object(ObjectDefinition(
            object_id="shopping-list",
            role="Manages a shopping list of items with quantity, name, and unit price.",
            state_description="Track current items (each with name, quantity, unit price).",
            behavior="When items are added, append them to the list. When items are removed, remove them.",
            peers=[PeerDeclaration("budget-manager", "Notify with item names and costs when items are added or removed")],
        ))

        rt.create_object(ObjectDefinition(
            object_id="budget-manager",
            role="Tracks a household budget.",
            state_description=(
                "Track: (1) total budget, (2) total spent so far, (3) remaining budget. "
                "Always include all three values in your state."
            ),
            behavior=(
                "When you receive a notification about items being added, increase total spent "
                "by the cost and decrease remaining budget. "
                "When you receive a notification about items being removed, decrease total spent "
                "and increase remaining budget. "
                "Always recompute remaining = total budget - total spent."
            ),
        ))

        # --- Step 1: Set budget ---
        print("\n[STEP 1] Setting budget to $100")
        rt.send("budget-manager", "Set the total budget to $100")
        print(f"  Budget state: {rt.state('budget-manager')}")

        # --- Step 2: Add apples — should notify budget (original contract) ---
        print("\n[STEP 2] Adding 3 apples at $2 each (should notify budget)")
        results = rt.send("shopping-list", "Add 3 apples at $2 each")
        print(f"  Results from chain ({len(results)} objects responded):")
        for r in results:
            print(f"    [{r.object_id}] Reply: {r.reply}")
        print(f"  Budget state: {rt.state('budget-manager')}")

        _llm_assert(
            rt.state("budget-manager"),
            "Total spent is $6 and remaining budget is $94",
            "Budget should reflect $6 spent on apples",
        )

        # --- Step 3: Admin modifies peer contract — only notify on removals ---
        print("\n[STEP 3] Admin modifies peer contract: only notify on removals")
        rt.modify(
            "shopping-list",
            peers=[PeerDeclaration("budget-manager", "Notify with item names and costs ONLY when items are REMOVED, not when added")],
        )

        # --- Step 4: Add milk — should NOT notify budget ---
        print("\n[STEP 4] Adding 2 milk at $5 each (should NOT notify budget)")
        results = rt.send("shopping-list", "Add 2 milk at $5 each")
        print(f"  Results from chain ({len(results)} objects responded):")
        for r in results:
            print(f"    [{r.object_id}] Reply: {r.reply}")
        print(f"  Shopping state: {rt.state('shopping-list')}")
        print(f"  Budget state:   {rt.state('budget-manager')}")

        _llm_assert(
            rt.state("shopping-list"),
            "The shopping list contains apples and milk",
            "Milk should be in the list",
        )

        _llm_assert(
            rt.state("budget-manager"),
            "Total spent is $6 and remaining budget is $94",
            "Budget should NOT have changed — add notifications are disabled",
        )

        # --- Step 5: Remove apples — should notify budget ---
        print("\n[STEP 5] Removing apples (should notify budget)")
        results = rt.send("shopping-list", "Remove the apples")
        print(f"  Results from chain ({len(results)} objects responded):")
        for r in results:
            print(f"    [{r.object_id}] Reply: {r.reply}")
        print(f"  Shopping state: {rt.state('shopping-list')}")
        print(f"  Budget state:   {rt.state('budget-manager')}")

        _llm_assert(
            rt.state("shopping-list"),
            "The shopping list contains only milk",
            "Apples should have been removed, only milk remains",
        )

        _llm_assert(
            rt.state("budget-manager"),
            "Total spent is $0 and remaining budget is $100",
            "Budget should reflect $6 decrease from apple removal",
        )


# ---------------------------------------------------------------------------
# Test 6: Service Objects — external services as LLM objects on the bus
# ---------------------------------------------------------------------------

class TestServiceQuery:
    """External services (Active Directory, Slack) are modeled as LLM objects.

    Read-side: active-directory has seeded org data, responds to queries.
    Write-side: slack-notifier records all messages in its state.

    A quote approval request triggers:
    1. quote-approvals queries active-directory for the submitter's manager
    2. quote-approvals sends a notification to slack-notifier
    All via message bus chaining within a single rt.send() call.
    """

    def test_quote_approval_queries_ad_and_notifies_slack(self):
        rt = _make_runtime()

        # --- Service objects ---

        rt.create_object(ObjectDefinition(
            object_id="active-directory",
            role="Responds to queries about organizational structure, employee details, and reporting chains.",
            state_description=(
                "Users:\n"
                "- Alice (title: Sales Rep, manager: Bob, department: Sales)\n"
                "- Bob (title: Sales Director, manager: Carol, department: Sales)\n"
                "- Carol (title: VP Sales, manager: none, department: Sales)\n"
                "- Dave (title: Sales Rep, manager: Bob, department: Sales)"
            ),
            behavior=(
                "When queried about an employee, respond with their details from state. "
                "When queried about a manager or reporting chain, respond with the relevant hierarchy. "
                "When queried about a department, list its members."
            ),
        ))

        rt.create_object(ObjectDefinition(
            object_id="slack-notifier",
            role="Records all notification messages sent through it. Acts as a write-side service representing Slack.",
            state_description="No messages sent yet.",
            behavior="When a message is received, record it in state with the sender and content. Keep a running log of all messages.",
        ))

        # --- Business logic object ---

        rt.create_object(ObjectDefinition(
            object_id="quote-approvals",
            role="Processes quote approval requests. Determines the approval chain based on quote value and submitter's org structure.",
            state_description=(
                "Approval rules:\n"
                "- Quotes under $1000: auto-approved\n"
                "- Quotes $1000-$10000: require submitter's direct manager approval\n"
                "- Quotes over $10000: require VP approval\n\n"
                "No pending approvals."
            ),
            behavior=(
                "When a quote approval request arrives, check the quote value against approval rules. "
                "If auto-approved, notify via slack-notifier. "
                "If manager approval needed, query active-directory for the submitter's manager, then notify the manager via slack-notifier. "
                "If VP approval needed, query active-directory for the VP in the submitter's department, then notify the VP via slack-notifier."
            ),
            peers=[
                PeerDeclaration("active-directory", "Query for employee manager and department VP when routing approvals"),
                PeerDeclaration("slack-notifier", "Send approval notifications and requests to approvers"),
            ],
        ))

        # --- Scenario execution ---

        # Step 1: Submit a $5000 quote for Alice — needs manager (Bob) approval
        print("\n[STEP 1] Submitting $5000 quote for Alice (needs manager approval)")
        results = rt.send("quote-approvals", "New quote approval request: Alice submitted a $5000 quote for Acme Corp")
        print(f"  Results from chain ({len(results)} objects responded):")
        for r in results:
            print(f"    [{r.object_id}] Reply: {r.reply}")
        print(f"  AD state:       {rt.state('active-directory')}")
        print(f"  Slack state:    {rt.state('slack-notifier')}")
        print(f"  Approvals state: {rt.state('quote-approvals')}")

        # Verify: slack-notifier should have recorded a notification mentioning Bob
        _llm_assert(
            rt.state("slack-notifier"),
            "A notification was recorded mentioning Bob as the approver or manager for Alice's quote",
            "Slack should have a notification about Bob approving Alice's quote",
        )

        # Step 2: Submit a $500 quote for Dave — auto-approved
        print("\n[STEP 2] Submitting $500 quote for Dave (should be auto-approved)")
        results = rt.send("quote-approvals", "New quote approval request: Dave submitted a $500 quote for Beta Inc")
        print(f"  Results from chain ({len(results)} objects responded):")
        for r in results:
            print(f"    [{r.object_id}] Reply: {r.reply}")
        print(f"  Slack state:    {rt.state('slack-notifier')}")

        _llm_assert(
            rt.state("slack-notifier"),
            "A notification was recorded about Dave's quote being auto-approved or approved",
            "Slack should have an auto-approval notification for Dave's quote",
        )

        # Step 3: Submit a $15000 quote for Alice — needs VP (Carol) approval
        print("\n[STEP 3] Submitting $15000 quote for Alice (needs VP approval)")
        results = rt.send("quote-approvals", "New quote approval request: Alice submitted a $15000 quote for Mega Corp")
        print(f"  Results from chain ({len(results)} objects responded):")
        for r in results:
            print(f"    [{r.object_id}] Reply: {r.reply}")
        print(f"  Slack state:    {rt.state('slack-notifier')}")

        _llm_assert(
            rt.state("slack-notifier"),
            "A notification was recorded mentioning Carol as the approver for Alice's $15000 quote",
            "Slack should have a notification about Carol (VP) approving Alice's large quote",
        )
