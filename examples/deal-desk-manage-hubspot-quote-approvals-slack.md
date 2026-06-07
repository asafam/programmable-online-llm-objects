# Example: HubSpot Automated Quote Approval

## Problem statement

Automate your HubSpot quote approval workflow to close deals faster and kick up your sales efficiency.

https://zapier.com/templates/details/deal-desk-manage-hubspot-quote-approvals-slack

## Template

1. A sales rep submits a new quote for approval in HubSpot Quotes
1. The system identifies approvers based on the specific concessions asked for and the rep's reporting chain
1. An email approval request gets sent to the designated approvers
1. The approver reviews the quote details and takes action—approve, reject, or request changes—in Slack
1. If approved, the quote is marked as such in HubSpot, and the rep is free to send it
1. If concessions aren't approved, the quote is marked rejected, and reps can resubmit
1. If quotes aren't approved in 24 hours, stakeholders are tagged in the thread as a reminder

## Grounded steps

1. A sales rep submits a new quote for approval in HubSpot Quotes
1. The system identifies approvers based on the specific concessions asked for and the rep's reporting chain
1. An email approval request gets sent to the designated approvers
1. The approver reviews the quote details and takes action—approve, reject, or request changes—in the #quote-approvals channel on Slack
1. If approved, the quote is marked as such in HubSpot, and the rep is free to send it
1. If concessions aren't approved, the quote is marked rejected, and reps can resubmit
1. If quotes aren't approved in 24 hours, the Deal Desk Team is tagged in the thread as a reminder

## System objects and relationships

```mermaid
graph TD
  HubSpot -->|notifies| QuoteApprovals
  QuoteApprovals -->|identifies approvers| OrganizationDirectory
  OrganizationDirectory -->|provides approver info| QuoteApprovals
  QuoteApprovals -->|sends approval request| Email
  Slack -->|approver action| QuoteApprovals
  QuoteApprovals -->|updates quote status| HubSpot
  QuoteApprovals -->|sends reminders| Slack
  
```

## Sequence diagrams

### Base scenario

A new quote is submitted for approval; a quote approval request is created in HubSpot; the system identifies approvers; an approver approves in Slack, quote gets marked as approved in HubSpot.

```mermaid
sequenceDiagram
  participant User
  participant HubSpot
  participant QuoteApprovals
  participant OrganizationDirectory
  participant Email
  participant Slack

  User->>HubSpot: Submits new quote for approval
  HubSpot->>QuoteApprovals: New quote submitted
  QuoteApprovals->>OrganizationDirectory: Identify approvers based on concessions and reporting chain
  OrganizationDirectory-->>QuoteApprovals: Return approver info
  QuoteApprovals->>Email: Send approval request to approvers
  Slack->>QuoteApprovals: Approver takes action (approve/reject/request changes)
  alt Approved
    QuoteApprovals->>HubSpot: Mark quote as approved
  else Rejected
    QuoteApprovals->>HubSpot: Mark quote as rejected
  end
  QuoteApprovals->>Slack: Send reminder if not approved in 24 hours
```

### Scenario: State-dependent rule — cumulative discount cap

**Workflow rule (to QuoteApprovals):**

```
Track the cumulative approved discount dollars for the current quarter. While the
running total is at or below $50K, route quotes through the normal approval flow.
If approving a quote would push the cumulative total above $50K, do not route it
normally — escalate to the VP of Sales for a budget exception and hold the quote
(do not mark it approved or add it to the total).
```

Unlike the per-quote rules above, this constraint is **value-dependent across requests**: whether a quote can follow the normal flow depends on a running total of discounts already approved this quarter — a figure that lives in no single quote. The object must accumulate that total as state, test each new quote against the $50K ceiling, and switch to escalation once a quote would cross it (and keep escalating, since the cap stays exceeded).

With traditional programming this requires a persistent quarter-to-date accumulator, a reset at quarter boundaries, and branch logic for the crossing case. Here the rule is stated once and the object maintains the total itself.

#### Event sequence (cumulative cap = $50K/quarter)

