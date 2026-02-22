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

### Scenario: Simple conflict resolution

**Modification 1 (to QuoteApprovals):**

<mark>Quotes under $10K auto-approve</mark>

**Modification 2 (to Slack):**

"==All approvals must be posted to #quote-approvals with approver name=="

In this scenario, the system receives two conflicting instructions: QuoteApprovals is told to auto-approve quotes under $10K, while Slack is told that all approvals must be posted in #quote-approvals with the approver's name. Yet, autoapproved quotes won't have an approver name to post in Slack. The system needs to determine how to handle this conflict. 

With traditional code based programs, the unavailability of the approver name for auto-approved quotes would likely be an edge case that isn't handled, resulting in errors or missing notifications in Slack.

```mermaid
sequenceDiagram
  participant Operator
  participant QuoteApprovals
  participant Slack

  Operator->>QuoteApprovals: If quote is under $10K, auto-approve without sending email or requiring approver action
  Operator->>Slack: All approvals must be posted to \#quote-approvals with approver name
```

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
  Slack->>Slack: Error - No approver name to post in #quote-approvals
  Slack-->>QuoteApprovals: Who is the approver for this quote approval that needs to be posted in #quote-approvals?
  QuoteApprovals-->>Slack: No approver since this quote was auto-approved
  Slack->>Slack: Post approval notification in #quote-approvals without approver name
```

### Scenario with modification: Retroactive modification

**Modification (to QuoteApprovals):**

=="Effective immediately, any concessions involving discounts over 20% require CFO approval"==

A modification is made to the system that requires retroactive changes to existing quotes in the approval pipeline. With traditional programming paradigm requiring updated code is not enough. A migration script is needed to update existing quotes to comply with the new logic. With natural language programming, you can simply state the new requirement and the system can automatically identify which existing quotes are affected and update them accordingly.

```mermaid
sequenceDiagram
  participant Operator
  participant QuoteApprovals
  participant Email

  Operator->>QuoteApprovals: If quote includes concessions with >20% discount, flag for CFO approval
  QuoteApprovals->>QuoteApprovals: Check for open concessions with >20% discount that did not require CFO approval
  alt Found any
    QuoteApprovals->>Email: Send approval request to CFO
  else No open concessions with >20% discount without CFO approval
    Note right of QuoteApprovals: No action needed for existing quotes
  end
```

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

=="If an approver is OOO, Email should route the request to their direct manager instead"==

In this example, the system is faced with conflicting instructions: an approval should be sent to the designated approver manager, if the approver is out-of-office. An approval request define an alternate approver (e.g., Bob) to route to when the primary approver (e.g., Alice) is unavailable. The system needs to determine which instruction takes precedence and how to route the approval request accordingly. Business context or system defaults may guide the decision.

**Conflict among:**
- **QuoteApprovals**: Knows alternate approver (Bob), doesn't know Alice is OOO
- **Email**: Knows Alice is OOO, doesn't know Bob is alternate
- **OrganizationDirectory**: Knows Carol is Alice's manager, doesn't know approval context

```mermaid
sequenceDiagram
  participant Operator
  participant Email

  Operator->>Email: If an approver is OOO, forward request to their direct manager instead
```

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

"==Enterprise quotes must be approved by VP or above=="

**Modification 2 (to OrganizationDirectory):**

"==EMEA quotes must be approved by someone in the EMEA region=="

**Modification 3 (to Slack):**

"==If no approval in 4 hours, tag the assigned approver's manager in #quote-approvals=="

**The conflicts:**

QuoteApprovals vs. OrganizationDirectory: QuoteApprovals needs VP. OrganizationDirectory can only provide Bob (EMEA Director). No valid approver -- not a VP.
Slack vs. OrganizationDirectory: After 4 hours, Slack asks OrganizationDirectory for Bob's manager. Gets Carol (VP, APAC). Carol is VP—but not EMEA.
Slack vs. QuoteApprovals: Slack escalates to Carol. But QuoteApprovals never assigned Carol. Is Carol now the approver? Or just notified?

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
  Slack->>Slack: Tag Carol in #quote-approvals
  Note over Slack: Carol is VP (satisfies rule 1) but APAC (violates rule 2)
  alt Carol responds and approves
    Note over QuoteApprovals: Approved by VP—but not EMEA. Is this valid?
    QuoteApprovals->>HubSpot: Mark quote as approved
  else Carol ignores (not her region)
    Note over Slack: Escalation failed. No valid approver found.
    Slack->>Slack: Send follow-up message in #quote-approvals tagging Quote Desk Team for assistance since no valid approver found.
  end
```