```mermaid
sequenceDiagram
  participant User
  participant HubSpot
  participant QuoteApprovals
  participant Email

  User->>HubSpot: Submit quote Q1 (discount $20K)
  HubSpot->>QuoteApprovals: New quote Q1, discount $20K
  Note over QuoteApprovals: cumulative $0 + $20K = $20K, at or below $50K: normal routing
  QuoteApprovals->>Email: Send approval request for Q1
  Note over QuoteApprovals: On approval, cumulative: $20K

  User->>HubSpot: Submit quote Q2 (discount $25K)
  HubSpot->>QuoteApprovals: New quote Q2, discount $25K
  Note over QuoteApprovals: cumulative $20K + $25K = $45K, at or below $50K: normal routing
  QuoteApprovals->>Email: Send approval request for Q2
  Note over QuoteApprovals: On approval, cumulative: $45K

  User->>HubSpot: Submit quote Q3 (discount $12K)
  HubSpot->>QuoteApprovals: New quote Q3, discount $12K
  Note over QuoteApprovals: $45K + $12K = $57K, over $50K: would cross cap
  QuoteApprovals->>Email: Escalate Q3 to VP of Sales for budget exception and hold quote
  Note over QuoteApprovals: Q3 not approved: cumulative stays $45K

  User->>HubSpot: Submit quote Q4 (discount $8K)
  HubSpot->>QuoteApprovals: New quote Q4, discount $8K
  Note over QuoteApprovals: $45K + $8K = $53K, over $50K: still over cap
  QuoteApprovals->>Email: Escalate Q4 to VP of Sales and hold quote
```

### Scenario: Simple conflict resolution

**Modification 1 (to QuoteApprovals):**

```
Quotes under $10K auto-approve
```

**Modification 2 (to Slack):**

```
All approvals must be posted to #quote-approvals with approver name
```

In this scenario, the system receives two conflicting instructions: QuoteApprovals is told to auto-approve quotes under $10K, while Slack is told that all approvals must be posted in #quote-approvals with the approver's name. Yet, autoapproved quotes won't have an approver name to post in Slack. The system needs to determine how to handle this conflict. 

With traditional code based programs, the unavailability of the approver name for auto-approved quotes would likely be an edge case that isn't handled, resulting in errors or missing notifications in Slack.

#### Modification sequence

```mermaid
sequenceDiagram
  participant Operator
  participant QuoteApprovals
  participant Slack

  Operator->>QuoteApprovals: If quote is under $10K, auto-approve without sending email or requiring approver action
  Operator->>Slack: All approvals must be posted to #35;quote-approvals with approver name
```

#### New event sequence

```mermaid
sequenceDiagram
  participant User
  participant HubSpot
  participant QuoteApprovals
  participant OrganizationDirectory
  participant Email
  participant Slack

  User->>HubSpot: Submits new quote for $8K approval
  HubSpot->>QuoteApprovals: New quote submitted for $8K
  QuoteApprovals->>QuoteApprovals: Quote is under $10K, auto-approve without sending email or requiring approver action
  QuoteApprovals->>HubSpot: Mark quote as approved
  QuoteApprovals->>Slack: Send approval notification"
  Slack->>Slack: Error - No approver name to post in #35;quote-approvals
  Slack-->>QuoteApprovals: Who is the approver for this quote approval that needs to be posted in #35;quote-approvals?
  QuoteApprovals-->>Slack: No approver since this quote was auto-approved
  Slack->>Slack: Post approval notification in #35;quote-approvals without approver name
```

### Scenario: Underspecified conflict resolution with probablistic state
**Modification (to QuoteApprovals):**

```
Enterprise quotes require VP approval
```

In this scenario, QuoteApprovals is given a new requirement that enterprise quotes require VP approval. However, in this example, the system doesn't have enough information to determine whether ACME corp is an enterprise customer and who the required approver should be. LLM-objects communicate with each other to identify the missing information and determine the appropriate approver. The confidence level of the system can be be updated as it gathers more information, and it can even seek clarification from the user if needed.
With traditional programming, this would likely result in a failure to route the approval request correctly, causing delays and possibly lost deals.

#### Modification sequence

```mermaid
sequenceDiagram
  participant Operator
  participant QuoteApprovals

  Operator->>QuoteApprovals: If quote is from an enterprise customer, require VP approval
```

#### New event sequence

```mermaid
sequenceDiagram
  participant User
  participant HubSpot
  participant QuoteApprovals
  participant OrganizationDirectory
  participant Slack

  User->>HubSpot: Submits new quote for Acme Corp 
  HubSpot->>QuoteApprovals: Quote from Acme Corp (tier: null)
  QuoteApprovals-->>QuoteApprovals: Enterprise requires VP. Is Acme Corp enterprise?
  Note over QuoteApprovals: {enterprise: 0.5, standard: 0.5}

  QuoteApprovals->>HubSpot: What is Acme Corp's tier?
  HubSpot-->>QuoteApprovals: Tier not specified
  Note over QuoteApprovals: {enterprise: 0.5, standard: 0.5}
  
  QuoteApprovals->>HubSpot: What is the original deal amount before discounts for this quote?
  HubSpot-->>QuoteApprovals: Original deal amount is $65K
  Note over QuoteApprovals: Med-high amount signal: {enterprise: 0.7, standard: 0.3}
  
  QuoteApprovals->>OrganizationDirectory: Who should approve quotes for Acme Corp?
  OrganizationDirectory-->>QuoteApprovals: Ted, director, from SMB and New Customers Sales is the approver for Acme Corp
  Note over QuoteApprovals: SMB team signal -> {enterprise: 0.55, standard: 0.45}

  QuoteApprovals->>OrganizationDirectory: Does Ted handles any enterprise accounts?
  OrganizationDirectory-->>QuoteApprovals: No, none of Ted's accounts are enterprise
  Note over QuoteApprovals: SMB team signal -> {enterprise: 0.15, standard: 0.85}

  QuoteApprovals ->> QuoteApprovals: Conflicting signals, but overall leaning towards standard.
  QuoteApprovals->>Email: Send approval request to Ted
```

### Scenario: Conflicting instructions, no breakage

**Modification 1 (to QuoteApprovals):**

```
Quotes under $10K auto-approve
```

**Modification 2 (to QuoteApprovals):**

```
From now on, All quotes to new customers require manager approval
```

In this scenario, QuoteApprovals receives two conflicting instructions: auto-approve quotes under $10K, and require manager approval for all quotes to new customers. If a quote is submitted for a new customer that's under $10K, the system needs to determine which instruction takes precedence. The system potentially can demonstrate proactiveness by recognizing the conflict and seeking clarification, or it can apply a default conflict resolution strategy (e.g., stricter rule wins, or most recent instruction takes precedence).
With traditional programming, this would likely require additional code to handle this specific edge case, and if not handled correctly, could lead to incorrect approvals or rejections.

#### Modification sequence

```mermaid
sequenceDiagram
  participant Operator
  participant QuoteApprovals

  Operator->>QuoteApprovals: If quote is under $10K, auto-approve without sending email or requiring approver action
  Operator->>QuoteApprovals: From now on, All quotes to new customers require manager approval
  Note over QuoteApprovals: A possible conflict detected. Rules don't override each other.
  QuoteApprovals-->>QuoteApprovals: Recent rule wins: require review
  QuoteApprovals-->>Operator: Notify operator of conflict and resolution
```

#### New event sequence

```mermaid
  participant User
  participant HubSpot
  participant QuoteApprovals
  participant OrganizationDirectory
  participant Email

  User->>HubSpot: Submits new quote for $8K for new customer
  HubSpot->>QuoteApprovals: New quote submitted for $8K for new customer
  QuoteApprovals->>QuoteApprovals: Quotes under $10K should be auto-approved, but new customer requires approval
  QuoteApprovals->>OrganizationDirectory: Identify manager approver for new customer
  OrganizationDirectory-->>QuoteApprovals: Return manager approver info
  QuoteApprovals->>Email: Send approval request to manager approver
```

### Scenario: Retroactive modification

**Modification (to QuoteApprovals):**

```
Effective immediately, any concessions involving discounts over 20% require CFO approval
```

A modification is made to the system that requires retroactive changes to existing quotes in the approval pipeline. With traditional programming paradigm requiring updated code is not enough. A migration script is needed to update existing quotes to comply with the new logic. With natural language programming, you can simply state the new requirement and the system can automatically identify which existing quotes are affected and update them accordingly.

#### Modification sequence

```mermaid
sequenceDiagram
  participant Operator
  participant QuoteApprovals
  participant Email

  Operator->>QuoteApprovals: If quote includes concessions with over 20% discount, flag for CFO approval
  QuoteApprovals->>QuoteApprovals: Check for open concessions with over 20% discount that did not require CFO approval
  alt Found any
    QuoteApprovals->>Email: Send approval request to CFO
  else No open concessions with over 20% discount without CFO approval
    Note right of QuoteApprovals: No action needed for existing quotes
  end
```

#### New event sequence

```mermaid
sequenceDiagram
  participant User
  participant HubSpot
  participant QuoteApprovals
  participant OrganizationDirectory
  participant Email
  participant Slack

  HubSpot->>QuoteApprovals: New quote submitted with concessions
  QuoteApprovals->>OrganizationDirectory: Identify approvers based on concessions and reporting chain
  OrganizationDirectory-->>QuoteApprovals: Return approver info and include the CFO
  QuoteApprovals->>Email: Send approval request to approvers including CFO
```

### Scenario with modification: Alternate approver vs. OOO routing

**Modification (to Email):**

```
If an approver is OOO, Email should route the request to their direct manager instead
```

In this example, the system is faced with conflicting instructions: an approval should be sent to the designated approver manager, if the approver is out-of-office. An approval request define an alternate approver (e.g., Bob) to route to when the primary approver (e.g., Alice) is unavailable. The system needs to determine which instruction takes precedence and how to route the approval request accordingly. Business context or system defaults may guide the decision.

**Conflict among:**
- **QuoteApprovals**: Knows alternate approver (Bob), doesn't know Alice is OOO
- **Email**: Knows Alice is OOO, doesn't know Bob is alternate
- **OrganizationDirectory**: Knows Carol is Alice's manager, doesn't know approval context

#### Modification sequence

```mermaid
sequenceDiagram
  participant Operator
  participant Email

  Operator->>Email: If an approver is OOO, forward request to their direct manager instead
```

#### New event sequence

```mermaid
sequenceDiagram
  participant User
  participant HubSpot
  participant QuoteApprovals
  participant OrganizationDirectory
  participant Email
  participant Slack

  User->>HubSpot: Submits a new quote for Alice's approval or Bob's if she's not available.
  HubSpot->>QuoteApprovals: New quote submitted
  QuoteApprovals->>OrganizationDirectory: Identify approvers
  OrganizationDirectory-->>QuoteApprovals: Return approver info (Alice)
  QuoteApprovals->>Email: Send approval request to Alice
  Email-->>Email: Alice is OOO, I should forward the approval request to her manager
  Note over Email: Doesn't know Bob is the alternate approver
  Email->>OrganizationDirectory: Get Alice's manager info
  OrganizationDirectory-->>Email: Return Alice's manager info (Carol)
  Email->>Email: Forward approval request to Carol since Alice is OOO
  Email-->>QuoteApprovals: Notify that approval request was forwarded to Carol (Alice is OOO)
  alt QuoteApprovals overrides Email instruction with alternate approver
    QuoteApprovals->>QuoteApprovals: I should override the Email instruction and send approval the request to Bob since this is a time sensitive request
    QuoteApprovals->>OrganizationDirectory: Get Bob's info
    OrganizationDirectory-->>QuoteApprovals: Return Bob's info
    QuoteApprovals->>Email: Send approval request to Bob since he's the alternate approver and cancel the request to Carol
    Email-->>QuoteApprovals: Notify that approval request was sent to Bob and the request to Carol was cancelled
  else Email instruction takes precedence
    Note over QuoteApprovals: Continue with the original flow
    QuoteApprovals->>QuoteApprovals: I should treat the sale rep requests only as recommendations. I will respect the explicit Email instructions
  end
```

### Scenario with modification: 3 Conflicting instructions

**Modification 1 (to QuoteApprovals):**

```
Enterprise quotes must be approved by VP or above
```

**Modification 2 (to OrganizationDirectory):**

```
EMEA quotes must be approved by someone in the EMEA region
```

**Modification 3 (to Slack):**

```
If no approval in 4 hours, tag the assigned approver's manager in #quote-approvals
```

**The conflicts:**

QuoteApprovals vs. OrganizationDirectory: QuoteApprovals needs VP. OrganizationDirectory can only provide Bob (EMEA Director). No valid approver -- not a VP.
Slack vs. OrganizationDirectory: After 4 hours, Slack asks OrganizationDirectory for Bob's manager. Gets Carol (VP, APAC). Carol is VP—but not EMEA.
Slack vs. QuoteApprovals: Slack escalates to Carol. But QuoteApprovals never assigned Carol. Is Carol now the approver? Or just notified?

#### Modification sequence

```mermaid
sequenceDiagram
  participant Operator
  participant QuoteApprovals
  participant OrganizationDirectory
  participant Slack

  Operator->>QuoteApprovals: Enterprise quotes must be approved by VP or above
  Operator->>OrganizationDirectory: EMEA quotes must be approved by someone in EMEA region
  Operator->>Slack: If no approval in 4 hours, tag approver's manager in #quote-approvals
```

#### New event sequence

```mermaid
sequenceDiagram
  participant User
  participant HubSpot
  participant QuoteApprovals
  participant OrganizationDirectory
  participant Slack

  User->>HubSpot: Submits new quote for approval from EMEA enterprise customer
  HubSpot->>QuoteApprovals: Enterprise quote from EMEA customer
  QuoteApprovals->>OrganizationDirectory: Get VP approver for EMEA customer
  OrganizationDirectory-->>QuoteApprovals: Bob (Director, EMEA) available.
  QuoteApprovals-->>QuoteApprovals: Bob doesn't meet VP requirement
  QuoteApprovals->>OrganizationDirectory: Get Bob's manager info for escalation
  OrganizationDirectory-->>QuoteApprovals: Carol (VP, APAC) but EMEA requests should be approved by someone in EMEA
  OrganizationDirectory-->>QuoteApprovals: No valid approver found that meets all criteria. Accept Bob as fallback since he's the closest valid approver.
  QuoteApprovals->>Email: Send approval request to Bob
  Note over QuoteApprovals: 4 hours pass, no response
  Slack->>QuoteApprovals: Which quotes are pending approval for over 4 hours and who are their approvers?
  QuoteApprovals->>QuoteApprovals: Bob is approver for the EMEA enterprise quote that's pending for over 4 hours
  Slack->>OrganizationDirectory: Who is Bob's manager?
  OrganizationDirectory-->>Slack: Carol (VP, APAC)
  Slack->>Slack: Tag Carol in #35;quote-approvals
  Note over Slack: Carol is VP (satisfies rule 1) but APAC (violates rule 2)
  alt Carol responds and approves
    Note over QuoteApprovals: Approved by VP—but not EMEA. Is this valid?
    QuoteApprovals->>HubSpot: Mark quote as approved
  else Carol ignores (not her region)
    Note over Slack: Escalation failed. No valid approver found.
    Slack->>Slack: Send follow-up message in #35;quote-approvals tagging Quote Desk Team for assistance since no valid approver found.
  end
```

### Scenario: Negotiate state transition (without user intervention)

**Modification (to QuoteApprovals):**

```
Big deals needs CFO approval
```

In this scenario, QuoteApprovals receives a new instruction that big deals require CFO approval. However, "big deal" is an ambiguous term that isn't clearly defined in the system. The system needs to negotiate the definition of "big deal" by communicating with other objects to gather necessary information (e.g., historical deal sizes, company benchmarks) and potentially seek clarification from the operator. With traditional programming, this would likely require additional code to handle the ambiguity, and if not handled correctly, could lead to inconsistent application of the new rule.

#### Modification sequence

```mermaid
sequenceDiagram
  participant Operator
  participant QuoteApprovals

  Operator->>QuoteApprovals: Big deals need CFO approval
  QuoteApprovals->>QuoteApprovals: What is "big"? Propose: over $100K
  QuoteApprovals->>OrganizationDirectory: What is typical "big deal" threshold?
  OrganizationDirectory-->>QuoteApprovals: Alice belongs to the premium sales team, check their average deal size
  QuoteApprovals->>HubSpot: What is the average deal size for Alice's team?
  HubSpot-->>QuoteApprovals: They deal with quotes above $75K
  QuoteApprovals-->>QuoteApprovals: Counter-propose: over $75K
  QuoteApprovals->>Slack: "Big deals" need CFO approval. Based on historical data, I propose defining "big deal" as over $75K. Does this sound right?
  Slack-->>QuoteApprovals: This year, the CFO approved deals above $100K, but for new customers the threshold is $50K
  QuoteApprovals-->>QuoteApprovals: Refine rule with context
  Note over QuoteApprovals: "Big deal" = over $50K (new) or over $100K (existing)
  QuoteApprovals->>Operator: Define "big deal" as over $50K for new customers and over $100K for existing customers. Is that correct?
```